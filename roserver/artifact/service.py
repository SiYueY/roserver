"""ArtifactService: digest-addressed storage and the Product Artifact contract.

Implements docs §5.1 (Product contract), §5.2 (mapping to roboagent) and the
content rules of §2.8.  ``artifact_id`` is always derived from the content, never
supplied by a client, and equals the ``sha256:`` digest roboagent uses in
``ArtifactReferenceContent``.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import re
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from roboagent.message import ArtifactReferenceContent

from ..config import Settings
from ..errors import ProductError
from ..store.application import ApplicationStore

_MEDIA_TYPE = re.compile(r"^[a-z0-9!#$&^_.+-]+/[a-z0-9!#$&^_.+-]+$")
_ARTIFACT_ID = re.compile(r"^sha256:[0-9a-f]{64}$")
_DEFAULT_MEDIA_TYPE = "application/octet-stream"


def is_artifact_id(value: object) -> bool:
    return isinstance(value, str) and bool(_ARTIFACT_ID.match(value))


def _normalize_media_type(media_type: object) -> str:
    if media_type is None:
        return _DEFAULT_MEDIA_TYPE
    if not isinstance(media_type, str):
        raise ProductError("invalid_input", "media_type must be a string.")
    candidate = media_type.split(";", 1)[0].strip().lower()
    if not candidate:
        return _DEFAULT_MEDIA_TYPE
    if not _MEDIA_TYPE.match(candidate):
        raise ProductError(
            "unsupported_content_type", f"Unsupported media_type {media_type!r}."
        )
    return candidate


def product_branch(media_type: str | None) -> str:
    """Product Content branch for a stored artifact (docs §2.8)."""
    value = media_type or ""
    if value.startswith("image/"):
        return "image"
    if value.startswith("audio/"):
        return "audio"
    return "file"


def _rfc3339(timestamp: float) -> str:
    return (
        datetime.fromtimestamp(timestamp, tz=timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


class ArtifactService:
    def __init__(
        self,
        store: ApplicationStore,
        settings: Settings,
        *,
        owner_id: str | None = None,
    ) -> None:
        self.store = store
        self.settings = settings
        self.owner_id = owner_id or settings.owner_id
        self.root = settings.resolved_artifacts_dir

    # -- storage -------------------------------------------------------
    def blob_path(self, artifact_id: str) -> Path:
        if not is_artifact_id(artifact_id):
            raise ProductError("artifact_not_found", "Artifact does not exist.")
        digest = artifact_id.split(":", 1)[1]
        return self.root / digest[:2] / digest

    def _write_blob(self, path: Path, data: bytes) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists() and path.stat().st_size == len(data):
            return
        descriptor, temporary = tempfile.mkstemp(
            prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
        )
        try:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
            directory = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        finally:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass

    def _read_blob(self, path: Path) -> bytes:
        try:
            return path.read_bytes()
        except FileNotFoundError as exc:
            raise ProductError(
                "artifact_not_found", "Artifact content is missing."
            ) from exc

    def _remove_blob(self, path: Path) -> None:
        try:
            path.unlink()
        except FileNotFoundError:
            pass

    # -- Product operations -------------------------------------------
    async def put(
        self,
        data: bytes,
        *,
        media_type: object = None,
        filename: str | None = None,
        session_id: str | None = None,
    ) -> dict[str, Any]:
        if not isinstance(data, bytes):  # pragma: no cover - defensive
            raise ProductError("invalid_input", "Artifact payload must be bytes.")
        if len(data) > self.settings.max_artifact_bytes:
            raise ProductError(
                "payload_too_large", "Artifact exceeds the configured limit."
            )
        if filename is not None and not isinstance(filename, str):
            raise ProductError("invalid_input", "filename must be a string.")
        resolved_media_type = _normalize_media_type(media_type)

        digest = hashlib.sha256(data).hexdigest()
        artifact_id = f"sha256:{digest}"

        # Content-addressed: identical bytes always resolve to the same artifact.
        existing = await self.store.get_artifact(artifact_id, self.owner_id)
        if existing is not None:
            result = self._object(existing)
            # Content-addressed dedup: keep the originally stored metadata so a
            # later GET returns exactly what POST returned.
            return result

        path = self.root / digest[:2] / digest
        await asyncio.to_thread(self._write_blob, path, data)
        await self.store.insert_artifact(
            {
                "artifact_id": artifact_id,
                "owner_id": self.owner_id,
                "session_id": session_id,
                "media_type": resolved_media_type,
                "size": len(data),
                "digest": artifact_id,
                "path": str(path),
                "filename": filename,
                "created_at": time.time(),
            }
        )
        stored = await self.store.get_artifact(artifact_id, self.owner_id)
        if stored is not None:
            return self._object(stored)
        return {
            "artifact_id": artifact_id,
            "media_type": resolved_media_type,
            "size": len(data),
            "filename": filename,
            "created_at": _rfc3339(time.time()),
        }

    async def get(self, artifact_id: str) -> dict[str, Any]:
        record = await self.store.get_artifact(artifact_id, self.owner_id)
        if record is None:
            raise ProductError("artifact_not_found", "Artifact does not exist.")
        return self._object(record)

    async def read(self, artifact_id: str) -> tuple[bytes, str]:
        record = await self.store.get_artifact(artifact_id, self.owner_id)
        if record is None:
            raise ProductError("artifact_not_found", "Artifact does not exist.")
        data = await asyncio.to_thread(self._read_blob, self.blob_path(artifact_id))
        return data, record.get("media_type") or _DEFAULT_MEDIA_TYPE

    async def delete(self, artifact_id: str) -> None:
        record = await self.store.get_artifact(artifact_id, self.owner_id)
        if record is None:
            # Repeated DELETE ends as deleted (docs §5.1).
            return
        references = await self.store.count_live_artifact_references(
            artifact_id, self.owner_id
        )
        if references:
            raise ProductError(
                "artifact_in_use",
                "Artifact is still referenced by a stored message.",
            )
        await self.store.delete_artifact(artifact_id, self.owner_id)
        path = record.get("path")
        if isinstance(path, str) and path:
            await asyncio.to_thread(self._remove_blob, Path(path))

    async def reference(self, artifact_id: str, session_id: str) -> None:
        """Record that a Session's input uses this artifact."""
        await self.store.add_artifact_reference(
            artifact_id, self.owner_id, session_id, time.time()
        )

    async def release_session(self, session_id: str) -> None:
        await self.store.delete_artifact_references_for_session(
            session_id, self.owner_id
        )

    # -- mapping to roboagent (docs §5.2) ------------------------------
    def workspace_uri(self, artifact_id: str) -> str:
        if not is_artifact_id(artifact_id):
            raise ProductError("artifact_not_found", "Artifact does not exist.")
        # roboagent's canonical content-addressed workspace path.
        return f"workspace://blobs/sha256/{artifact_id.split(':', 1)[1]}"

    def to_reference(self, record: Mapping[str, Any]) -> ArtifactReferenceContent:
        artifact_id = str(record["artifact_id"])
        return ArtifactReferenceContent(
            self.workspace_uri(artifact_id),
            record.get("media_type"),
            int(record["size"]),
            artifact_id,
            None,
        )

    def _object(self, record: Mapping[str, Any]) -> dict[str, Any]:
        return {
            "artifact_id": record["artifact_id"],
            "media_type": record.get("media_type") or _DEFAULT_MEDIA_TYPE,
            "size": int(record.get("size") or 0),
            "filename": record.get("filename"),
            "created_at": _rfc3339(float(record.get("created_at") or 0.0)),
        }


__all__ = [
    "ArtifactService",
    "is_artifact_id",
    "product_branch",
]
