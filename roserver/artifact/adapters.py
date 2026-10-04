"""roboagent extension adapters backed by :class:`ArtifactService` (docs §5.2).

Artifact read/write plumbing is *not* re-implemented here: roboagent already
ships ``WorkspaceArtifactReader`` / ``WorkspaceArtifactDestination``, which work
against any :class:`~roserver.artifact.workspace.ArtifactWorkspace`.  Only the
``MediaResolver`` — which roboagent does not provide — lives in this module.
"""

from __future__ import annotations

from pathlib import Path

from roboagent.message import BytesSource, FileSource, UrlSource
from roboagent.runtime import (
    MediaOwnership,
    MediaResolutionError,
    MediaResolutionErrorCode,
    ResolvedMedia,
)

from .service import ArtifactService


class ServiceMediaResolver:
    """``MediaResolver`` protocol for inline (non-artifact) media sources."""

    def __init__(self, service: ArtifactService) -> None:
        self.service = service

    async def resolve(
        self,
        source: object,
        *,
        expected_media_type: str | None,
        cancellation: object,
    ) -> ResolvedMedia:
        limit = self.service.settings.max_artifact_bytes
        if isinstance(source, BytesSource):
            data = source.data
            if len(data) > limit:
                raise MediaResolutionError(
                    MediaResolutionErrorCode.TOO_LARGE,
                    "Inline media exceeds the configured artifact limit.",
                )
            return ResolvedMedia(
                payload=data,
                media_type=expected_media_type,
                size=len(data),
                source=source,
                ownership=MediaOwnership.OWNED,
            )
        if isinstance(source, FileSource):
            path = Path(source.path)
            try:
                size = path.stat().st_size
            except OSError as exc:
                raise MediaResolutionError(
                    MediaResolutionErrorCode.NOT_FOUND,
                    "Media file is not accessible.",
                ) from exc
            if size > limit:
                raise MediaResolutionError(
                    MediaResolutionErrorCode.TOO_LARGE,
                    "Media file exceeds the configured artifact limit.",
                )
            return ResolvedMedia(
                payload=path,
                media_type=expected_media_type,
                size=size,
                source=source,
                ownership=MediaOwnership.BORROWED,
            )
        if isinstance(source, UrlSource):
            # roserver deliberately does not fetch arbitrary URLs for the runtime.
            raise MediaResolutionError(
                MediaResolutionErrorCode.ACCESS_DENIED,
                "URL media sources are not resolvable; upload an artifact instead.",
            )
        raise MediaResolutionError(
            MediaResolutionErrorCode.FETCH_FAILED,
            f"Unsupported media source {type(source).__name__}.",
        )


__all__ = ["ServiceMediaResolver"]
