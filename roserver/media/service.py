"""MediaService: the WebRTC signaling session lifecycle (docs §5.5/§5.6).

The Product protocol is media-session-centric: create a session, exchange one
non-trickle SDP offer, tear it down.  This service owns the state machine

    created -> negotiating -> connected -> closed | failed

and guarantees that **every** terminal path releases the engine-side resources:
explicit DELETE, client disconnect, negotiation failure, ``expires_at`` and
server shutdown (docs §5.5「终结条件」).

Nothing here touches the agent / session / run / artifact core: the media layer
talks to an injectable :class:`~.engine.MediaEngine`, which today is the
deterministic simulator and later is a real WebRTC channel.
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from ..agent.schema import rfc3339
from ..config import Settings
from ..errors import ProductError
from .engine import (
    MEDIA_KINDS,
    MediaEngine,
    MediaEngineError,
    to_product_error,
)

logger = logging.getLogger("roserver")

STATE_CREATED = "created"
STATE_NEGOTIATING = "negotiating"
STATE_CONNECTED = "connected"
STATE_CLOSED = "closed"
STATE_FAILED = "failed"

TERMINAL_STATES = frozenset({STATE_CLOSED, STATE_FAILED})

CLOSE_BY_CLIENT = "client_disconnect"
CLOSE_BY_DELETE = "closed_by_client"
CLOSE_BY_EXPIRY = "expired"
CLOSE_BY_NEGOTIATION = "negotiation_failed"
CLOSE_BY_SHUTDOWN = "server_shutdown"


@dataclass(slots=True)
class _MediaSession:
    media_session_id: str
    kind: str
    session_id: str | None
    state: str
    video_source: str | None
    audio: bool
    video: bool
    created_at: float
    expires_at: float
    close_reason: str | None = None
    closed_at: float | None = None


def media_session_object(session: _MediaSession) -> dict[str, Any]:
    """Project a media session onto the §5.5 MediaSession object (snake_case).

    ``close_reason`` is only present once the session is ``closed``/``failed``
    (docs §5.5 field list).
    """
    body: dict[str, Any] = {
        "media_session_id": session.media_session_id,
        "kind": session.kind,
        "session_id": session.session_id,
        "state": session.state,
        "video_source": session.video_source,
        "audio": session.audio,
        "video": session.video,
        "created_at": rfc3339(session.created_at),
        "expires_at": rfc3339(session.expires_at),
    }
    if session.state in TERMINAL_STATES:
        body["close_reason"] = session.close_reason
    return body


class MediaService:
    """Product-facing media-session lifecycle over a :class:`MediaEngine`."""

    def __init__(
        self,
        *,
        settings: Settings,
        engine: MediaEngine,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.settings = settings
        self.engine = engine
        self._clock = clock
        self._sessions: dict[str, _MediaSession] = {}
        self._sweeper: asyncio.Task[None] | None = None
        # Phase 5B: layers that hold per-media-session resources (the speech
        # bridge) subscribe here so *every* terminal path releases them.
        self._close_listeners: list[Callable[[str, str], Awaitable[None]]] = []

    def add_close_listener(
        self, listener: Callable[[str, str], Awaitable[None]]
    ) -> None:
        """Register a best-effort ``(media_session_id, reason)`` teardown hook."""
        self._close_listeners.append(listener)

    # =================================================================
    # lifecycle
    # =================================================================
    async def startup(self) -> None:
        """Start the expiry sweeper.

        The sweeper is what releases engine resources when ``expires_at`` is
        reached without any further request, so expiry is not dependent on a
        client touching the session again.
        """
        if self._sweeper is None or self._sweeper.done():
            self._sweeper = asyncio.create_task(self._sweep_loop())

    async def close(self) -> None:
        """Release every session and the engine itself (server shutdown)."""
        task = self._sweeper
        self._sweeper = None
        if task is not None and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        for session in list(self._sessions.values()):
            await self._release(session, state=STATE_CLOSED, reason=CLOSE_BY_SHUTDOWN)
        try:
            await self.engine.close()
        except Exception:  # pragma: no cover - defensive isolation
            logger.exception("media engine failed to shut down cleanly")

    async def _sweep_loop(self) -> None:
        try:
            while True:
                await asyncio.sleep(self.settings.media_expiry_tick)
                await self._sweep_once()
        except asyncio.CancelledError:
            raise

    async def _sweep_once(self) -> None:
        now = self._clock()
        for session in list(self._sessions.values()):
            if session.state not in TERMINAL_STATES:
                if session.expires_at <= now:
                    await self._release(
                        session, state=STATE_FAILED, reason=CLOSE_BY_EXPIRY
                    )
                continue
            closed_at = session.closed_at or now
            if closed_at + self.settings.media_tombstone_ttl <= now:
                # Tombstone retention elapsed: drop the record so the process
                # does not accumulate closed sessions forever.
                self._sessions.pop(session.media_session_id, None)

    # =================================================================
    # session lifecycle (docs §5.5)
    # =================================================================
    async def create_session(
        self,
        *,
        kind: str,
        session_id: str | None = None,
        audio: bool = False,
        video: bool = False,
        video_source: str | None = None,
    ) -> dict[str, Any]:
        if kind not in MEDIA_KINDS:
            raise ProductError(
                "invalid_input", f"kind must be one of {sorted(MEDIA_KINDS)}."
            )
        media_session_id = f"media_{uuid.uuid4().hex}"
        now = self._clock()
        try:
            await self.engine.create_session(
                media_session_id,
                kind=kind,
                audio=audio,
                video=video,
                video_source=video_source,
            )
        except MediaEngineError as exc:
            raise to_product_error(exc) from exc
        session = _MediaSession(
            media_session_id=media_session_id,
            kind=kind,
            session_id=session_id,
            state=STATE_CREATED,
            video_source=video_source,
            audio=audio,
            video=video,
            created_at=now,
            expires_at=now + self.settings.media_session_ttl,
        )
        self._sessions[media_session_id] = session
        return media_session_object(session)

    async def get_session(self, media_session_id: str) -> dict[str, Any]:
        session = self._require(media_session_id)
        await self._expire_if_due(session)
        return media_session_object(session)

    async def handle_offer(
        self,
        media_session_id: str,
        *,
        type: str | None,
        sdp: str | None,
    ) -> dict[str, Any]:
        session = self._require(media_session_id)
        await self._expire_if_due(session)
        if session.state in TERMINAL_STATES:
            raise ProductError(
                "media_session_closed",
                f"Media session {media_session_id!r} is {session.state}.",
            )
        payload = self._validate_offer(type=type, sdp=sdp)

        session.state = STATE_NEGOTIATING
        try:
            answer_sdp = await self.engine.handle_offer(media_session_id, payload)
        except MediaEngineError as exc:
            await self._release(
                session, state=STATE_FAILED, reason=CLOSE_BY_NEGOTIATION
            )
            raise to_product_error(exc) from exc
        session.state = STATE_CONNECTED
        return {"type": "answer", "sdp": answer_sdp}

    async def close_session(
        self, media_session_id: str, *, reason: str = CLOSE_BY_DELETE
    ) -> bool:
        """Explicit DELETE.  Idempotent: a repeat is a 404, never a 500."""
        session = self._require(media_session_id)
        await self._expire_if_due(session)
        if session.state in TERMINAL_STATES:
            raise ProductError(
                "media_session_not_found",
                f"Media session {media_session_id!r} is no longer active.",
            )
        await self._release(session, state=STATE_CLOSED, reason=reason)
        return True

    async def on_client_disconnect(self, media_session_id: str) -> None:
        """Client / transport disconnect -> release resources (docs §5.5).

        Best effort: a missing or already-terminal session is a no-op.
        """
        session = self._sessions.get(media_session_id)
        if session is None or session.state in TERMINAL_STATES:
            return
        await self._release(session, state=STATE_CLOSED, reason=CLOSE_BY_CLIENT)

    # =================================================================
    # internals
    # =================================================================
    def _require(self, media_session_id: str) -> _MediaSession:
        session = self._sessions.get(media_session_id)
        if session is None:
            raise ProductError(
                "media_session_not_found",
                f"Media session {media_session_id!r} does not exist.",
            )
        return session

    async def _expire_if_due(self, session: _MediaSession) -> None:
        """Lazy expiry so a terminal state is observable without the sweeper."""
        if session.state in TERMINAL_STATES:
            return
        if session.expires_at <= self._clock():
            await self._release(session, state=STATE_FAILED, reason=CLOSE_BY_EXPIRY)

    async def _release(
        self, session: _MediaSession, *, state: str, reason: str
    ) -> None:
        if session.state in TERMINAL_STATES:
            return
        session.state = state
        session.close_reason = reason
        session.closed_at = self._clock()
        await self._release_engine(session, reason)
        await self._notify_closed(session.media_session_id, reason)

    async def _notify_closed(self, media_session_id: str, reason: str) -> None:
        """Best-effort teardown notification; never fails the media path."""
        for listener in list(self._close_listeners):
            try:
                await listener(media_session_id, reason)
            except Exception:  # pragma: no cover - defensive isolation
                logger.exception(
                    "media close listener failed for %s", media_session_id
                )

    async def _release_engine(self, session: _MediaSession, reason: str) -> None:
        try:
            await self.engine.close_session(session.media_session_id, reason)
        except Exception:  # pragma: no cover - teardown must not fail closed
            logger.exception(
                "media engine failed to release session %s", session.media_session_id
            )

    def _validate_offer(self, *, type: str | None, sdp: str | None) -> str:
        if type != "offer":
            raise ProductError(
                "unsupported_content_type",
                "Offer type must be 'offer'.",
            )
        if not isinstance(sdp, str) or not sdp.strip():
            raise ProductError("invalid_input", "Offer sdp must be a non-empty string.")
        if len(sdp.encode("utf-8")) > self.settings.media_max_sdp_bytes:
            raise ProductError(
                "payload_too_large",
                "Offer sdp exceeds the configured limit.",
            )
        return sdp


__all__ = [
    "CLOSE_BY_CLIENT",
    "CLOSE_BY_DELETE",
    "CLOSE_BY_EXPIRY",
    "CLOSE_BY_NEGOTIATION",
    "CLOSE_BY_SHUTDOWN",
    "MediaService",
    "STATE_CLOSED",
    "STATE_CONNECTED",
    "STATE_CREATED",
    "STATE_FAILED",
    "STATE_NEGOTIATING",
    "TERMINAL_STATES",
    "media_session_object",
]
