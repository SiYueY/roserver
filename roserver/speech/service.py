"""SpeechService: the Product speech bridge for one media session (docs §5.7).

A ``MediaSession`` (docs §5.5) is the Product handle a Call owns.  This service
opens one roboagent speech session per media session through an injectable
:class:`~.engine.SpeechEngine`, feeds it decoded client PCM, forwards barge-in,
and projects roboagent ``SpeechEvent`` objects into the Product speech event
envelope (docs §5.7 / §7.11).

Design constraints (mirrors the media layer):

* nothing is emitted into the agent session's event stream; speech events live
  on the speech channel only (docs §5.7);
* every terminal path releases the engine-side bridge and leaves no task
  behind: client WS close, media-session DELETE / expiry / negotiation failure,
  and server shutdown;
* a speech failure is mapped onto the closed Product error registry and is
  isolated from health / session / run / artifact.
"""

from __future__ import annotations

import logging
import time
from typing import Any, AsyncIterator, Callable

from roboagent.speech.event import (
    InterruptedEvent,
    ResponseTextEvent,
    SpeechErrorEvent,
    SpeechEvent,
    TranscriptFinalEvent,
    TranscriptPartialEvent,
)

from ..agent.schema import rfc3339
from ..config import Settings
from ..errors import ProductError
from .engine import SpeechEngine, SpeechEngineError, to_product_error

logger = logging.getLogger("roserver")

CLOSE_BY_CLIENT = "client_disconnect"
CLOSE_BY_DELETE = "closed_by_client"
CLOSE_BY_SHUTDOWN = "server_shutdown"

#: Active media-session states that may host a speech bridge.
SPEECHABLE_STATES = frozenset({"created", "negotiating", "connected"})
#: Terminal media-session states map to ``409 media_session_closed``.
TERMINAL_STATES = frozenset({"closed", "failed"})

# roboagent SpeechEvent type -> Product speech event type.  One Product type
# per roboagent type; unmapped low-level events stay internal (docs §5.7).
_PRODUCT_TYPES: dict[str, str] = {
    "speech.started": "speech.started",
    "speech.stopped": "speech.stopped",
    "transcript.partial": "transcript.partial",
    "transcript.final": "transcript.final",
    "response.started": "response.started",
    "response.delta": "response.delta",
    "response.completed": "response.completed",
    "interrupted": "speech.interrupted",
    "error": "speech.error",
}


class SpeechBridge:
    """Per-connection handle: media session id, bound agent session, sequence."""

    __slots__ = ("media_session_id", "session_id", "_sequence")

    def __init__(self, media_session_id: str, session_id: str | None) -> None:
        self.media_session_id = media_session_id
        self.session_id = session_id
        self._sequence = 0

    def next_sequence(self) -> int:
        self._sequence += 1
        return self._sequence


def speech_event_object(
    bridge: SpeechBridge, product_type: str, data: dict[str, Any], timestamp: float
) -> dict[str, Any]:
    """The Product speech event envelope (snake_case, docs §5.7)."""
    return {
        "media_session_id": bridge.media_session_id,
        "session_id": bridge.session_id,
        "sequence": bridge.next_sequence(),
        "type": product_type,
        "timestamp": rfc3339(timestamp) or "",
        "data": data,
    }


class SpeechService:
    """Product-facing speech bridge over a :class:`SpeechEngine`."""

    def __init__(
        self,
        *,
        settings: Settings,
        engine: SpeechEngine,
        media: Any,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.settings = settings
        self.engine = engine
        self.media = media
        self._clock = clock
        self._bridges: dict[str, SpeechBridge] = {}

    # =================================================================
    # lifecycle
    # =================================================================
    async def startup(self) -> None:
        """Subscribe to media-session teardown so DELETE releases the bridge."""
        register = getattr(self.media, "add_close_listener", None)
        if register is not None:
            register(self._on_media_closed)

    async def close(self) -> None:
        """Release every bridge and the engine (server shutdown)."""
        for media_session_id in list(self._bridges):
            await self.end(media_session_id, reason=CLOSE_BY_SHUTDOWN)
        try:
            await self.engine.close()
        except Exception:  # pragma: no cover - defensive isolation
            logger.exception("speech engine failed to shut down cleanly")

    async def _on_media_closed(self, media_session_id: str, reason: str) -> None:
        await self.end(media_session_id, reason=reason)

    # =================================================================
    # bridge lifecycle
    # =================================================================
    async def begin(self, media_session_id: str) -> SpeechBridge:
        """Validate the media session and open a speech bridge.

        Raises ``404 media_session_not_found`` for an unknown session and
        ``409 media_session_closed`` for a terminal one (docs §5.5).
        """
        body = await self.media.get_session(media_session_id)
        state = str(body.get("state", ""))
        if state in TERMINAL_STATES:
            raise ProductError(
                "media_session_closed",
                f"Media session {media_session_id!r} is {state}.",
            )
        if state not in SPEECHABLE_STATES:  # pragma: no cover - defensive
            raise ProductError(
                "media_session_closed",
                f"Media session {media_session_id!r} cannot host speech ({state}).",
            )
        # One bridge per media session; a reconnect replaces the old one.
        if media_session_id in self._bridges:
            await self.end(media_session_id, reason=CLOSE_BY_CLIENT)
        session_id = body.get("session_id")
        try:
            await self.engine.start(media_session_id, session_id=session_id)
        except SpeechEngineError as exc:
            raise to_product_error(exc) from exc
        except Exception as exc:  # pragma: no cover - defensive isolation
            logger.exception("speech engine failed to start a bridge")
            raise ProductError("internal_error", "Speech engine failed to start.") from exc
        bridge = SpeechBridge(media_session_id, session_id)
        self._bridges[media_session_id] = bridge
        return bridge

    async def end(self, media_session_id: str, *, reason: str = CLOSE_BY_CLIENT) -> None:
        """Idempotently release one bridge; teardown must never fail closed."""
        bridge = self._bridges.pop(media_session_id, None)
        if bridge is None:
            return
        try:
            await self.engine.stop(media_session_id, reason=reason)
        except Exception:  # pragma: no cover - teardown must not fail closed
            logger.exception(
                "speech engine failed to release session %s", media_session_id
            )

    # =================================================================
    # audio / control
    # =================================================================
    async def feed(self, bridge: SpeechBridge, audio: Any) -> None:
        self._require(bridge)
        try:
            await self.engine.feed_audio(bridge.media_session_id, audio)
        except SpeechEngineError as exc:
            raise to_product_error(exc) from exc
        except Exception as exc:  # pragma: no cover - defensive isolation
            logger.exception("speech engine failed to accept audio")
            raise ProductError("internal_error", "Speech engine failed.") from exc

    async def interrupt(
        self, bridge: SpeechBridge, *, reason: str = "barge_in"
    ) -> None:
        self._require(bridge)
        try:
            await self.engine.interrupt(bridge.media_session_id, reason=reason)
        except SpeechEngineError as exc:
            raise to_product_error(exc) from exc
        except Exception as exc:  # pragma: no cover - defensive isolation
            logger.exception("speech engine failed to interrupt")
            raise ProductError("internal_error", "Speech engine failed.") from exc

    # =================================================================
    # event projection
    # =================================================================
    async def events(self, bridge: SpeechBridge) -> AsyncIterator[SpeechEvent]:
        """Yield roboagent speech events for one live bridge."""
        if self._bridges.get(bridge.media_session_id) is not bridge:
            return
        iterator = self.engine.events(bridge.media_session_id)
        async for event in iterator:
            yield event

    def project(self, bridge: SpeechBridge, event: SpeechEvent) -> dict[str, Any] | None:
        """Project one roboagent event onto the Product envelope.

        Returns ``None`` for low-level events that have no Product projection.
        ``turn_id`` / ``response_id`` are always carried so a late event can be
        discarded by the client instead of overwriting a newer caption.
        """
        product_type = _PRODUCT_TYPES.get(event.type)
        if product_type is None:
            return None
        data: dict[str, Any] = {
            "turn_id": event.turn_id,
            "response_id": event.response_id,
        }
        if isinstance(event, (TranscriptPartialEvent, TranscriptFinalEvent)):
            data["text"] = event.text
        elif isinstance(event, ResponseTextEvent):
            data["delta"] = event.delta
        elif isinstance(event, InterruptedEvent):
            data["reason"] = event.reason
        elif isinstance(event, SpeechErrorEvent):
            data["message"] = event.error
        return speech_event_object(bridge, product_type, data, self._clock())

    def error_event(
        self, bridge: SpeechBridge, code: str, message: str
    ) -> dict[str, Any]:
        """A registered Product error delivered on the speech channel."""
        return speech_event_object(
            bridge,
            "speech.error",
            {"code": code, "message": message, "turn_id": None, "response_id": None},
            self._clock(),
        )

    # =================================================================
    # internals
    # =================================================================
    def _require(self, bridge: SpeechBridge) -> None:
        if self._bridges.get(bridge.media_session_id) is not bridge:
            raise ProductError(
                "media_session_closed",
                f"Speech bridge {bridge.media_session_id!r} is no longer active.",
            )

    @property
    def active_session_ids(self) -> set[str]:
        return set(self._bridges)


__all__ = [
    "CLOSE_BY_CLIENT",
    "CLOSE_BY_DELETE",
    "CLOSE_BY_SHUTDOWN",
    "SPEECHABLE_STATES",
    "TERMINAL_STATES",
    "SpeechBridge",
    "SpeechService",
    "speech_event_object",
]
