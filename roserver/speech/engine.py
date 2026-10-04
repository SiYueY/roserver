"""Speech engine boundary and a deterministic simulated engine (docs §5.7).

This mirrors the established ``RobotBackend`` / ``MediaEngine`` pattern: roserver
defines one narrow, injectable boundary and ships a single offline
implementation.  The boundary is *real*; only the ASR / TTS / agent providers
behind it are simulated today.

    SpeechEngine            抽象接口（今天只有模拟实现）
    SimulatedSpeechEngine   离线、确定性、无 DashScope、无网络

The simulated engine does **not** reimplement the speech pipeline.  It drives
roboagent's real :class:`~roboagent.speech.session.SpeechSession` through the
public ``create_speech_session`` injection point (``asr`` / ``tts`` / ``vad`` /
``audio_processor``), so the emitted event sequence is produced by roboagent's
actual turn detection, endpointing and orchestration code:

    client PCM -> SpeechTransport.receive_audio
               -> SpeechSession (VAD / turn / ASR / agent / TTS)
               -> SpeechTransport.send_event  (SpeechEvent*)
               -> SpeechTransport.send_audio  (rendered PCM16)

What is real vs simulated behind this engine:

    真实    roboagent SpeechSession、TurnDetector、InterruptionDetector、
            TextSegmenter、EnergyVAD、PassthroughAudioProcessor、事件序列
    模拟    语音识别（固定 transcript）、合成（确定性 PCM 帧）、
            agent 回合（确定性 response.delta）

Future real integrations implement :class:`SpeechEngine` with DashScope
ASR/TTS and the roboagent agent ``Session`` bound to ``media_session_id``'s
``session_id``; the Product API above it does not change.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from typing import Any, Protocol, cast, runtime_checkable

from roboagent.message import FrozenJsonObject, UserMessage
from roboagent.runtime import AgentEvent
from roboagent.speech.audio import EnergyVAD, PassthroughAudioProcessor
from roboagent.speech.config import InterruptionConfig, SpeechConfig, TurnConfig
from roboagent.speech.event import SpeechEvent
from roboagent.speech.factory import create_speech_session
from roboagent.speech.types import (
    DEFAULT_INPUT_FORMAT,
    DEFAULT_OUTPUT_FORMAT,
    AudioChunk,
    AudioFormat,
    Transcript,
)

from ..errors import ProductError

logger = logging.getLogger("roserver")

# Sentinel pushed through the transport's audio queue to end ``receive_audio``.
_STOP = object()

# Graceful teardown budget: a synthetic session must never hang shutdown.
_STOP_TIMEOUT_SECONDS = 5.0


# ---------------------------------------------------------------------
# speech-engine errors -> Product error codes
# ---------------------------------------------------------------------
class SpeechEngineError(Exception):
    """Base class for speech-engine failures carrying a Product error code."""

    code = "internal_error"

    def __init__(self, message: str) -> None:
        self.message = message
        super().__init__(message)


def to_product_error(exc: SpeechEngineError) -> ProductError:
    """Map a speech-engine failure onto the Product error envelope."""
    return ProductError(exc.code, exc.message)


# ---------------------------------------------------------------------
# speech engine boundary (future real implementation: DashScope + agent)
# ---------------------------------------------------------------------
@runtime_checkable
class SpeechEngine(Protocol):
    """Per-media-session speech bridge boundary.

    ``start`` opens one speech session for a media session; ``feed_audio``
    pushes decoded client PCM into it; ``interrupt`` performs barge-in;
    ``stop`` tears it down.  ``events`` is a long-lived per-session async
    iterator of roboagent :data:`~roboagent.speech.event.SpeechEvent` objects
    that the service projects into the Product envelope.

    ``close()`` is the server-shutdown hook and releases every session.
    """

    async def start(
        self, media_session_id: str, *, session_id: str | None = None
    ) -> None: ...

    async def feed_audio(
        self, media_session_id: str, audio: AudioChunk
    ) -> None: ...

    async def interrupt(
        self, media_session_id: str, *, reason: str = "barge_in"
    ) -> None: ...

    async def stop(
        self, media_session_id: str, *, reason: str = "closed"
    ) -> None: ...

    def events(self, media_session_id: str) -> AsyncIterator[SpeechEvent]: ...

    async def close(self) -> None: ...


# ---------------------------------------------------------------------
# simulated providers (no DashScope, no network, deterministic)
# ---------------------------------------------------------------------
class _SimulatedRun:
    """Minimal roboagent ``Run`` stand-in used by the simulated agent."""

    def __init__(self, deltas: tuple[str, ...], delay: float = 0.0) -> None:
        self._deltas = deltas
        self._delay = delay
        self._cancelled = False

    def subscribe(self) -> AsyncIterator[AgentEvent]:
        async def _iterate() -> AsyncIterator[AgentEvent]:
            for index, delta in enumerate(self._deltas):
                if self._cancelled:
                    return
                if self._delay:
                    # Give other tasks (barge-in) a deterministic window.
                    await asyncio.sleep(self._delay)
                yield AgentEvent(
                    run_id="run_simulated",
                    sequence=index + 1,
                    type="model.delta",
                    payload=FrozenJsonObject({"text": delta}),
                )

        return _iterate()

    async def result(self) -> None:
        return None

    def cancel(self) -> None:
        self._cancelled = True


class _SimulatedAgentSession:
    """Text ``Session`` stand-in: one deterministic assistant reply per turn."""

    def __init__(self, deltas: tuple[str, ...], delay: float = 0.0) -> None:
        self._deltas = deltas
        self._delay = delay
        self.started = 0

    def start(self, message: UserMessage) -> _SimulatedRun:
        self.started += 1
        return _SimulatedRun(self._deltas, self._delay)


class _SimulatedASRSession:
    """Emits one partial and one final transcript once the turn is committed."""

    persistent = False

    def __init__(self, partial: str, final: str) -> None:
        self._partial = partial
        self._final = final
        self._committed = asyncio.Event()
        self._closed = False
        self.bytes_written = 0

    async def start(self) -> None:
        return None

    async def write(self, audio: AudioChunk) -> None:
        self.bytes_written += len(audio.data)

    async def commit(self) -> None:
        self._committed.set()

    async def close(self) -> None:
        self._closed = True
        # Unblock any concurrent ``events()`` consumer so teardown never
        # leaves a pending task behind.
        self._committed.set()

    async def events(self) -> AsyncIterator[Transcript]:
        await self._committed.wait()
        if self._closed:
            return
        yield Transcript(self._partial, False)
        yield Transcript(self._final, True)


class _SimulatedASR:
    def __init__(self, partial: str, final: str) -> None:
        self._partial = partial
        self._final = final

    def create_session(self) -> _SimulatedASRSession:
        return _SimulatedASRSession(self._partial, self._final)


class _SimulatedTTS:
    """Deterministic PCM16 renderer: one silent frame per response chunk."""

    def __init__(self, *, frame_ms: int = 20, max_frames: int = 3) -> None:
        self._max_frames = max_frames
        samples = DEFAULT_OUTPUT_FORMAT.sample_rate * frame_ms // 1000
        self._frame = bytes(2) * samples

    def synthesize(self, text: str) -> AsyncIterator[AudioChunk]:
        async def _iterate() -> AsyncIterator[AudioChunk]:
            frames = min(self._max_frames, max(1, len(text)))
            for _ in range(frames):
                yield AudioChunk(self._frame, DEFAULT_OUTPUT_FORMAT)

        return _iterate()

    async def cancel(self) -> None:
        return None

    async def close(self) -> None:
        return None


class _ObservingAudioProcessor(PassthroughAudioProcessor):
    """Passthrough DSP that records the rendered PCM it observes (docs §5.7).

    ``create_speech_session`` wires ``transport.set_render_observer`` to the
    audio processor's ``observe_render`` when both exist.  The real WebRTC
    processor uses that hook to feed its far-end reference; the simulator stays
    a true passthrough, but records the frames so the bridge is observable
    rather than silently dropped.
    """

    def __init__(self) -> None:
        self.observed_render: list[AudioChunk] = []

    def observe_render(self, audio: AudioChunk) -> None:
        self.observed_render.append(audio)


class _SimulatedTransport:
    """Implements roboagent's :class:`SpeechTransport` against in-memory queues."""

    def __init__(
        self, *, max_audio_frames: int = 32, event_queue_bound: int = 256
    ) -> None:
        self._audio: asyncio.Queue[AudioChunk | object] = asyncio.Queue(
            maxsize=max_audio_frames
        )
        self._events: asyncio.Queue[SpeechEvent | object] = asyncio.Queue(
            maxsize=max(1, event_queue_bound)
        )
        self.closed = False
        self.clear_output_calls = 0
        self.rendered_frames: list[AudioChunk] = []
        self.playback_buffer: list[AudioChunk] = []
        # ``create_speech_session`` sets this when the audio processor exposes
        # ``observe_render`` (docs §5.7).
        self.render_observer: Any = None

    # -- capture side -------------------------------------------------
    def feed(self, audio: AudioChunk) -> None:
        if self._audio.full():
            # Prefer recent audio; this mirrors SpeechSession's own bounded
            # queue and keeps feed_audio from blocking the WS reader.
            try:
                self._audio.get_nowait()
            except asyncio.QueueEmpty:  # pragma: no cover - defensive
                pass
        self._audio.put_nowait(audio)

    def stop_capture(self) -> None:
        # Teardown discards queued capture so the sentinel is always delivered
        # even when a burst filled the bounded queue.
        while self._audio.full():
            try:
                self._audio.get_nowait()
            except asyncio.QueueEmpty:  # pragma: no cover - defensive
                break
        self._audio.put_nowait(_STOP)

    async def receive_audio(self) -> AsyncIterator[AudioChunk]:
        while True:
            item = await self._audio.get()
            if item is _STOP:
                return
            yield cast(AudioChunk, item)

    # -- render / event side ------------------------------------------
    def set_render_observer(self, observer: Any) -> None:
        """Receive PCM at the point it is handed to the outgoing track."""
        self.render_observer = observer

    async def send_audio(self, audio: AudioChunk) -> None:
        self.rendered_frames.append(audio)
        self.playback_buffer.append(audio)
        observer = self.render_observer
        if observer is not None:
            outcome = observer(audio)
            if asyncio.iscoroutine(outcome):
                await outcome

    async def send_event(self, event: SpeechEvent) -> None:
        if self._events.full():
            # The Product reader stalls -> keep the newest state; a dropped
            # caption is recoverable, blocking the speech pipeline is not.
            try:
                self._events.get_nowait()
            except asyncio.QueueEmpty:  # pragma: no cover - defensive
                pass
        self._events.put_nowait(event)

    async def clear_output(self) -> None:
        self.clear_output_calls += 1
        self.playback_buffer.clear()

    async def close(self) -> None:
        self.closed = True
        # Unblock receive_audio; send_event is left open so SpeechSession can
        # still flush InterruptedEvent during close().
        if self._audio.empty():
            self._audio.put_nowait(_STOP)

    async def events(self) -> AsyncIterator[SpeechEvent]:
        while True:
            item = await self._events.get()
            if item is _STOP:
                return
            yield cast(SpeechEvent, item)

    def drain_events(self) -> None:
        """Let a consumer's ``events`` iterator finish without blocking close."""
        self._events.put_nowait(_STOP)


class _SimulatedSession:
    __slots__ = ("media_session_id", "session_id", "transport", "speech", "task")

    def __init__(
        self,
        *,
        media_session_id: str,
        session_id: str | None,
        transport: _SimulatedTransport,
        speech: Any,
        task: asyncio.Task[None],
    ) -> None:
        self.media_session_id = media_session_id
        self.session_id = session_id
        self.transport = transport
        self.speech = speech
        self.task = task


# ---------------------------------------------------------------------
# deterministic simulator
# ---------------------------------------------------------------------
class SimulatedSpeechEngine:
    """Offline, in-memory :class:`SpeechEngine` used by tests and dev.

    Every method is deterministic and allocates nothing outside the event loop.
    The engine drives roboagent's real ``SpeechSession`` with simulated ASR /
    TTS / agent providers, so the event sequence is the one the real pipeline
    documents.  ``framing`` and transcript text are configurable for tests.
    """

    def __init__(
        self,
        *,
        transcript: str = "hello robot",
        partial: str = "hello",
        response_deltas: tuple[str, ...] = ("Hello", " from", " the", " robot"),
        response_delay: float = 0.0,
        max_audio_frames: int = 32,
        event_queue_bound: int = 256,
        capture_format: AudioFormat = DEFAULT_INPUT_FORMAT,
        render_format: AudioFormat = DEFAULT_OUTPUT_FORMAT,
        config: SpeechConfig | None = None,
    ) -> None:
        self.transcript = transcript
        self.partial = partial
        self.response_deltas = response_deltas
        self.response_delay = response_delay
        self.max_audio_frames = max_audio_frames
        self.event_queue_bound = event_queue_bound
        self.capture_format = capture_format
        self.render_format = render_format
        self.config = config or _default_config()
        self._sessions: dict[str, _SimulatedSession] = {}
        self._closed = False

    # -- test/dev control surface -------------------------------------
    @property
    def active_session_ids(self) -> set[str]:
        return set(self._sessions)

    @property
    def active_session_count(self) -> int:
        return len(self._sessions)

    def transport(self, media_session_id: str) -> _SimulatedTransport | None:
        session = self._sessions.get(media_session_id)
        return None if session is None else session.transport

    def render_observations(self, media_session_id: str) -> list[AudioChunk]:
        """Rendered PCM observed at the transport's render hook (docs §5.7)."""
        session = self._sessions.get(media_session_id)
        if session is None:
            return []
        processor = getattr(session.speech, "audio_processor", None)
        return list(getattr(processor, "observed_render", ()))

    # -- SpeechEngine --------------------------------------------------
    async def start(
        self, media_session_id: str, *, session_id: str | None = None
    ) -> None:
        if self._closed:
            raise SpeechEngineError("Speech engine is closed.")
        if media_session_id in self._sessions:
            return
        transport = _SimulatedTransport(
            max_audio_frames=self.max_audio_frames,
            event_queue_bound=self.event_queue_bound,
        )
        speech = create_speech_session(
            session=_SimulatedAgentSession(self.response_deltas, self.response_delay),
            transport=transport,
            config=self.config,
            capture_format=self.capture_format,
            render_format=self.render_format,
            asr=_SimulatedASR(self.partial, self.transcript),
            tts=_SimulatedTTS(),
            vad=EnergyVAD(threshold=0.02, calibration_frames=0),
            audio_processor=_ObservingAudioProcessor(),
        )
        task = asyncio.create_task(speech.run())
        self._sessions[media_session_id] = _SimulatedSession(
            media_session_id=media_session_id,
            session_id=session_id,
            transport=transport,
            speech=speech,
            task=task,
        )

    async def feed_audio(self, media_session_id: str, audio: AudioChunk) -> None:
        session = self._require(media_session_id)
        session.transport.feed(audio)

    async def interrupt(
        self, media_session_id: str, *, reason: str = "barge_in"
    ) -> None:
        session = self._require(media_session_id)
        await session.speech.interrupt(reason)

    async def stop(self, media_session_id: str, *, reason: str = "closed") -> None:
        session = self._sessions.pop(media_session_id, None)
        if session is None:
            return
        # Graceful: end the capture iterator so SpeechSession flushes the
        # current turn and runs its own ``close()`` (interrupt + ASR/TTS/DSP
        # release).  Cancel only if that does not settle in time.
        session.transport.stop_capture()
        task = session.task
        try:
            if task is not None and not task.done():
                try:
                    await asyncio.wait_for(
                        asyncio.shield(task), _STOP_TIMEOUT_SECONDS
                    )
                except TimeoutError:  # pragma: no cover - defensive teardown
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)
                except asyncio.CancelledError:
                    task.cancel()
                    raise
        finally:
            # Ending the event iterator lets the Product reader finish and the
            # websocket teardown proceed instead of leaving a pending task.
            session.transport.drain_events()

    async def close(self) -> None:
        self._closed = True
        for media_session_id in list(self._sessions):
            try:
                await self.stop(media_session_id, reason="server_shutdown")
            except Exception:  # pragma: no cover - defensive isolation
                logger.exception(
                    "speech engine failed to release session %s", media_session_id
                )
        self._sessions.clear()

    def events(self, media_session_id: str) -> AsyncIterator[SpeechEvent]:
        session = self._sessions.get(media_session_id)
        if session is None:
            return _empty_events()
        return session.transport.events()

    # -- internals -----------------------------------------------------
    def _require(self, media_session_id: str) -> _SimulatedSession:
        session = self._sessions.get(media_session_id)
        if session is None:
            raise SpeechEngineError(
                f"Speech session {media_session_id!r} does not exist."
            )
        return session


async def _empty_events() -> AsyncIterator[SpeechEvent]:
    return
    yield  # pragma: no cover - makes this an async generator


def _default_config() -> SpeechConfig:
    """Fast, deterministic turn parameters for the simulator.

    ``min_speech_ms=0`` keeps a synthetic burst valid even when frames are
    processed instantly; ``idle_timeout_ms`` completes the turn once the
    synthetic utterance stops arriving.  Interruption is driven explicitly by
    ``interrupt()`` instead of automatically from synthetic energy.
    """
    return SpeechConfig(
        turn=TurnConfig(
            silence_ms=120,
            max_duration_ms=10_000,
            idle_timeout_ms=150,
            min_speech_ms=0,
            interruption=InterruptionConfig(enabled=False),
        ),
        diagnostics=False,
    )


__all__ = [
    "SpeechEngine",
    "SpeechEngineError",
    "SimulatedSpeechEngine",
    "to_product_error",
]
