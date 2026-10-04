"""System endpoints."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter

from .. import __version__

router = APIRouter(prefix="/api/v1")


@router.get("/health")
async def health() -> dict[str, Any]:
    return {"status": "ok", "version": __version__, "workers": 1}


__all__ = ["router"]
