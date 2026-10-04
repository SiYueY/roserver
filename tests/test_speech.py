"""Phase 5B speech bridge: PCM -> roboagent SpeechSession (docs §5.7).

Two layers are covered:

* the injectable :class:`~roserver.speech.engine.SimulatedSpeechEngine`, which
  drives roboagent's real ``SpeechSession`` with simulated ASR / TTS / agent
  providers and must produce the documented event order deterministically;
* the Product websocket ``/api/v1/media/sessions/{id}/speech``, its envelope,
  barge-in, teardown and fault isolation.

Everything here is offline: no DashScope, no network audio, no WebRTC.
"""

from __future__ import annotations

import asyncio
import base64
import json
import struct
import time
from contextlib import contextmanager
from typing import Iterator

import pytest
from websockets.exceptions import InvalidStatus

from conftest import Api, FakeModel, LiveServer, Turn

from roboagent.speech.audio import PassthroughAudioProcessor
from roboagent.speech.factory import create_speech_session
from roboagent.speech.types import DEFAULT_INPUT_FORMAT, AudioChunk
from roserver.app import create_app
from roserver.config import Settings
from roserver.speech.engine import SimulatedSpeechEngine

# ---------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------
FRAME_SAMPLES = 320  # 20 ms of 16 kHz mono PCM16
LOUD = struct.pack("<%dh" % FRAME_SAMPLES, *([8000] * FRAME_SAMPLES))

STATE_TYPES = {
    "speech.started",
    "speech.stopped",
    "transcript.partial",
    "transcript.final",
    "response.started",
    "response.delta",
    "response.completed",
}


def _chunk() -> AudioChunk:
    return AudioChunk(LOUD, DEFAULT_INPUT_FORMAT, time.monotonic())


async def _collect_all(
    engine: SimulatedSpeechEngine, media_id: str, sink: list
) -> None:
    async for event in engine.events(media_id):
        sink.append(event)


async def _await_type(sink: list, event_type: str, timeout: float = 4.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if any(getattr(event, "type", None) == event_type for event in sink):
            return
        await asyncio.sleep(0.01)
    raise AssertionError(f"timed out waiting for {event_type}: {[e.type for e in sink]}")


async def _feed_loud(engine: SimulatedSpeechEngine, media_id: str, count: int = 6) -> None:
    for _ in range(count):
        await engine.feed_audio(media_id, _chunk())
        await asyncio.sleep(0.01)


@contextmanager
def speech_server(
    settings: Settings,
    *,
    engine: object | None = None,
    model: object | None = None,
) -> Iterator[tuple]:
    app = create_app(
        settings,
        model=model or FakeModel([Turn(text="ok")]),
        speech_engine=engine,  # type: ignore[arg-type]
    )
    with LiveServer(app) as client:
        yield client, app


def _create_call(client, *, session_id: str | None = None) -> str:
    response = client.post(
        "/api/v1/media/sessions",
        json={
            "kind": "call",
            "session_id": session_id,
            "audio": True,
            "video": False,
        },
    )
    assert response.status_code == 201, response.text
    return response.json()["media_session_id"]


def _collect_until(
    ws, stop_types: set[str], *, timeout: float = 5.0, limit: int = 200
) -> list[dict]:
    events: list[dict] = []
    deadline = time.time() + timeout
    while len(events) < limit:
        remaining = deadline - time.time()
        if remaining <= 0:
            break
        try:
            event = ws.receive_json(timeout=remaining)
        except Exception:  # noqa: BLE001 - timeout / peer close both end the loop
            break
        events.append(event)
        if event.get("type") in stop_types:
            break
    return events


def _wait_until(predicate, timeout: float = 3.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


# =====================================================================
# simulated engine: documented event order, deterministically
# =====================================================================
def test_simulated_engine_produces_documented_event_order() -> None:
    async def scenario() -> None:
        engine = SimulatedSpeechEngine()
        await engine.start("media_1", session_id="sess_1")
        seen: list = []
        collector = asyncio.create_task(_collect_all(engine, "media_1", seen))
        try:
            await _feed_loud(engine, "media_1")
            await _await_type(seen, "response.completed")
            await asyncio.sleep(0.05)

            core = [e.type for e in seen if e.type in STATE_TYPES]
            assert core == [
                "speech.started",
                "speech.stopped",
                "transcript.partial",
                "transcript.final",
                "response.started",
                "response.delta",
                "response.delta",
                "response.delta",
                "response.delta",
                "response.completed",
            ]

            started = next(e for e in seen if e.type == "speech.started")
            assert started.turn_id == 1
            assert started.response_id is None
            partial = next(e for e in seen if e.type == "transcript.partial")
            assert partial.text == "hello"
            assert partial.turn_id == 1
            final = next(e for e in seen if e.type == "transcript.final")
            assert final.text == "hello robot"
            assert final.turn_id == 1
            response = next(e for e in seen if e.type == "response.started")
            assert response.turn_id == 1
            assert response.response_id == 1
            completed = next(e for e in seen if e.type == "response.completed")
            assert completed.response_id == 1
        finally:
            collector.cancel()
            await asyncio.gather(collector, return_exceptions=True)
            await engine.stop("media_1")
        assert engine.active_session_count == 0

    asyncio.run(scenario())


def test_simulated_engine_interrupt_clears_output_and_stops_response() -> None:
    async def scenario() -> None:
        engine = SimulatedSpeechEngine(
            response_deltas=tuple("abcdefgh"), response_delay=0.05
        )
        await engine.start("media_1")
        seen: list = []
        collector = asyncio.create_task(_collect_all(engine, "media_1", seen))
        try:
            await _feed_loud(engine, "media_1")
            await _await_type(seen, "response.started")
            await engine.interrupt("media_1", reason="barge_in")
            await asyncio.sleep(0.15)

            types = [e.type for e in seen]
            assert "interrupted" in types
            assert "response.completed" not in types
            index = types.index("interrupted")
            assert "response.delta" not in types[index + 1 :]

            transport = engine.transport("media_1")
            assert transport is not None
            assert transport.clear_output_calls == 1
        finally:
            collector.cancel()
            await asyncio.gather(collector, return_exceptions=True)
            await engine.stop("media_1")

    asyncio.run(scenario())


def test_simulated_engine_stop_releases_every_session() -> None:
    async def scenario() -> None:
        baseline = asyncio.all_tasks()
        engine = SimulatedSpeechEngine()
        for media_id in ("media_a", "media_b"):
            await engine.start(media_id)
        assert engine.active_session_count == 2
        # Start a real turn (opens ASR + agent tasks) before tearing down.
        await engine.feed_audio("media_a", _chunk())
        await asyncio.sleep(0.05)
        await engine.stop("media_a")
        assert engine.active_session_ids == {"media_b"}
        await engine.stop("media_b")
        assert engine.active_session_count == 0
        await asyncio.sleep(0.05)
        # No speech task survives the release.
        assert asyncio.all_tasks() <= baseline

    asyncio.run(scenario())


# =====================================================================
# render observer bridge (docs §5.7)
# =====================================================================
def test_render_observer_is_wired_to_audio_processor() -> None:
    async def scenario() -> None:
        engine = SimulatedSpeechEngine()
        await engine.start("media_1")
        try:
            transport = engine.transport("media_1")
            assert transport is not None
            observer = transport.render_observer
            # The transport hook was set by create_speech_session, not left None.
            assert observer is not None
            # It is the audio processor's bound observe_render, not a stub.
            assert getattr(observer, "__name__", None) == "observe_render"
            assert hasattr(observer.__self__, "observed_render")
        finally:
            await engine.stop("media_1")

    asyncio.run(scenario())


def test_rendered_tts_audio_reaches_render_observer() -> None:
    async def scenario() -> None:
        engine = SimulatedSpeechEngine()
        await engine.start("media_1")
        seen: list = []
        collector = asyncio.create_task(_collect_all(engine, "media_1", seen))
        try:
            await _feed_loud(engine, "media_1")
            await _await_type(seen, "response.completed")
            transport = engine.transport("media_1")
            assert transport is not None

            deadline = time.monotonic() + 2.0
            observations = engine.render_observations("media_1")
            while not observations and time.monotonic() < deadline:
                await asyncio.sleep(0.01)
                observations = engine.render_observations("media_1")

            assert observations, "rendered TTS audio never reached the observer"
            # Observed frames are exactly the frames the transport handed to the
            # outgoing track, in order (rendered may still grow after the turn).
            assert observations == list(transport.rendered_frames)[: len(observations)]
            assert all(frame.data for frame in observations)
        finally:
            collector.cancel()
            await asyncio.gather(collector, return_exceptions=True)
            await engine.stop("media_1")

    asyncio.run(scenario())


class _TransportWithoutRenderHook:
    """A transport that does not provide ``set_render_observer`` (§5.7 guard)."""


def test_transport_without_render_hook_is_skipped() -> None:
    transport = _TransportWithoutRenderHook()
    processor = PassthroughAudioProcessor()
    speech = create_speech_session(
        session=object(),
        transport=transport,
        config=SimulatedSpeechEngine().config,
        asr=object(),
        tts=object(),
        vad=object(),
        audio_processor=processor,
    )
    # The guard path must build a working session without attempting to wire a
    # render observer onto a transport that has no hook.
    assert speech.audio_processor is processor
    assert not hasattr(transport, "render_observer")


def test_speech_settings_are_env_configurable() -> None:
    settings = Settings.from_env(
        {
            "ROSERVER_SPEECH_MAX_AUDIO_FRAME_BYTES": "1024",
            "ROSERVER_SPEECH_MAX_AUDIO_FRAMES": "4",
            "ROSERVER_SPEECH_EVENT_QUEUE_BOUND": "8",
        }
    )
    assert settings.speech_max_audio_frame_bytes == 1024
    assert settings.speech_max_audio_frames == 4
    assert settings.speech_event_queue_bound == 8
    with pytest.raises(ValueError):
        Settings(speech_max_audio_frames=0)


# =====================================================================
# Product websocket: envelope, fields, audio feed
# =====================================================================
def test_ws_emits_product_speech_envelope(settings_factory) -> None:
    engine = SimulatedSpeechEngine()
    with speech_server(settings_factory(), engine=engine) as (client, _app):
        session = Api().create_session(client)
        media_id = _create_call(client, session_id=session["session_id"])
        with client.websocket_connect(
            f"/api/v1/media/sessions/{media_id}/speech"
        ) as ws:
            for _ in range(6):
                ws._connection.send(LOUD)
            events = _collect_until(ws, {"response.completed"})

        assert events, "speech channel emitted nothing"
        types = [event["type"] for event in events]
        assert "speech.started" in types
        assert "transcript.partial" in types
        assert "transcript.final" in types
        assert "response.started" in types
        assert "response.completed" in types

        sequences = [event["sequence"] for event in events]
        assert sequences == sorted(sequences)
        assert len(set(sequences)) == len(sequences)
        for event in events:
            assert set(event) == {
                "media_session_id",
                "session_id",
                "sequence",
                "type",
                "timestamp",
                "data",
            }
            assert event["media_session_id"] == media_id
            assert event["session_id"] == session["session_id"]
            assert {"turn_id", "response_id"} <= set(event["data"])

        partial = next(e for e in events if e["type"] == "transcript.partial")
        assert partial["data"]["text"] == "hello"
        assert partial["data"]["turn_id"] == 1
        final = next(e for e in events if e["type"] == "transcript.final")
        assert final["data"]["text"] == "hello robot"
        delta = next(e for e in events if e["type"] == "response.delta")
        assert delta["data"]["response_id"] == 1


def test_ws_accepts_base64_json_audio(settings_factory) -> None:
    engine = SimulatedSpeechEngine()
    with speech_server(settings_factory(), engine=engine) as (client, _app):
        media_id = _create_call(client)
        with client.websocket_connect(
            f"/api/v1/media/sessions/{media_id}/speech"
        ) as ws:
            for _ in range(6):
                ws.send_json(
                    {
                        "type": "audio",
                        "audio": base64.b64encode(LOUD).decode("ascii"),
                        "format": "pcm16_16k_mono",
                    }
                )
            events = _collect_until(ws, {"response.completed"})
        types = [event["type"] for event in events]
        assert "speech.started" in types
        assert "transcript.final" in types


def test_ws_interrupt_message_drives_barge_in(settings_factory) -> None:
    engine = SimulatedSpeechEngine(
        response_deltas=tuple("abcdefgh"), response_delay=0.05
    )
    with speech_server(settings_factory(), engine=engine) as (client, _app):
        media_id = _create_call(client)
        with client.websocket_connect(
            f"/api/v1/media/sessions/{media_id}/speech"
        ) as ws:
            for _ in range(6):
                ws._connection.send(LOUD)
            first = _collect_until(ws, {"response.started"})
            assert any(e["type"] == "response.started" for e in first), first
            ws.send_json({"type": "interrupt", "reason": "barge_in"})
            rest = _collect_until(ws, {"speech.interrupted"})
            types = [event["type"] for event in rest]
            assert "speech.interrupted" in types
            assert "response.completed" not in types
            interrupt = next(e for e in rest if e["type"] == "speech.interrupted")
            assert interrupt["data"]["reason"] == "barge_in"
            transport = engine.transport(media_id)
            assert transport is not None
            assert transport.clear_output_calls >= 1


# =====================================================================
# errors: unknown -> 404, closed -> 409
# =====================================================================
def test_ws_unknown_media_session_is_404(settings_factory) -> None:
    with speech_server(settings_factory()) as (client, _app):
        with pytest.raises(InvalidStatus) as exc:
            with client.websocket_connect(
                "/api/v1/media/sessions/media_missing/speech"
            ):
                pass
        assert exc.value.response.status_code == 404
        body = json.loads(exc.value.response.body)
        assert body["error"]["code"] == "media_session_not_found"


def test_ws_closed_media_session_is_409(settings_factory) -> None:
    with speech_server(settings_factory()) as (client, _app):
        media_id = _create_call(client)
        deleted = client.delete(f"/api/v1/media/sessions/{media_id}")
        assert deleted.status_code == 204, deleted.text
        with pytest.raises(InvalidStatus) as exc:
            with client.websocket_connect(
                f"/api/v1/media/sessions/{media_id}/speech"
            ):
                pass
        assert exc.value.response.status_code == 409
        body = json.loads(exc.value.response.body)
        assert body["error"]["code"] == "media_session_closed"


# =====================================================================
# teardown: WS close / media DELETE / server shutdown release the session
# =====================================================================
def test_ws_close_releases_speech_session(settings_factory) -> None:
    engine = SimulatedSpeechEngine()
    with speech_server(settings_factory(), engine=engine) as (client, app):
        media_id = _create_call(client)
        with client.websocket_connect(
            f"/api/v1/media/sessions/{media_id}/speech"
        ) as ws:
            ws._connection.send(LOUD)
            _collect_until(ws, {"speech.started"})
        assert _wait_until(lambda: engine.active_session_count == 0)
        assert app.state.speech.active_session_ids == set()


def test_media_delete_releases_speech_session(settings_factory) -> None:
    engine = SimulatedSpeechEngine()
    with speech_server(settings_factory(), engine=engine) as (client, app):
        media_id = _create_call(client)
        with client.websocket_connect(
            f"/api/v1/media/sessions/{media_id}/speech"
        ) as ws:
            ws._connection.send(LOUD)
            _collect_until(ws, {"speech.started"})
            deleted = client.delete(f"/api/v1/media/sessions/{media_id}")
            assert deleted.status_code == 204, deleted.text
            # DELETE awaits the media close listener, so release is synchronous.
            assert engine.active_session_count == 0
            assert app.state.speech.active_session_ids == set()


def test_server_shutdown_releases_speech_sessions(settings_factory) -> None:
    engine = SimulatedSpeechEngine()
    with speech_server(settings_factory(), engine=engine) as (client, app):
        media_id = _create_call(client)
        with client.websocket_connect(
            f"/api/v1/media/sessions/{media_id}/speech"
        ) as ws:
            ws._connection.send(LOUD)
            _collect_until(ws, {"speech.started"})
        assert _wait_until(lambda: engine.active_session_count == 0)
        assert app.state.speech.active_session_ids == set()
    assert engine.active_session_count == 0


# =====================================================================
# isolation: a broken speech engine cannot break core features
# =====================================================================
class ExplodingSpeechEngine:
    """Every method raises: the speech layer must degrade, not propagate."""

    async def start(self, media_session_id: str, *, session_id: str | None = None) -> None:
        raise RuntimeError("speech engine exploded")

    async def feed_audio(self, media_session_id: str, audio: object) -> None:
        raise RuntimeError("speech engine exploded")

    async def interrupt(self, media_session_id: str, *, reason: str = "barge_in") -> None:
        raise RuntimeError("speech engine exploded")

    async def stop(self, media_session_id: str, *, reason: str = "closed") -> None:
        raise RuntimeError("speech engine exploded")

    def events(self, media_session_id: str):  # noqa: ANN201 - test double
        raise RuntimeError("speech engine exploded")

    async def close(self) -> None:
        raise RuntimeError("speech engine exploded")


def test_speech_failure_is_isolated_from_core_features(settings_factory) -> None:
    api = Api()
    with speech_server(
        settings_factory(), engine=ExplodingSpeechEngine()
    ) as (client, _app):
        # health
        assert client.get("/api/v1/health").status_code == 200
        # session + full run
        session = api.create_session(client, title="isolation")
        run = api.start_run(client, session["session_id"], "hello").json()
        terminal = api.wait_terminal(client, run["run_id"])
        assert terminal["status"] == "completed"
        # artifact upload
        uploaded = client.post(
            "/api/v1/artifacts",
            files={"file": ("a.txt", b"hello artifact", "text/plain")},
        )
        assert uploaded.status_code == 201, uploaded.text
        artifact_id = uploaded.json()["artifact_id"]
        assert (
            client.get(f"/api/v1/artifacts/{artifact_id}").status_code == 200
        )
        # the speech channel degrades to a registered Product error
        media_id = _create_call(client)
        with pytest.raises(InvalidStatus) as exc:
            with client.websocket_connect(
                f"/api/v1/media/sessions/{media_id}/speech"
            ):
                pass
        assert exc.value.response.status_code == 500
        body = json.loads(exc.value.response.body)
        assert body["error"]["code"] == "internal_error"


def test_speech_events_stay_off_the_agent_stream(settings_factory) -> None:
    api = Api()
    # A slow model keeps the run stream observable while we subscribe.
    slow = FakeModel([Turn(text="x" * 80, chunks=20, delay=0.03)])
    with speech_server(settings_factory(), model=slow) as (client, _app):
        session = api.create_session(client)
        media_id = _create_call(client, session_id=session["session_id"])
        run = api.start_run(client, session["session_id"], "hello").json()
        events = api.collect_events(client, run["run_id"])
        assert events
        assert not any(
            str(event.get("type", "")).startswith("speech.") for event in events
        )
        with client.websocket_connect(
            f"/api/v1/media/sessions/{media_id}/speech"
        ) as ws:
            ws._connection.send(LOUD)
            speech_events = _collect_until(ws, {"speech.started"})
        assert speech_events
        assert all(
            event["type"].startswith("speech.")
            or event["type"].startswith(("transcript.", "response."))
            for event in speech_events
        )
