"""Durable roboagent ``Workspace`` backed by the Product artifact store.

roboagent materializes binary Tool output through a *Workspace* (see
``roboagent.tool.materializer.WorkspaceToolResultMaterializer``): it writes to
``blobs/sha256/<digest>`` and requires the returned entry to carry that exact
path, size and ``sha256:`` digest.  Implementing the protocol here means the
runtime's own ``WorkspaceArtifactReader`` / ``WorkspaceArtifactDestination`` work
unchanged, so roserver does not re-implement artifact plumbing (docs §5.2).
"""

from __future__ import annotations

import re
from collections.abc import Sequence

from roboagent.tool import (
    WorkspaceEntry,
    WorkspaceError,
    WorkspaceMissingError,
    WorkspacePermissionError,
)

from .service import ArtifactService

_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
_HEX = re.compile(r"^[0-9a-f]{64}$")

ARTIFACT_PREFIX = "blobs/sha256/"


def artifact_path(artifact_id: str) -> str:
    """Canonical workspace path for an artifact id (roboagent's convention)."""
    if not _DIGEST.match(artifact_id):
        raise WorkspacePermissionError("Invalid artifact id.")
    return f"{ARTIFACT_PREFIX}{artifact_id.split(':', 1)[1]}"


class ArtifactWorkspace:
    """``Workspace`` protocol implementation over :class:`ArtifactService`."""

    def __init__(self, service: ArtifactService) -> None:
        self.service = service

    @property
    def durable(self) -> bool:
        return True

    # -- path mapping --------------------------------------------------
    def _artifact_id(self, path: str) -> str:
        if not isinstance(path, str) or not path:
            raise WorkspacePermissionError("Invalid workspace path.")
        normalized = path.strip("/")
        if not normalized.startswith(ARTIFACT_PREFIX):
            raise WorkspaceMissingError(
                f"Workspace path {path!r} is not a content-addressed artifact."
            )
        digest = normalized[len(ARTIFACT_PREFIX) :]
        if not _HEX.match(digest):
            raise WorkspacePermissionError("Invalid workspace artifact path.")
        return f"sha256:{digest}"

    # -- Workspace protocol --------------------------------------------
    async def read(self, path: str) -> bytes:
        artifact_id = self._artifact_id(path)
        data, _ = await self.service.read(artifact_id)
        return data

    async def write(
        self, path: str, data: bytes, *, media_type: str | None = None
    ) -> WorkspaceEntry:
        artifact_id = self._artifact_id(path)
        if not isinstance(data, bytes):
            raise WorkspaceError("Workspace.write requires bytes.")
        record = await self.service.put(data, media_type=media_type)
        if record["artifact_id"] != artifact_id:
            raise WorkspaceError(
                "Workspace path digest does not match the written content."
            )
        return WorkspaceEntry(
            path=path,
            size=int(record["size"]),
            media_type=record["media_type"],
            digest=record["artifact_id"],
        )

    async def stat(self, path: str) -> WorkspaceEntry:
        artifact_id = self._artifact_id(path)
        record = await self.service.get(artifact_id)
        return WorkspaceEntry(
            path=path,
            size=int(record["size"]),
            media_type=record["media_type"],
            digest=record["artifact_id"],
        )

    async def list(self, path: str = ".") -> Sequence[WorkspaceEntry]:
        prefix = "" if path.strip("/") in ("", ".") else path.strip("/") + "/"
        entries: list[WorkspaceEntry] = []
        for record in await self.service.store.list_artifacts(self.service.owner_id):
            entry_path = artifact_path(str(record["artifact_id"]))
            if prefix and not entry_path.startswith(prefix):
                continue
            entries.append(
                WorkspaceEntry(
                    path=entry_path,
                    size=int(record.get("size") or 0),
                    media_type=record.get("media_type"),
                    digest=str(record["artifact_id"]),
                )
            )
        return entries

    async def delete(self, path: str) -> None:
        artifact_id = self._artifact_id(path)
        await self.service.delete(artifact_id)


__all__ = ["ARTIFACT_PREFIX", "ArtifactWorkspace", "artifact_path"]
