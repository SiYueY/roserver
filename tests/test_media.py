"""Phase 5A media / WebRTC signaling infrastructure (docs §5.4-§5.6, Phase 5A).

The project does not have a real WebRTC stack yet, so every test injects
:class:`SimulatedMediaEngine` through ``create_app(media_engine=...)``.  The
Product API and lifecycle are real; no test touches the network.
"""

from __future__ import annotations

import asyncio
import time
from contextlib import contextmanager

from conftest import FakeModel, Turn

from roserver.media.engine import (
    MediaEngineError,
    MediaNegotiationError,
    SimulatedMediaEngine,
)
from roserver.media.service import MediaService

MEDIA_FIELDS = {
    "media_session_id",
    "kind",
    "session_id",
    "state",
    "video_source",
    "audio",
    "video",
    "created_at",
    "expires_at",
}
TERMINAL_FIELDS = MEDIA_FIELDS | {"close_reason"}

OFFER = {
    "type": "offer",
    "sdp": "v=0\r\no=- 0 0 IN IP4 127.0.0.1\r\ns=-\r\nt=0 0\r\nm=video 9 UDP/TLS/RTP/SAVPF 96\r\n",
}


@contextmanager
def media_app(client_factory, settings_factory, engine=None, **overrides):
    base = {"media_session_ttl": 60.0, "media_expiry_tick": 0.01}
    base.update(overrides)
    settings = settings_factory(**base)
    with client_factory(
        settings,
        model=FakeModel([Turn(text="ok")]),
        media_engine=engine,
    ) as client:
        yield client


def create(client, **body):
    payload = {"kind": "camera", "session_id": None, "audio": False, "video": True}
    payload.update(body)
    response = client.post("/api/v1/media/sessions", json=payload)
    assert response.status_code == 201, response.text
    return response.json()


def offer(client, media_session_id: str, **body):
    return client.post(
        f"/api/v1/media/sessions/{media_session_id}/offer", json={**OFFER, **body}
    )


# =====================================================================
# MediaSession object shape (the tested rodesk client parses these names)
# =====================================================================
def test_create_media_session_shape_and_engine_resources(
    client_factory, settings_factory
):
    engine = SimulatedMediaEngine()
    with media_app(client_factory, settings_factory, engine=engine) as client:
        session = create(
            client,
            kind="camera",
            video_source="robot_head",
            audio=False,
            video=True,
        )
        assert set(session) == MEDIA_FIELDS
        assert session["media_session_id"].startswith("media_")
        assert session["kind"] == "camera"
        assert session["session_id"] is None
        assert session["state"] == "created"
        assert session["video_source"] == "robot_head"
        assert session["audio"] is False
        assert session["video"] is True
        assert session["created_at"].endswith("Z")
        assert session["expires_at"].endswith("Z")
        assert session["expires_at"] > session["created_at"]
        assert engine.active_session_ids == {session["media_session_id"]}


def test_create_call_session_carries_agent_session(client_factory, settings_factory):
    with media_app(client_factory, settings_factory) as client:
        session = create(
            client,
            kind="call",
            session_id="sess_1",
            audio=True,
            video=True,
        )
        assert session["kind"] == "call"
        assert session["session_id"] == "sess_1"
        assert session["audio"] is True and session["video"] is True


def test_create_invalid_kind_is_400(client_factory, settings_factory):
    with media_app(client_factory, settings_factory) as client:
        response = client.post(
            "/api/v1/media/sessions",
            json={"kind": "telepathy", "audio": False, "video": False},
        )
        assert response.status_code == 400
        assert response.json()["error"]["code"] == "invalid_input"


# =====================================================================
# offer / answer and the state machine
# =====================================================================
def test_offer_returns_complete_answer_and_connects(client_factory, settings_factory):
    engine = SimulatedMediaEngine()
    with media_app(client_factory, settings_factory, engine=engine) as client:
        session = create(client)
        media_session_id = session["media_session_id"]

        fetched = client.get(f"/api/v1/media/sessions/{media_session_id}")
        assert fetched.status_code == 200
        assert fetched.json() == session

        answer = offer(client, media_session_id)
        assert answer.status_code == 200, answer.text
        body = answer.json()
        assert body["type"] == "answer"
        assert isinstance(body["sdp"], str) and body["sdp"]
        assert body["sdp"].startswith("v=0")

        connected = client.get(f"/api/v1/media/sessions/{media_session_id}").json()
        assert connected["state"] == "connected"
        assert connected["media_session_id"] == media_session_id
        assert engine.resource(media_session_id)["negotiated"] is True


class _BlockingEngine(SimulatedMediaEngine):
    """Simulator that parks inside handle_offer so ``negotiating`` is visible."""

    def __init__(self) -> None:
        super().__init__()
        self.gate = asyncio.Event()

    async def handle_offer(self, media_session_id: str, sdp: str) -> str:
        await self.gate.wait()
        return await super().handle_offer(media_session_id, sdp)


def test_state_machine_created_negotiating_connected(settings_factory):
    engine = _BlockingEngine()
    settings = settings_factory(media_session_ttl=60.0, media_expiry_tick=0.01)

    async def scenario() -> dict:
        service = MediaService(settings=settings, engine=engine)
        await service.startup()
        try:
            created = await service.create_session(
                kind="call", session_id="sess_1", audio=True, video=True
            )
            media_session_id = created["media_session_id"]
            assert created["state"] == "created"

            task = asyncio.create_task(
                service.handle_offer(media_session_id, type="offer", sdp=OFFER["sdp"])
            )
            current = created
            for _ in range(1000):
                current = await service.get_session(media_session_id)
                if current["state"] == "negotiating":
                    break
                await asyncio.sleep(0)
            assert current["state"] == "negotiating"

            engine.gate.set()
            answer = await task
            assert answer["type"] == "answer"

            final = await service.get_session(media_session_id)
            assert final["state"] == "connected"
            return final
        finally:
            await service.close()

    final = asyncio.run(scenario())
    assert final["state"] == "connected"
    assert engine.active_session_ids == set()


# =====================================================================
# termination: DELETE / repeat DELETE / closed session / unknown id
# =====================================================================
def test_delete_releases_resources_and_repeat_is_404(
    client_factory, settings_factory
):
    engine = SimulatedMediaEngine()
    with media_app(client_factory, settings_factory, engine=engine) as client:
        session = create(client)
        media_session_id = session["media_session_id"]
        assert offer(client, media_session_id).status_code == 200
        assert engine.active_session_ids == {media_session_id}

        deleted = client.delete(f"/api/v1/media/sessions/{media_session_id}")
        assert deleted.status_code == 204
        assert not deleted.content
        assert engine.active_session_ids == set()

        # The tombstone is still readable for reconnect / diagnostics.
        closed = client.get(f"/api/v1/media/sessions/{media_session_id}").json()
        assert set(closed) == TERMINAL_FIELDS
        assert closed["state"] == "closed"
        assert closed["close_reason"] is not None

        # rodesk tolerates 404 on a repeat delete; it must never be a 500.
        again = client.delete(f"/api/v1/media/sessions/{media_session_id}")
        assert again.status_code == 404
        assert again.json()["error"]["code"] == "media_session_not_found"


def test_offer_on_closed_session_is_409(client_factory, settings_factory):
    engine = SimulatedMediaEngine()
    with media_app(client_factory, settings_factory, engine=engine) as client:
        session = create(client)
        media_session_id = session["media_session_id"]
        client.delete(f"/api/v1/media/sessions/{media_session_id}")

        response = offer(client, media_session_id)
        assert response.status_code == 409
        assert response.json()["error"]["code"] == "media_session_closed"


def test_unknown_media_session_is_404(client_factory, settings_factory):
    with media_app(client_factory, settings_factory) as client:
        assert client.get("/api/v1/media/sessions/media_nope").status_code == 404
        missing = client.get("/api/v1/media/sessions/media_nope")
        assert missing.json()["error"]["code"] == "media_session_not_found"

        response = offer(client, "media_nope")
        assert response.status_code == 404
        assert response.json()["error"]["code"] == "media_session_not_found"

        deleted = client.delete("/api/v1/media/sessions/media_nope")
        assert deleted.status_code == 404
        assert deleted.json()["error"]["code"] == "media_session_not_found"


# =====================================================================
# malformed offer mapping
# =====================================================================
def test_malformed_offer_mapping(client_factory, settings_factory):
    engine = SimulatedMediaEngine()
    with media_app(
        client_factory, settings_factory, engine=engine, media_max_sdp_bytes=128
    ) as client:
        media_session_id = create(client)["media_session_id"]

        wrong_type = offer(client, media_session_id, type="answer")
        assert wrong_type.status_code == 422
        assert wrong_type.json()["error"]["code"] == "unsupported_content_type"

        missing_sdp = client.post(
            f"/api/v1/media/sessions/{media_session_id}/offer", json={"type": "offer"}
        )
        assert missing_sdp.status_code == 400
        assert missing_sdp.json()["error"]["code"] == "invalid_input"

        empty_sdp = offer(client, media_session_id, sdp="   ")
        assert empty_sdp.status_code == 400
        assert empty_sdp.json()["error"]["code"] == "invalid_input"

        oversized = offer(client, media_session_id, sdp="v=0\r\n" + "a" * 200)
        assert oversized.status_code == 413
        assert oversized.json()["error"]["code"] == "payload_too_large"

        # Validation never advances the FSM; the session is still usable.
        assert client.get(
            f"/api/v1/media/sessions/{media_session_id}"
        ).json()["state"] == "created"
        assert offer(client, media_session_id).status_code == 200


# =====================================================================
# failure mapping
# =====================================================================
def test_negotiation_failure_is_502_and_releases_resources(
    client_factory, settings_factory
):
    engine = SimulatedMediaEngine()
    with media_app(client_factory, settings_factory, engine=engine) as client:
        media_session_id = create(client)["media_session_id"]
        engine.fail_next("negotiation")

        response = offer(client, media_session_id)
        assert response.status_code == 502
        assert response.json()["error"]["code"] == "media_negotiation_failed"

        failed = client.get(f"/api/v1/media/sessions/{media_session_id}").json()
        assert failed["state"] == "failed"
        assert failed["close_reason"] is not None
        assert engine.active_session_ids == set()

        # A failed session is terminal, exactly like a closed one.
        again = offer(client, media_session_id)
        assert again.status_code == 409
        assert again.json()["error"]["code"] == "media_session_closed"


# =====================================================================
# expiry / shutdown release resources
# =====================================================================
def test_expiry_releases_engine_resources(client_factory, settings_factory):
    engine = SimulatedMediaEngine()
    with media_app(
        client_factory,
        settings_factory,
        engine=engine,
        media_session_ttl=0.1,
        media_expiry_tick=0.01,
    ) as client:
        media_session_id = create(client)["media_session_id"]
        assert offer(client, media_session_id).status_code == 200

        # The server-side sweeper releases resources without any request.
        deadline = time.time() + 3.0
        while engine.active_session_ids and time.time() < deadline:
            time.sleep(0.02)
        assert engine.active_session_ids == set()

        expired = client.get(f"/api/v1/media/sessions/{media_session_id}").json()
        assert expired["state"] in {"closed", "failed"}
        assert expired["close_reason"] is not None

        response = offer(client, media_session_id)
        assert response.status_code == 409
        assert response.json()["error"]["code"] == "media_session_closed"


def test_client_disconnect_releases_resources(settings_factory):
    engine = SimulatedMediaEngine()
    settings = settings_factory(media_session_ttl=60.0, media_expiry_tick=0.01)

    async def scenario() -> dict:
        service = MediaService(settings=settings, engine=engine)
        await service.startup()
        try:
            created = await service.create_session(
                kind="call", session_id="sess_1", audio=True, video=True
            )
            media_session_id = created["media_session_id"]
            await service.handle_offer(
                media_session_id, type="offer", sdp=OFFER["sdp"]
            )
            assert engine.active_session_ids == {media_session_id}

            await service.on_client_disconnect(media_session_id)
            assert engine.active_session_ids == set()
            closed = await service.get_session(media_session_id)
            assert closed["state"] == "closed"
            assert closed["close_reason"] == "client_disconnect"

            # A repeat disconnect is a no-op, never an error.
            await service.on_client_disconnect(media_session_id)
            return closed
        finally:
            await service.close()

    assert asyncio.run(scenario())["state"] == "closed"


def test_shutdown_releases_all_sessions(client_factory, settings_factory):
    engine = SimulatedMediaEngine()
    with media_app(client_factory, settings_factory, engine=engine) as client:
        first = create(client)["media_session_id"]
        second = create(client)["media_session_id"]
        assert offer(client, first).status_code == 200
        assert engine.active_session_ids == {first, second}

    # Leaving the live-server context ran the lifespan shutdown.
    assert engine.active_session_ids == set()


# =====================================================================
# isolation: a broken media engine must not affect core features
# =====================================================================
class BrokenMediaEngine:
    """Media engine that cannot serve anything (future real WebRTC未就绪)."""

    async def create_session(self, media_session_id: str, **kwargs):
        raise MediaNegotiationError("media engine is not available")

    async def handle_offer(self, media_session_id: str, sdp: str) -> str:
        raise MediaNegotiationError("media engine is not available")

    async def close_session(self, media_session_id: str, reason: str) -> None:
        raise MediaNegotiationError("media engine is not available")

    async def close(self) -> None:
        raise MediaEngineError("media engine is not available")


def test_broken_media_engine_does_not_break_core_features(
    client_factory, settings_factory, api
):
    """媒体层不可用时，agent/session/run/artifact/health 必须照常工作。"""
    with media_app(
        client_factory, settings_factory, engine=BrokenMediaEngine()
    ) as client:
        # core: health
        assert client.get("/api/v1/health").status_code == 200

        # core: session + run + projection
        session = api.create_session(client)
        run = api.start_run(client, session["session_id"], "media is broken")
        assert run.status_code == 201, run.text
        info = api.wait_terminal(client, run.json()["run_id"])
        assert info["status"] == "completed", info
        snapshot = client.get(f"/api/v1/sessions/{session['session_id']}").json()
        assert snapshot["messages"], snapshot

        # core: artifacts
        uploaded = client.post(
            "/api/v1/artifacts",
            files={"file": ("a.png", b"\x89PNG\r\n\x1a\nfake", "image/png")},
        )
        assert uploaded.status_code == 201, uploaded.text
        artifact_id = uploaded.json()["artifact_id"]
        assert (
            client.get(f"/api/v1/artifacts/{artifact_id}").status_code == 200
        )
        assert (
            client.get(f"/api/v1/artifacts/{artifact_id}/content").content
            == b"\x89PNG\r\n\x1a\nfake"
        )

        # media endpoints degrade to a registered Product error, not a crash
        created = client.post(
            "/api/v1/media/sessions",
            json={"kind": "camera", "audio": False, "video": True},
        )
        assert created.status_code == 502
        assert created.json()["error"]["code"] == "media_negotiation_failed"

        missing = client.get("/api/v1/media/sessions/media_nope")
        assert missing.status_code == 404
        assert missing.json()["error"]["code"] == "media_session_not_found"

        deleted = client.delete("/api/v1/media/sessions/media_nope")
        assert deleted.status_code == 404
        assert deleted.json()["error"]["code"] == "media_session_not_found"


def test_media_startup_failure_is_isolated(
    client_factory, settings_factory, monkeypatch
):
    async def boom(self) -> None:
        raise RuntimeError("media startup exploded")

    monkeypatch.setattr(MediaService, "startup", boom)
    with media_app(client_factory, settings_factory) as client:
        assert client.get("/api/v1/health").status_code == 200
        session = client.post(
            "/api/v1/media/sessions",
            json={"kind": "camera", "audio": False, "video": True},
        )
        assert session.status_code == 201, session.text


def test_media_shutdown_failure_is_isolated(
    client_factory, settings_factory, monkeypatch
):
    async def boom(self) -> None:
        raise RuntimeError("media shutdown exploded")

    monkeypatch.setattr(MediaService, "close", boom)
    with media_app(client_factory, settings_factory) as client:
        assert client.get("/api/v1/health").status_code == 200
