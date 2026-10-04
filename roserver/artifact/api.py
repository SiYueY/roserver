"""Artifact Product API (docs §5.1)."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, File, Form, Request, UploadFile
from fastapi.responses import Response

from ..errors import ProductError
from .service import ArtifactService

router = APIRouter(prefix="/api/v1")

_CHUNK = 1024 * 1024


def _service(request: Request) -> ArtifactService:
    service = getattr(request.app.state, "artifacts", None)
    if service is None:  # pragma: no cover - create_app always wires this
        raise ProductError("internal_error", "Artifact service is not configured.")
    return service


async def _read_limited(upload: UploadFile, limit: int) -> bytes:
    """Read an upload in chunks, failing fast once the limit is exceeded."""
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = await upload.read(_CHUNK)
        if not chunk:
            break
        total += len(chunk)
        if total > limit:
            raise ProductError(
                "payload_too_large", "Artifact exceeds the configured limit."
            )
        chunks.append(chunk)
    return b"".join(chunks)


@router.post("/artifacts", status_code=201)
async def create_artifact(
    request: Request,
    file: UploadFile = File(...),
    media_type: str | None = Form(default=None),
    filename: str | None = Form(default=None),
) -> dict[str, Any]:
    service = _service(request)
    declared = media_type or file.content_type
    data = await _read_limited(file, service.settings.max_artifact_bytes)
    name = filename or file.filename
    return await service.put(data, media_type=declared, filename=name)


@router.get("/artifacts/{artifact_id}")
async def get_artifact(artifact_id: str, request: Request) -> dict[str, Any]:
    return await _service(request).get(artifact_id)


@router.get("/artifacts/{artifact_id}/content")
async def get_artifact_content(artifact_id: str, request: Request) -> Response:
    data, media_type = await _service(request).read(artifact_id)
    return Response(
        content=data,
        media_type=media_type,
        headers={"ETag": f'"{artifact_id}"', "Cache-Control": "private, immutable"},
    )


@router.delete("/artifacts/{artifact_id}", status_code=204)
async def delete_artifact(artifact_id: str, request: Request) -> Response:
    await _service(request).delete(artifact_id)
    return Response(status_code=204)


__all__ = ["router"]
