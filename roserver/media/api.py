"""Media Product API: WebRTC signaling (docs §5.5).

    POST   /api/v1/media/sessions
    GET    /api/v1/media/sessions/{media_session_id}
    POST   /api/v1/media/sessions/{media_session_id}/offer
    DELETE /api/v1/media/sessions/{media_session_id}

This is the exact wire contract consumed by rodesk's
``ProductCameraSignaling`` (non-trickle ICE: the answer is complete).
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request, Response
from pydantic import BaseModel

from .service import MediaService

router = APIRouter(prefix="/api/v1")


class CreateMediaSessionRequest(BaseModel):
    kind: str
    session_id: str | None = None
    audio: bool = False
    video: bool = False
    video_source: str | None = None


class OfferRequest(BaseModel):
    type: str | None = None
    sdp: str | None = None


def get_media_service(request: Request) -> MediaService:
    return request.app.state.media


@router.post("/media/sessions", status_code=201)
async def create_media_session(
    request: Request, body: CreateMediaSessionRequest
) -> dict[str, Any]:
    return await get_media_service(request).create_session(
        kind=body.kind,
        session_id=body.session_id,
        audio=body.audio,
        video=body.video,
        video_source=body.video_source,
    )


@router.get("/media/sessions/{media_session_id}")
async def get_media_session(
    media_session_id: str, request: Request
) -> dict[str, Any]:
    return await get_media_service(request).get_session(media_session_id)


@router.post("/media/sessions/{media_session_id}/offer")
async def exchange_offer(
    media_session_id: str, request: Request, body: OfferRequest
) -> dict[str, Any]:
    return await get_media_service(request).handle_offer(
        media_session_id, type=body.type, sdp=body.sdp
    )


@router.delete("/media/sessions/{media_session_id}", status_code=204)
async def delete_media_session(media_session_id: str, request: Request) -> Response:
    await get_media_service(request).close_session(media_session_id)
    return Response(status_code=204)


__all__ = ["get_media_service", "router"]
