"""SQLite ApplicationStore (docs §1.5).

Authoritative for Product metadata only: Session rows, RunRecords,
IdempotencyRecords, ApprovalRecords and the Phase 3/4 stub tables.  It never
stores a second canonical transcript.  All blocking SQLite work is offloaded
with ``asyncio.to_thread``.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
import threading
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    session_id         TEXT PRIMARY KEY,
    owner_id           TEXT NOT NULL,
    title              TEXT NOT NULL,
    internal_state     TEXT NOT NULL,
    active_root_run_id TEXT,
    created_at         REAL NOT NULL,
    updated_at         REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_sessions_owner_updated
    ON sessions(owner_id, updated_at DESC, session_id DESC);
CREATE INDEX IF NOT EXISTS idx_sessions_state
    ON sessions(internal_state);

CREATE TABLE IF NOT EXISTS runs (
    run_id           TEXT PRIMARY KEY,
    owner_id         TEXT NOT NULL,
    session_id       TEXT NOT NULL,
    root_run_id      TEXT NOT NULL,
    parent_run_id    TEXT,
    source_run_id    TEXT NOT NULL,
    agent_tool_name  TEXT,
    status           TEXT NOT NULL,
    request_id       TEXT,
    created_at       REAL NOT NULL,
    started_at       REAL,
    ended_at         REAL,
    error_code       TEXT,
    error_message    TEXT,
    error_retryable  INTEGER,
    input_tokens     INTEGER,
    output_tokens    INTEGER,
    total_tokens     INTEGER,
    usage_known      INTEGER,
    retry_safe       INTEGER,
    effects_json     TEXT
);
CREATE INDEX IF NOT EXISTS idx_runs_session ON runs(owner_id, session_id);
CREATE INDEX IF NOT EXISTS idx_runs_status ON runs(owner_id, status);
CREATE INDEX IF NOT EXISTS idx_runs_root ON runs(root_run_id);

CREATE TABLE IF NOT EXISTS idempotency (
    owner_id       TEXT NOT NULL,
    operation      TEXT NOT NULL,
    scope_id       TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    request_digest TEXT NOT NULL,
    state          TEXT NOT NULL,
    response_status INTEGER,
    response_body  TEXT,
    result_reference TEXT,
    created_at     REAL NOT NULL,
    expires_at     REAL NOT NULL,
    PRIMARY KEY (owner_id, operation, scope_id, idempotency_key)
);
CREATE INDEX IF NOT EXISTS idx_idempotency_expires ON idempotency(expires_at);

CREATE TABLE IF NOT EXISTS approvals (
    approval_id       TEXT NOT NULL,
    owner_id          TEXT NOT NULL,
    session_id        TEXT NOT NULL,
    root_run_id       TEXT NOT NULL,
    source_run_id     TEXT NOT NULL,
    tool_call_id      TEXT NOT NULL,
    tool_name         TEXT NOT NULL,
    arguments_json    TEXT NOT NULL,
    arguments_digest  TEXT NOT NULL,
    reason            TEXT,
    effect_capability TEXT,
    summary           TEXT NOT NULL,
    status            TEXT NOT NULL,
    decision          TEXT,
    decision_reason   TEXT,
    terminal_reason   TEXT,
    created_at        REAL NOT NULL,
    expires_at        REAL NOT NULL,
    resolved_at       REAL,
    PRIMARY KEY (owner_id, approval_id)
);
CREATE INDEX IF NOT EXISTS idx_approvals_root ON approvals(root_run_id);
CREATE INDEX IF NOT EXISTS idx_approvals_status ON approvals(owner_id, status);

CREATE TABLE IF NOT EXISTS artifacts (
    artifact_id  TEXT PRIMARY KEY,
    owner_id     TEXT NOT NULL,
    session_id   TEXT,
    media_type   TEXT,
    size         INTEGER NOT NULL DEFAULT 0,
    digest       TEXT,
    path         TEXT,
    filename     TEXT,
    created_at   REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS robot_authorities (
    robot_id     TEXT PRIMARY KEY,
    owner_id     TEXT NOT NULL,
    authority_id TEXT,
    holder_id    TEXT,
    mode         TEXT,
    acquired_at  REAL,
    expires_at   REAL
);

CREATE TABLE IF NOT EXISTS robot_operations (
    operation_id TEXT PRIMARY KEY,
    owner_id TEXT NOT NULL,
    robot_id TEXT NOT NULL,
    idempotency_key TEXT,
    arguments_digest TEXT NOT NULL,
    arguments_json TEXT NOT NULL,
    record_json TEXT NOT NULL,
    updated_at REAL NOT NULL,
    UNIQUE(owner_id, robot_id, idempotency_key)
);

CREATE TABLE IF NOT EXISTS artifact_references (
    artifact_id TEXT NOT NULL,
    owner_id    TEXT NOT NULL,
    session_id  TEXT NOT NULL,
    created_at  REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_artifact_refs
    ON artifact_references(artifact_id, owner_id);
"""

TERMINAL_RUN_STATUSES = ("completed", "failed", "cancelled", "interrupted")
NONTERMINAL_RUN_STATUSES = ("created", "running")


def _row_to_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    return None if row is None else {key: row[key] for key in row.keys()}


def _migrate(connection: sqlite3.Connection) -> None:
    """Idempotent, additive schema upgrades for databases created earlier."""
    columns = {
        row["name"]
        for row in connection.execute("PRAGMA table_info(artifacts)").fetchall()
    }
    if "filename" not in columns:
        connection.execute("ALTER TABLE artifacts ADD COLUMN filename TEXT")
    robot_columns = {
        row["name"]
        for row in connection.execute("PRAGMA table_info(robot_authorities)").fetchall()
    }
    if "authority_id" not in robot_columns:
        connection.execute("ALTER TABLE robot_authorities ADD COLUMN authority_id TEXT")
    if "mode" not in robot_columns:
        connection.execute("ALTER TABLE robot_authorities ADD COLUMN mode TEXT")


class ApplicationStore:
    """Single-connection SQLite store guarded by a thread lock."""

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path).expanduser().resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._connection: sqlite3.Connection | None = None

    # -- lifecycle -------------------------------------------------
    async def open(self) -> None:
        await asyncio.to_thread(self._open)

    def _open(self) -> None:
        with self._lock:
            if self._connection is not None:
                return
            connection = sqlite3.connect(self.path, check_same_thread=False)
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("PRAGMA synchronous=NORMAL")
            connection.execute("PRAGMA foreign_keys=ON")
            connection.executescript(SCHEMA)
            _migrate(connection)
            connection.commit()
            self._connection = connection

    async def close(self) -> None:
        await asyncio.to_thread(self._close)

    def _close(self) -> None:
        with self._lock:
            if self._connection is not None:
                self._connection.close()
                self._connection = None

    def _execute(
        self, sql: str, parameters: Sequence[object] = ()
    ) -> sqlite3.Cursor:
        if self._connection is None:
            raise RuntimeError("ApplicationStore is not open.")
        with self._lock:
            cursor = self._connection.execute(sql, parameters)
            self._connection.commit()
            return cursor

    def _query(
        self, sql: str, parameters: Sequence[object] = ()
    ) -> list[dict[str, Any]]:
        if self._connection is None:
            raise RuntimeError("ApplicationStore is not open.")
        with self._lock:
            cursor = self._connection.execute(sql, parameters)
            return [{key: row[key] for key in row.keys()} for row in cursor.fetchall()]

    # -- sessions --------------------------------------------------
    async def create_session(
        self,
        *,
        session_id: str,
        owner_id: str,
        title: str,
        internal_state: str,
        created_at: float,
        updated_at: float,
    ) -> None:
        await asyncio.to_thread(
            self._execute,
            "INSERT INTO sessions(session_id, owner_id, title, internal_state, "
            "active_root_run_id, created_at, updated_at) VALUES (?,?,?,?,NULL,?,?)",
            (session_id, owner_id, title, internal_state, created_at, updated_at),
        )

    async def get_session(
        self, session_id: str, owner_id: str
    ) -> dict[str, Any] | None:
        rows = await asyncio.to_thread(
            self._query,
            "SELECT * FROM sessions WHERE session_id=? AND owner_id=?",
            (session_id, owner_id),
        )
        return rows[0] if rows else None

    async def list_sessions(
        self,
        owner_id: str,
        *,
        limit: int,
        cursor: tuple[float, str] | None,
    ) -> list[dict[str, Any]]:
        if cursor is None:
            rows = await asyncio.to_thread(
                self._query,
                "SELECT * FROM sessions WHERE owner_id=? "
                "ORDER BY updated_at DESC, session_id DESC LIMIT ?",
                (owner_id, limit + 1),
            )
        else:
            updated_at, session_id = cursor
            rows = await asyncio.to_thread(
                self._query,
                "SELECT * FROM sessions WHERE owner_id=? AND "
                "(updated_at < ? OR (updated_at = ? AND session_id < ?)) "
                "ORDER BY updated_at DESC, session_id DESC LIMIT ?",
                (owner_id, updated_at, updated_at, session_id, limit + 1),
            )
        return rows

    async def update_session_title(
        self, session_id: str, owner_id: str, title: str, updated_at: float
    ) -> None:
        await asyncio.to_thread(
            self._execute,
            "UPDATE sessions SET title=?, updated_at=? WHERE session_id=? AND owner_id=?",
            (title, updated_at, session_id, owner_id),
        )

    async def set_session_state(
        self, session_id: str, state: str, updated_at: float
    ) -> None:
        await asyncio.to_thread(
            self._execute,
            "UPDATE sessions SET internal_state=?, updated_at=? WHERE session_id=?",
            (state, updated_at, session_id),
        )

    async def set_session_active_run(
        self, session_id: str, run_id: str | None, updated_at: float
    ) -> None:
        await asyncio.to_thread(
            self._execute,
            "UPDATE sessions SET active_root_run_id=?, updated_at=? WHERE session_id=?",
            (run_id, updated_at, session_id),
        )

    async def touch_session(self, session_id: str, updated_at: float) -> None:
        await asyncio.to_thread(
            self._execute,
            "UPDATE sessions SET updated_at=? WHERE session_id=?",
            (updated_at, session_id),
        )

    async def delete_session(self, session_id: str, owner_id: str) -> None:
        await asyncio.to_thread(
            self._execute,
            "DELETE FROM sessions WHERE session_id=? AND owner_id=?",
            (session_id, owner_id),
        )

    async def list_sessions_by_state(self, state: str) -> list[dict[str, Any]]:
        return await asyncio.to_thread(
            self._query, "SELECT * FROM sessions WHERE internal_state=?", (state,)
        )

    # -- runs ------------------------------------------------------
    async def insert_run(self, record: Mapping[str, Any]) -> None:
        fields = (
            "run_id",
            "owner_id",
            "session_id",
            "root_run_id",
            "parent_run_id",
            "source_run_id",
            "agent_tool_name",
            "status",
            "request_id",
            "created_at",
            "started_at",
            "ended_at",
            "error_code",
            "error_message",
            "error_retryable",
            "input_tokens",
            "output_tokens",
            "total_tokens",
            "usage_known",
            "retry_safe",
            "effects_json",
        )
        values = tuple(record.get(name) for name in fields)
        placeholders = ",".join("?" for _ in fields)
        await asyncio.to_thread(
            self._execute,
            f"INSERT INTO runs({','.join(fields)}) VALUES ({placeholders})",
            values,
        )

    async def get_run(self, run_id: str, owner_id: str) -> dict[str, Any] | None:
        rows = await asyncio.to_thread(
            self._query,
            "SELECT * FROM runs WHERE run_id=? AND owner_id=?",
            (run_id, owner_id),
        )
        return rows[0] if rows else None

    async def list_runs_for_session(
        self, session_id: str, owner_id: str
    ) -> list[dict[str, Any]]:
        return await asyncio.to_thread(
            self._query,
            "SELECT * FROM runs WHERE session_id=? AND owner_id=? ORDER BY created_at",
            (session_id, owner_id),
        )

    async def list_nonterminal_runs(self, owner_id: str) -> list[dict[str, Any]]:
        placeholders = ",".join("?" for _ in NONTERMINAL_RUN_STATUSES)
        return await asyncio.to_thread(
            self._query,
            f"SELECT * FROM runs WHERE owner_id=? AND status IN ({placeholders}) "
            "ORDER BY created_at",
            (owner_id, *NONTERMINAL_RUN_STATUSES),
        )

    async def update_run_started(self, run_id: str, started_at: float) -> None:
        await asyncio.to_thread(
            self._execute,
            "UPDATE runs SET status='running', started_at=COALESCE(started_at, ?) "
            "WHERE run_id=? AND status='created'",
            (started_at, run_id),
        )

    async def update_run_terminal(
        self,
        run_id: str,
        *,
        status: str,
        ended_at: float,
        error_code: str | None = None,
        error_message: str | None = None,
        error_retryable: bool | None = None,
        input_tokens: int | None = None,
        output_tokens: int | None = None,
        total_tokens: int | None = None,
        usage_known: bool | None = None,
        retry_safe: bool | None = None,
        effects_json: str | None = None,
    ) -> None:
        await asyncio.to_thread(
            self._execute,
            "UPDATE runs SET status=?, ended_at=?, error_code=?, error_message=?, "
            "error_retryable=?, input_tokens=?, output_tokens=?, total_tokens=?, "
            "usage_known=?, retry_safe=?, effects_json=? WHERE run_id=?",
            (
                status,
                ended_at,
                error_code,
                error_message,
                None if error_retryable is None else int(error_retryable),
                input_tokens,
                output_tokens,
                total_tokens,
                None if usage_known is None else int(usage_known),
                None if retry_safe is None else int(retry_safe),
                effects_json,
                run_id,
            ),
        )

    async def delete_runs_for_session(
        self, session_id: str, owner_id: str
    ) -> None:
        await asyncio.to_thread(
            self._execute,
            "DELETE FROM runs WHERE session_id=? AND owner_id=?",
            (session_id, owner_id),
        )

    # -- idempotency ----------------------------------------------
    async def get_idempotency(
        self, owner_id: str, operation: str, scope_id: str, key: str
    ) -> dict[str, Any] | None:
        rows = await asyncio.to_thread(
            self._query,
            "SELECT * FROM idempotency WHERE owner_id=? AND operation=? AND "
            "scope_id=? AND idempotency_key=?",
            (owner_id, operation, scope_id, key),
        )
        return rows[0] if rows else None

    async def insert_idempotency(
        self,
        *,
        owner_id: str,
        operation: str,
        scope_id: str,
        key: str,
        request_digest: str,
        created_at: float,
        expires_at: float,
    ) -> None:
        await asyncio.to_thread(
            self._execute,
            "INSERT INTO idempotency(owner_id, operation, scope_id, idempotency_key, "
            "request_digest, state, response_status, response_body, result_reference, "
            "created_at, expires_at) VALUES (?,?,?,?,?,'in_progress',NULL,NULL,NULL,?,?)",
            (owner_id, operation, scope_id, key, request_digest, created_at, expires_at),
        )

    async def complete_idempotency(
        self,
        *,
        owner_id: str,
        operation: str,
        scope_id: str,
        key: str,
        response_status: int,
        response_body: Mapping[str, Any],
        result_reference: str | None,
    ) -> None:
        await asyncio.to_thread(
            self._execute,
            "UPDATE idempotency SET state='completed', response_status=?, "
            "response_body=?, result_reference=? WHERE owner_id=? AND operation=? "
            "AND scope_id=? AND idempotency_key=?",
            (
                response_status,
                json.dumps(response_body, ensure_ascii=False),
                result_reference,
                owner_id,
                operation,
                scope_id,
                key,
            ),
        )

    async def delete_idempotency_for_scope(
        self, owner_id: str, scope_id: str
    ) -> None:
        await asyncio.to_thread(
            self._execute,
            "DELETE FROM idempotency WHERE owner_id=? AND scope_id=?",
            (owner_id, scope_id),
        )

    async def purge_expired_idempotency(self, now: float) -> int:
        cursor = await asyncio.to_thread(
            self._execute, "DELETE FROM idempotency WHERE expires_at < ?", (now,)
        )
        return cursor.rowcount

    # -- approvals -------------------------------------------------
    async def insert_approval(self, record: Mapping[str, Any]) -> None:
        await asyncio.to_thread(
            self._execute,
            "INSERT INTO approvals(approval_id, owner_id, session_id, root_run_id, "
            "source_run_id, tool_call_id, tool_name, arguments_json, arguments_digest, "
            "reason, effect_capability, summary, status, decision, decision_reason, "
            "terminal_reason, created_at, expires_at, resolved_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,NULL,NULL,NULL,?,?,NULL)",
            (
                record["approval_id"],
                record["owner_id"],
                record["session_id"],
                record["root_run_id"],
                record["source_run_id"],
                record["tool_call_id"],
                record["tool_name"],
                json.dumps(record["arguments"], ensure_ascii=False),
                record["arguments_digest"],
                record.get("reason"),
                record.get("effect_capability"),
                record["summary"],
                record["status"],
                record["created_at"],
                record["expires_at"],
            ),
        )

    async def get_approval(
        self, approval_id: str, owner_id: str
    ) -> dict[str, Any] | None:
        rows = await asyncio.to_thread(
            self._query,
            "SELECT * FROM approvals WHERE approval_id=? AND owner_id=?",
            (approval_id, owner_id),
        )
        return rows[0] if rows else None

    async def list_pending_approvals(self, owner_id: str) -> list[dict[str, Any]]:
        return await asyncio.to_thread(
            self._query,
            "SELECT * FROM approvals WHERE owner_id=? AND status='pending'",
            (owner_id,),
        )

    async def list_pending_approvals_for_run(
        self, root_run_id: str, owner_id: str
    ) -> list[dict[str, Any]]:
        return await asyncio.to_thread(
            self._query,
            "SELECT * FROM approvals WHERE owner_id=? AND root_run_id=? AND status='pending'",
            (owner_id, root_run_id),
        )

    async def terminate_approval(
        self,
        approval_id: str,
        owner_id: str,
        *,
        status: str,
        decision: str | None,
        decision_reason: str | None,
        terminal_reason: str | None,
        resolved_at: float,
    ) -> dict[str, Any] | None:
        """CAS a pending approval into a terminal state; None if it was already terminal."""
        cursor = await asyncio.to_thread(
            self._execute,
            "UPDATE approvals SET status=?, decision=?, decision_reason=?, "
            "terminal_reason=?, resolved_at=? WHERE approval_id=? AND owner_id=? "
            "AND status='pending'",
            (
                status,
                decision,
                decision_reason,
                terminal_reason,
                resolved_at,
                approval_id,
                owner_id,
            ),
        )
        if cursor.rowcount == 0:
            return None
        rows = await asyncio.to_thread(
            self._query,
            "SELECT * FROM approvals WHERE approval_id=? AND owner_id=?",
            (approval_id, owner_id),
        )
        return rows[0] if rows else None

    async def delete_approvals_for_session(
        self, session_id: str, owner_id: str
    ) -> None:
        await asyncio.to_thread(
            self._execute,
            "DELETE FROM approvals WHERE session_id=? AND owner_id=?",
            (session_id, owner_id),
        )

    async def list_approvals_for_session(
        self, session_id: str, owner_id: str
    ) -> list[dict[str, Any]]:
        return await asyncio.to_thread(
            self._query,
            "SELECT * FROM approvals WHERE session_id=? AND owner_id=?",
            (session_id, owner_id),
        )

    # -- artifacts (docs §5.1) -----------------------------------------
    async def insert_artifact(self, record: Mapping[str, Any]) -> None:
        await asyncio.to_thread(
            self._execute,
            "INSERT OR IGNORE INTO artifacts(artifact_id, owner_id, session_id, "
            "media_type, size, digest, path, filename, created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (
                record["artifact_id"],
                record["owner_id"],
                record.get("session_id"),
                record.get("media_type"),
                record["size"],
                record.get("digest"),
                record.get("path"),
                record.get("filename"),
                record["created_at"],
            ),
        )

    async def get_artifact(
        self, artifact_id: str, owner_id: str
    ) -> dict[str, Any] | None:
        rows = await asyncio.to_thread(
            self._query,
            "SELECT * FROM artifacts WHERE artifact_id=? AND owner_id=?",
            (artifact_id, owner_id),
        )
        return rows[0] if rows else None

    async def delete_artifact(self, artifact_id: str, owner_id: str) -> None:
        await asyncio.to_thread(
            self._execute,
            "DELETE FROM artifacts WHERE artifact_id=? AND owner_id=?",
            (artifact_id, owner_id),
        )

    async def list_artifacts(self, owner_id: str) -> list[dict[str, Any]]:
        return await asyncio.to_thread(
            self._query,
            "SELECT * FROM artifacts WHERE owner_id=? ORDER BY created_at ASC",
            (owner_id,),
        )

    async def add_artifact_reference(
        self, artifact_id: str, owner_id: str, session_id: str, created_at: float
    ) -> None:
        await asyncio.to_thread(
            self._execute,
            "INSERT INTO artifact_references(artifact_id, owner_id, session_id, "
            "created_at) VALUES (?,?,?,?)",
            (artifact_id, owner_id, session_id, created_at),
        )

    async def count_live_artifact_references(
        self, artifact_id: str, owner_id: str
    ) -> int:
        """References from Sessions that still exist (docs §5.1 delete rule)."""
        rows = await asyncio.to_thread(
            self._query,
            "SELECT COUNT(*) AS n FROM artifact_references AS r "
            "WHERE r.artifact_id=? AND r.owner_id=? "
            "AND EXISTS (SELECT 1 FROM sessions AS s WHERE s.session_id=r.session_id)",
            (artifact_id, owner_id),
        )
        return int(rows[0]["n"]) if rows else 0

    async def delete_artifact_references_for_session(
        self, session_id: str, owner_id: str
    ) -> None:
        await asyncio.to_thread(
            self._execute,
            "DELETE FROM artifact_references WHERE session_id=? AND owner_id=?",
            (session_id, owner_id),
        )

    # -- robot control authority (docs §1.5 / §4.6) --------------------
    async def get_robot_authority(
        self, robot_id: str, owner_id: str
    ) -> dict[str, Any] | None:
        rows = await asyncio.to_thread(
            self._query,
            "SELECT * FROM robot_authorities WHERE robot_id=? AND owner_id=?",
            (robot_id, owner_id),
        )
        return rows[0] if rows else None

    async def acquire_robot_authority(
        self,
        *,
        robot_id: str,
        owner_id: str,
        authority_id: str,
        holder_id: str,
        mode: str,
        now: float,
        expires_at: float,
    ) -> dict[str, Any] | None:
        """Atomically claim authority.

        Returns the existing live row when another unexpired holder owns the
        robot, otherwise writes the new authority and returns ``None``.
        """
        return await asyncio.to_thread(
            self._acquire_robot_authority,
            robot_id,
            owner_id,
            authority_id,
            holder_id,
            mode,
            now,
            expires_at,
        )

    def _acquire_robot_authority(
        self,
        robot_id: str,
        owner_id: str,
        authority_id: str,
        holder_id: str,
        mode: str,
        now: float,
        expires_at: float,
    ) -> dict[str, Any] | None:
        if self._connection is None:
            raise RuntimeError("ApplicationStore is not open.")
        with self._lock:
            row = self._connection.execute(
                "SELECT * FROM robot_authorities WHERE robot_id=? AND owner_id=?",
                (robot_id, owner_id),
            ).fetchone()
            if row is not None and row["expires_at"] is not None:
                if float(row["expires_at"]) > now:
                    return {key: row[key] for key in row.keys()}
            self._connection.execute(
                "INSERT INTO robot_authorities(robot_id, owner_id, authority_id, "
                "holder_id, mode, acquired_at, expires_at) VALUES (?,?,?,?,?,?,?) "
                "ON CONFLICT(robot_id) DO UPDATE SET owner_id=excluded.owner_id, "
                "authority_id=excluded.authority_id, holder_id=excluded.holder_id, "
                "mode=excluded.mode, acquired_at=excluded.acquired_at, "
                "expires_at=excluded.expires_at",
                (robot_id, owner_id, authority_id, holder_id, mode, now, expires_at),
            )
            self._connection.commit()
            return None

    async def release_robot_authority(
        self, robot_id: str, owner_id: str, authority_id: str | None = None
    ) -> bool:
        if authority_id is None:
            cursor = await asyncio.to_thread(
                self._execute,
                "DELETE FROM robot_authorities WHERE robot_id=? AND owner_id=?",
                (robot_id, owner_id),
            )
        else:
            cursor = await asyncio.to_thread(
                self._execute,
                "DELETE FROM robot_authorities WHERE robot_id=? AND owner_id=? "
                "AND authority_id=?",
                (robot_id, owner_id, authority_id),
            )
        return cursor.rowcount > 0


def decode_effects(value: object) -> list[dict[str, Any]]:
    if not value:
        return []
    if isinstance(value, bytes):  # pragma: no cover - defensive
        value = value.decode("utf-8")
    try:
        decoded = json.loads(str(value))
    except (TypeError, ValueError):  # pragma: no cover - defensive
        return []
    return decoded if isinstance(decoded, list) else []


def rows_to_iter(rows: Iterable[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
    return list(rows)


__all__ = [
    "ApplicationStore",
    "NONTERMINAL_RUN_STATUSES",
    "TERMINAL_RUN_STATUSES",
    "decode_effects",
]
