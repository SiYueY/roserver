"""Speech Product API: the PCM / speech-event bridge (docs §5.7).

    WS /api/v1/media/sessions/{media_session_id}/speech

Server -> client: the Product speech event envelope, one type per projected
roboagent ``SpeechEvent``::

    {"media_session_id": "...", "session_id": "...", "sequence": 1,
     "type": "transcript.partial", "timestamp": "...", "data": {...}}

Client -> server:

* binary frame  -- raw PCM16 little-endian, 16 kHz, mono (the canonical
  capture format).  The most efficient feed path.
* text ``{"type": "audio", "audio": "<base64>", "format": "pcm16_16k_mono"}``
* text ``{"type": "interrupt", "reason": "barge_in"}`` -- barge-in; the engine
  invalidates queued output and calls roboagent ``clear_output``.

Errors: unknown media session -> 404 ``media_session_not_found``; terminal
media session -> 409 ``media_session_closed``.  Mid-stream failures degrade to
a ``speech.error`` envelope carrying a registered Product code.  Speech events
never enter the agent session's event stream.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import json
import time
from typing import Any

from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from starlette.responses import JSONResponse

from roboagent.speech.types import DEFAULT_INPUT_FORMAT, AudioChunk

from ..errors import ProductError
from .service import CLOSE_BY_CLIENT, SpeechBridge, SpeechService

router = APIRouter(prefix="/api/v1")

#: The documented capture format of the client -> server audio path.
CAPTURE_FORMAT_NAME = "pcm16_16k_mono"


def get_speech_service(websocket: WebSocket) -> SpeechService:
    return websocket.scope["app"].state.speech


def _origin_allowed(websocket: WebSocket) -> bool:
    app = websocket.scope["app"]
    settings = app.state.settings
    if not settings.ws_origin_validation:
        return True
    origin = websocket.headers.get("origin")
    if origin is None:
        return True
    return origin in settings.resolved_ws_origins


async def _deny(websocket: WebSocket, exc: ProductError) -> None:
    """Reject the handshake with the Product error envelope and its status."""
    response = JSONResponse(status_code=exc.status, content=exc.to_body(""))
    try:
        await websocket.send_denial_response(response)
    except Exception:  # pragma: no cover - transport-level fallback
        await websocket.close(code=1008)


async def _safe_send_error(
    websocket: WebSocket, speech: SpeechService, bridge: SpeechBridge, code: str, message: str
) -> None:
    try:
        await websocket.send_json(speech.error_event(bridge, code, message))
    except Exception:  # pragma: no cover - the peer already went away
        return


def _audio_chunk(raw: bytes) -> AudioChunk:
    return AudioChunk(raw, DEFAULT_INPUT_FORMAT, time.monotonic())


async def _feed_bytes(
    websocket: WebSocket, speech: SpeechService, bridge: SpeechBridge, raw: bytes
) -> None:
    if len(raw) > speech.settings.speech_max_audio_frame_bytes:
        await _safe_send_error(
            websocket,
            speech,
            bridge,
            "payload_too_large",
            "Audio frame exceeds the configured limit.",
        )
        return
    try:
        await speech.feed(bridge, _audio_chunk(raw))
    except ProductError as exc:
        await _safe_send_error(websocket, speech, bridge, exc.code, exc.message)


def _decode_base64_audio(value: Any) -> bytes | None:
    if not isinstance(value, str):
        return None
    try:
        return base64.b64decode(value, validate=True)
    except (binascii.Error, ValueError):
        return None


async def _handle_text(
    websocket: WebSocket, speech: SpeechService, bridge: SpeechBridge, text: str
) -> None:
    try:
        payload = json.loads(text)
    except ValueError:
        await _safe_send_error(
            websocket, speech, bridge, "invalid_input", "Message is not valid JSON."
        )
        return
    if not isinstance(payload, dict):
        await _safe_send_error(
            websocket, speech, bridge, "invalid_input", "Message must be a JSON object."
        )
        return
    kind = payload.get("type")
    if kind == "audio":
        fmt = payload.get("format", CAPTURE_FORMAT_NAME)
        if fmt not in (None, CAPTURE_FORMAT_NAME):
            await _safe_send_error(
                websocket,
                speech,
                bridge,
                "unsupported_content_type",
                f"Unsupported audio format {fmt!r}.",
            )
            return
        raw = _decode_base64_audio(payload.get("audio"))
        if raw is None:
            await _safe_send_error(
                websocket,
                speech,
                bridge,
                "invalid_input",
                "audio must be a base64 string.",
            )
            return
        await _feed_bytes(websocket, speech, bridge, raw)
        return
    if kind == "interrupt":
        reason = payload.get("reason", "barge_in")
        if not isinstance(reason, str):
            reason = "barge_in"
        try:
            await speech.interrupt(bridge, reason=reason)
        except ProductError as exc:
            await _safe_send_error(websocket, speech, bridge, exc.code, exc.message)
        return
    await _safe_send_error(
        websocket,
        speech,
        bridge,
        "unsupported_content_type",
        f"Unsupported message type {kind!r}.",
    )


async def _pump(
    websocket: WebSocket, speech: SpeechService, bridge: SpeechBridge
) -> None:
    async def reader() -> None:
        while True:
            message = await websocket.receive()
            if message.get("type") == "websocket.disconnect":
                return
            raw = message.get("bytes")
            if raw is not None:
                await _feed_bytes(websocket, speech, bridge, raw)
                continue
            text = message.get("text")
            if text is not None:
                await _handle_text(websocket, speech, bridge, text)

    async def writer() -> None:
        try:
            async for event in speech.events(bridge):
                envelope = speech.project(bridge, event)
                if envelope is None:
                    continue
                await websocket.send_json(envelope)
        except WebSocketDisconnect:
            return
        except ProductError as exc:
            await _safe_send_error(websocket, speech, bridge, exc.code, exc.message)
        except Exception:  # pragma: no cover - defensive isolation
            await _safe_send_error(
                websocket,
                speech,
                bridge,
                "internal_error",
                "Speech event stream failed.",
            )

    read_task = asyncio.create_task(reader())
    write_task = asyncio.create_task(writer())
    try:
        _, pending = await asyncio.wait(
            {read_task, write_task}, return_when=asyncio.FIRST_COMPLETED
        )
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
    finally:
        for task in (read_task, write_task):
            if not task.done():
                task.cancel()
        await asyncio.gather(read_task, write_task, return_exceptions=True)


@router.websocket("/media/sessions/{media_session_id}/speech")
async def speech_channel(websocket: WebSocket, media_session_id: str) -> None:
    if not _origin_allowed(websocket):
        await websocket.close(code=1008)
        return
    speech = get_speech_service(websocket)
    try:
        bridge = await speech.begin(media_session_id)
    except ProductError as exc:
        await _deny(websocket, exc)
        return
    except Exception:  # pragma: no cover - defensive isolation
        await _deny(
            websocket,
            ProductError("internal_error", "Speech bridge is unavailable."),
        )
        return
    await websocket.accept()
    try:
        await _pump(websocket, speech, bridge)
    finally:
        await speech.end(bridge.media_session_id, reason=CLOSE_BY_CLIENT)


__all__ = ["CAPTURE_FORMAT_NAME", "get_speech_service", "router"]
