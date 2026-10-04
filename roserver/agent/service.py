"""AgentService: Product session/run/approval sagas and run projection.

Docs §3.1-§3.15, §2.10-§2.13.  Everything here runs on the single owning event
loop; roboagent objects never leave it and SQLite work is offloaded by the
ApplicationStore.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from roboagent import Agent, Session
from roboagent.agent import (
    LocalSessionRepository,
    Run,
    RunResult,
    SessionBusyError,
    SessionNotFoundError,
)
from roboagent.message import ArtifactReferenceContent, UserMessage
from roboagent.runtime import (
    EventSubscription,
    EventSubscriptionConfig,
    RunStatus,
)

from ..artifact.service import ArtifactService
from ..config import Settings
from ..errors import ProductError, map_runtime_error_code, public_message_for
from ..store.application import ApplicationStore
from .approval import ProductApprovalProvider, approval_resolved_data
from .schema import (
    RUN_STATUS_CANCELLED,
    RUN_STATUS_COMPLETED,
    RUN_STATUS_CREATED,
    RUN_STATUS_FAILED,
    RUN_STATUS_INTERRUPTED,
    RUN_STATUS_RUNNING,
    TERMINAL_RUN_STATUSES,
    active_run_summary,
    approval_info,
    effect_summary,
    product_message,
    rfc3339,
    run_info,
    session_status,
    session_summary,
    usage_dict,
)
from .stream import PublicEventBuffer

_RUNTIME_TERMINAL = frozenset({"run.completed", "run.failed", "run.cancelled"})
_CHILD_TERMINAL = {
    "child_run.completed": RUN_STATUS_COMPLETED,
    "child_run.failed": RUN_STATUS_FAILED,
    "child_run.cancelled": RUN_STATUS_CANCELLED,
}
_STREAM_TERMINAL = {
    "run.completed": RUN_STATUS_COMPLETED,
    "run.failed": RUN_STATUS_FAILED,
    "run.cancelled": RUN_STATUS_CANCELLED,
    "run.interrupted": RUN_STATUS_INTERRUPTED,
}


class RunProjection:
    """Ephemeral in-process Product state for one root run (docs §2.10)."""

    def __init__(self, root_run_id: str, session_id: str) -> None:
        self.root_run_id = root_run_id
        self.session_id = session_id
        self.status = RUN_STATUS_CREATED
        self.last_sequence = 0
        self.assistant_messages: list[dict[str, Any]] = []
        self.tools: list[dict[str, Any]] = []
        self.approvals: list[dict[str, Any]] = []
        self.child_runs: list[dict[str, Any]] = []
        self.scope_to_run: dict[str, str] = {}
        self._assistant_index: dict[str, dict[str, Any]] = {}
        self._tool_index: dict[str, dict[str, Any]] = {}

    # -- assistant -------------------------------------------------
    def assistant_start(self, message_id: str, source_run_id: str) -> dict[str, Any]:
        entry = self._assistant_index.get(message_id)
        if entry is None:
            entry = {
                "message_id": message_id,
                "source_run_id": source_run_id,
                "blocks": [],
                "state": "streaming",
            }
            self._assistant_index[message_id] = entry
            self.assistant_messages.append(entry)
        return entry

    def assistant_append_text(self, message_id: str, source_run_id: str, text: str) -> int:
        entry = self.assistant_start(message_id, source_run_id)
        blocks = entry["blocks"]
        if not blocks or blocks[-1].get("type") != "text":
            blocks.append({"type": "text", "text": ""})
        blocks[-1]["text"] += text
        return len(blocks) - 1

    def assistant_complete(self, message_id: str) -> bool:
        entry = self._assistant_index.get(message_id)
        if entry is None or entry["state"] != "streaming":
            return False
        entry["state"] = "completed"
        return True

    def assistant_abort(self, message_id: str) -> bool:
        entry = self._assistant_index.get(message_id)
        if entry is None or entry["state"] != "streaming":
            return False
        entry["state"] = "aborted"
        return True

    def open_assistants(self, source_run_id: str | None = None) -> list[dict[str, Any]]:
        return [
            entry
            for entry in self.assistant_messages
            if entry["state"] == "streaming"
            and (source_run_id is None or entry["source_run_id"] == source_run_id)
        ]

    # -- tools -----------------------------------------------------
    def tool(self, message_id: str | None, tool_call_id: str, tool_name: str) -> dict[str, Any]:
        entry = self._tool_index.get(tool_call_id)
        if entry is None:
            entry = {
                "message_id": message_id,
                "tool_call_id": tool_call_id,
                "tool_name": tool_name,
                "execution_status": "pending",
                "effect_status": "unknown",
                "certainty": "unknown",
            }
            self._tool_index[tool_call_id] = entry
            self.tools.append(entry)
        elif tool_name and not entry["tool_name"]:
            entry["tool_name"] = tool_name
        return entry

    def tool_execution(self, tool_call_id: str, execution_status: str) -> dict[str, Any]:
        entry = self.tool(None, tool_call_id, "")
        entry["execution_status"] = execution_status
        return entry

    def tool_effect(
        self, tool_call_id: str, effect_status: str, certainty: str
    ) -> dict[str, Any]:
        entry = self.tool(None, tool_call_id, "")
        entry["effect_status"] = effect_status
        entry["certainty"] = certainty
        return entry

    # -- children --------------------------------------------------
    def child(self, run_id: str, parent_run_id: str | None, agent_tool_name: str | None) -> dict[str, Any]:
        for entry in self.child_runs:
            if entry["run_id"] == run_id:
                return entry
        entry = {
            "run_id": run_id,
            "parent_run_id": parent_run_id,
            "status": RUN_STATUS_RUNNING,
            "agent_tool_name": agent_tool_name,
        }
        self.child_runs.append(entry)
        return entry

    def child_status(self, run_id: str, status: str) -> None:
        for entry in self.child_runs:
            if entry["run_id"] == run_id:
                entry["status"] = status

    # -- approvals -------------------------------------------------
    def approval(self, record: Mapping[str, Any]) -> None:
        for entry in self.approvals:
            if entry["approval_id"] == record["approval_id"]:
                entry["status"] = record.get("status")
                entry["expires_at"] = rfc3339(record.get("expires_at"))
                return
        self.approvals.append(
            {
                "approval_id": record.get("approval_id"),
                "tool_call_id": record.get("tool_call_id"),
                "root_run_id": record.get("root_run_id"),
                "status": record.get("status"),
                "expires_at": rfc3339(record.get("expires_at")),
            }
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "root_run_id": self.root_run_id,
            "session_id": self.session_id,
            "status": self.status,
            "last_sequence": self.last_sequence,
            "assistant_messages": self.assistant_messages,
            "tools": self.tools,
            "approvals": self.approvals,
            "child_runs": self.child_runs,
        }


@dataclass
class ActiveRun:
    root_run_id: str
    session_id: str
    session: Session
    run: Run
    buffer: PublicEventBuffer
    projection: RunProjection
    pump: asyncio.Task[None]
    done: asyncio.Event = field(default_factory=asyncio.Event)


class AgentService:
    def __init__(
        self,
        *,
        settings: Settings,
        agent: Agent,
        repository: LocalSessionRepository,
        store: ApplicationStore,
        approval_provider: ProductApprovalProvider | None = None,
        artifacts: ArtifactService | None = None,
        artifact_reader: object | None = None,
        artifact_destination: object | None = None,
        media_resolver: object | None = None,
        workspace: object | None = None,
        result_materializer: object | None = None,
    ) -> None:
        self.settings = settings
        self.agent = agent
        self.repository = repository
        self.store = store
        self.approval_provider = approval_provider
        self.artifacts = artifacts
        self.artifact_reader = artifact_reader
        self.artifact_destination = artifact_destination
        self.media_resolver = media_resolver
        self.workspace = workspace
        self.result_materializer = result_materializer
        if approval_provider is not None:
            approval_provider.set_emitter(self._emit_approval_event)
        self._sessions: dict[str, Session] = {}
        self._active: dict[str, ActiveRun] = {}
        self._buffers: dict[str, PublicEventBuffer] = {}
        self._projections: dict[str, RunProjection] = {}
        self._locks: dict[str, asyncio.Lock] = {}

    def _session_kwargs(self) -> dict[str, object]:
        """Extension points injected into every Session (docs §5.2)."""
        kwargs: dict[str, object] = {}
        if self.workspace is not None:
            kwargs["workspace"] = self.workspace
        if self.result_materializer is not None:
            kwargs["result_materializer"] = self.result_materializer
        if self.artifact_reader is not None:
            kwargs["artifact_reader"] = self.artifact_reader
        if self.artifact_destination is not None:
            kwargs["artifact_destination"] = self.artifact_destination
        return kwargs

    async def record_artifact_references(
        self, session_id: str, message: UserMessage
    ) -> None:
        """Remember that a Session's input uses these artifacts (docs §5.1)."""
        if self.artifacts is None:
            return
        for block in message.content:
            if isinstance(block, ArtifactReferenceContent):
                await self.artifacts.reference(block.digest, session_id)

    # =================================================================
    # lifecycle
    # =================================================================
    async def startup(self) -> None:
        await self.store.open()
        await self.store.purge_expired_idempotency(time.time())
        await self._reconcile()

    async def shutdown(self) -> None:
        if self.approval_provider is not None:
            await self.approval_provider.shutdown()
        actives = list(self._active.values())
        # docs §4.6: cancel active Runs and let them settle before closing
        # resources, so no runtime task outlives the application lifespan.
        for active in actives:
            try:
                active.run.cancel()
            except Exception:  # pragma: no cover - best effort shutdown
                pass
        for active in actives:
            try:
                await asyncio.wait_for(
                    active.run.result(), timeout=self.settings.cancel_settle_timeout
                )
            except BaseException:  # pragma: no cover - best effort shutdown
                pass
        tasks = [active.pump for active in actives]
        for task in tasks:
            if not task.done():
                task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._active.clear()
        for session in tuple(self._sessions.values()):
            try:
                await session.close()
            except Exception:  # pragma: no cover - best effort shutdown
                pass
        self._sessions.clear()
        await self.store.close()

    def _lock_for(self, session_id: str) -> asyncio.Lock:
        lock = self._locks.get(session_id)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[session_id] = lock
        return lock

    # =================================================================
    # startup reconciliation (docs §2.12 / §3.2 / §3.11)
    # =================================================================
    async def _reconcile(self) -> None:
        now = time.time()
        for row in await self.store.list_sessions_by_state("provisioning"):
            snapshot = await self.repository.load(row["session_id"])
            if snapshot is not None:
                await self.store.set_session_state(row["session_id"], "ready", now)
            else:
                await self.store.delete_session(row["session_id"], self.settings.owner_id)
        for row in await self.store.list_sessions_by_state("deleting"):
            await self._finish_delete(row["session_id"])
        for record in await self.store.list_nonterminal_runs(self.settings.owner_id):
            await self._interrupt_run(record, now)
        if self.approval_provider is not None:
            for record in await self.store.list_pending_approvals(self.settings.owner_id):
                updated = await self.approval_provider.store.terminate_approval(
                    record["approval_id"],
                    self.settings.owner_id,
                    status="cancelled",
                    decision=None,
                    decision_reason=None,
                    terminal_reason="server_restarted",
                    resolved_at=now,
                )
                if updated is not None:
                    buffer = self._ensure_buffer(
                        str(record["root_run_id"]), str(record["session_id"])
                    )
                    self._publish_event(
                        buffer,
                        session_id=str(record["session_id"]),
                        root_run_id=str(record["root_run_id"]),
                        source_run_id=str(record["root_run_id"]),
                        parent_run_id=None,
                        event_type="approval.resolved",
                        data=approval_resolved_data(updated),
                        timestamp=now,
                    )

    async def _interrupt_run(self, record: Mapping[str, Any], now: float) -> None:
        run_id = str(record["run_id"])
        session_id = str(record["session_id"])
        root_run_id = str(record["root_run_id"])
        is_root = record.get("parent_run_id") is None
        await self.store.update_run_terminal(
            run_id, status=RUN_STATUS_INTERRUPTED, ended_at=now
        )
        if not is_root:
            return
        discarded = await self._clear_runtime_pending(session_id)
        await self.store.set_session_active_run(session_id, None, now)
        buffer = self._ensure_buffer(root_run_id, session_id)
        self._publish_event(
            buffer,
            session_id=session_id,
            root_run_id=root_run_id,
            source_run_id=run_id,
            parent_run_id=None,
            event_type="run.interrupted",
            data={
                "run_id": run_id,
                "reason": "server_restarted",
                "discarded_pending_count": discarded,
            },
            timestamp=now,
        )

    async def _clear_runtime_pending(self, session_id: str) -> int:
        try:
            session = await Session.open(
                agent=self.agent,
                session_id=session_id,
                repository=self.repository,
                **self._session_kwargs(),
            )
        except SessionNotFoundError:
            return 0
        try:
            receipts = await session.clear_pending()
            return len(receipts)
        finally:
            await session.close()

    # =================================================================
    # sessions
    # =================================================================
    async def create_session(self, *, title: str | None = None) -> dict[str, Any]:
        now = time.time()
        session_id = uuid.uuid4().hex
        resolved_title = (title or "New session").strip() or "New session"
        await self.store.create_session(
            session_id=session_id,
            owner_id=self.settings.owner_id,
            title=resolved_title,
            internal_state="provisioning",
            created_at=now,
            updated_at=now,
        )
        try:
            session = self.agent.new_session(
                session_id=session_id,
                repository=self.repository,
                **self._session_kwargs(),
            )
            await session.persist()
        except Exception:
            await self.store.delete_session(session_id, self.settings.owner_id)
            raise
        self._sessions[session_id] = session
        await self.store.set_session_state(session_id, "ready", now)
        return await self.get_session(session_id)

    async def list_sessions(
        self, *, limit: int, cursor: tuple[float, str] | None
    ) -> dict[str, Any]:
        rows = await self.store.list_sessions(
            self.settings.owner_id, limit=limit, cursor=cursor
        )
        has_more = len(rows) > limit
        page = rows[:limit]
        next_cursor = None
        if has_more and page:
            last = page[-1]
            next_cursor = encode_cursor(float(last["updated_at"]), str(last["session_id"]))
        return {
            "items": [session_summary(row) for row in page],
            "next_cursor": next_cursor,
        }

    async def get_session(self, session_id: str) -> dict[str, Any]:
        row = await self.store.get_session(session_id, self.settings.owner_id)
        if row is None:
            raise ProductError("session_not_found", "Session does not exist.")
        snapshot = await self._load_snapshot(session_id)
        active_record = None
        active_id = row.get("active_root_run_id")
        if active_id:
            active_record = await self.store.get_run(str(active_id), self.settings.owner_id)
        messages = (
            [product_message(message) for message in snapshot.messages]
            if snapshot is not None
            else []
        )
        return {
            "session_id": row["session_id"],
            "title": row["title"],
            "status": session_status(row),
            "active_root_run_id": active_id,
            "created_at": rfc3339(row["created_at"]),
            "updated_at": rfc3339(row["updated_at"]),
            "active_run": active_run_summary(active_record),
            "messages": messages,
        }

    async def update_session(
        self, session_id: str, *, title: str
    ) -> dict[str, Any]:
        row = await self.store.get_session(session_id, self.settings.owner_id)
        if row is None:
            raise ProductError("session_not_found", "Session does not exist.")
        if row.get("internal_state") == "deleting":
            raise ProductError("session_deleting", "Session is being deleted.")
        resolved = title.strip()
        if not resolved:
            raise ProductError("invalid_input", "title must not be blank.")
        if len(resolved) > 512:
            raise ProductError("invalid_input", "title is too long.")
        await self.store.update_session_title(
            session_id, self.settings.owner_id, resolved, time.time()
        )
        return await self.get_session(session_id)

    async def delete_session(self, session_id: str) -> None:
        row = await self.store.get_session(session_id, self.settings.owner_id)
        if row is None:
            return
        async with self._lock_for(session_id):
            row = await self.store.get_session(session_id, self.settings.owner_id)
            if row is None:
                return
            if row.get("active_root_run_id"):
                raise ProductError("session_busy", "Session has an active Run.")
            await self.store.set_session_state(session_id, "deleting", time.time())
            await self._finish_delete(session_id)

    async def _finish_delete(self, session_id: str) -> None:
        if self.approval_provider is not None:
            await self.approval_provider.cancel_pending_for_session(
                session_id, terminal_reason="session_deleted"
            )
        session = self._sessions.pop(session_id, None)
        if session is not None and not session.closed:
            try:
                await session.close()
            except SessionBusyError:  # pragma: no cover - busy is rejected earlier
                pass
        try:
            loaded = await Session.open(
                agent=self.agent,
                session_id=session_id,
                repository=self.repository,
                **self._session_kwargs(),
            )
        except SessionNotFoundError:
            loaded = None
        if loaded is not None:
            await loaded.delete()
        owner = self.settings.owner_id
        await self.store.delete_runs_for_session(session_id, owner)
        await self.store.delete_approvals_for_session(session_id, owner)
        await self.store.delete_idempotency_for_scope(owner, session_id)
        await self.store.delete_session(session_id, owner)
        self._locks.pop(session_id, None)

    # =================================================================
    # runs
    # =================================================================
    async def start_run(
        self,
        session_id: str,
        message: UserMessage,
        *,
        client_message_id: str | None,
        request_id: str,
    ) -> tuple[dict[str, Any], str]:
        row = await self.store.get_session(session_id, self.settings.owner_id)
        if row is None:
            raise ProductError("session_not_found", "Session does not exist.")
        async with self._lock_for(session_id):
            row = await self.store.get_session(session_id, self.settings.owner_id)
            if row is None:
                raise ProductError("session_not_found", "Session does not exist.")
            if row.get("internal_state") == "deleting":
                raise ProductError("session_deleting", "Session is being deleted.")
            session = await self._open_session(session_id)
            if session.active_run_id is not None or row.get("active_root_run_id"):
                raise ProductError("session_busy", "Session already has an active Run.")
            body = await self._start_run_locked(
                session, row, message, client_message_id, request_id
            )
            return body, str(body["run_id"])

    async def follow_up(
        self, session_id: str, message: UserMessage, *, request_id: str
    ) -> tuple[dict[str, Any], str]:
        row = await self.store.get_session(session_id, self.settings.owner_id)
        if row is None:
            raise ProductError("session_not_found", "Session does not exist.")
        async with self._lock_for(session_id):
            row = await self.store.get_session(session_id, self.settings.owner_id)
            if row is None:
                raise ProductError("session_not_found", "Session does not exist.")
            if row.get("internal_state") == "deleting":
                raise ProductError("session_deleting", "Session is being deleted.")
            session = await self._open_session(session_id)
            await self._check_pending_limits(session)
            receipt = await session.follow_up(message)
            run_id = session.active_run_id
            if run_id is None:
                body = await self._start_run_locked(
                    session, row, None, None, request_id
                )
                run_id = str(body["run_id"])
            return (
                input_receipt_body(receipt, message, run_id, "follow_up"),
                receipt.input_id,
            )

    async def steer(
        self, run_id: str, message: UserMessage, *, request_id: str
    ) -> tuple[dict[str, Any], str]:
        record = await self.store.get_run(run_id, self.settings.owner_id)
        if record is None:
            raise ProductError("run_not_found", "Run does not exist.")
        root_run_id = str(record["root_run_id"])
        active = self._active.get(root_run_id)
        if active is None or record.get("status") not in {RUN_STATUS_CREATED, RUN_STATUS_RUNNING}:
            raise ProductError("session_busy", "Run is not active.")
        session = active.session
        await self._check_pending_limits(session)
        receipt = await session.steer(message)
        await self.store.touch_session(str(record["session_id"]), time.time())
        return (
            input_receipt_body(receipt, message, run_id, "steer"),
            receipt.input_id,
        )

    async def _check_pending_limits(self, session: Session) -> None:
        pending = await session.pending_inputs()
        if len(pending) >= self.settings.max_pending_inputs:
            raise ProductError("pending_queue_full", "Pending input queue is full.")
        size = 0
        for item in pending:
            size += len(str(item.message.content))
        if size > self.settings.max_pending_input_bytes:
            raise ProductError("pending_queue_full", "Pending input queue is full.")

    async def _start_run_locked(
        self,
        session: Session,
        row: Mapping[str, Any],
        message: UserMessage | None,
        client_message_id: str | None,
        request_id: str,
    ) -> dict[str, Any]:
        now = time.time()
        session_id = str(row["session_id"])
        try:
            run = session.start(message)
        except SessionBusyError as exc:
            raise ProductError("session_busy", "Session already has an active Run.") from exc
        root_run_id = run.run_id
        buffer = PublicEventBuffer(
            root_run_id,
            session_id,
            max_events=self.settings.max_replay_events,
            max_bytes=self.settings.max_replay_bytes,
            retention=self.settings.replay_retention,
        )
        projection = RunProjection(root_run_id, session_id)
        self._buffers[root_run_id] = buffer
        self._projections[root_run_id] = projection
        subscription = run.subscribe(
            EventSubscriptionConfig(replay_limit=256, max_queue_size=1024)
        )
        await self.store.insert_run(
            {
                "run_id": root_run_id,
                "owner_id": self.settings.owner_id,
                "session_id": session_id,
                "root_run_id": root_run_id,
                "parent_run_id": None,
                "source_run_id": root_run_id,
                "agent_tool_name": None,
                "status": RUN_STATUS_CREATED,
                "request_id": request_id,
                "created_at": now,
                "started_at": None,
                "ended_at": None,
                "effects_json": None,
            }
        )
        await self.store.set_session_active_run(session_id, root_run_id, now)
        await self.store.touch_session(session_id, now)
        active = ActiveRun(
            root_run_id=root_run_id,
            session_id=session_id,
            session=session,
            run=run,
            buffer=buffer,
            projection=projection,
            pump=asyncio.create_task(
                self._drive_run(
                    root_run_id=root_run_id,
                    session=session,
                    run=run,
                    buffer=buffer,
                    projection=projection,
                    subscription=subscription,
                    request_id=request_id,
                )
            ),
        )
        self._active[root_run_id] = active
        body = {
            "run_id": root_run_id,
            "root_run_id": root_run_id,
            "session_id": session_id,
            "status": RUN_STATUS_CREATED,
            "client_message_id": client_message_id,
            "user_message_id": message.message_id if message is not None else None,
            "created_at": rfc3339(now),
        }
        return body

    async def get_run(self, run_id: str) -> dict[str, Any]:
        record = await self.store.get_run(run_id, self.settings.owner_id)
        if record is None:
            raise ProductError("run_not_found", "Run does not exist.")
        return run_info(record)

    async def get_projection(self, run_id: str) -> dict[str, Any]:
        record = await self.store.get_run(run_id, self.settings.owner_id)
        if record is None:
            raise ProductError("run_not_found", "Run does not exist.")
        self._evict_expired()
        projection = self._projections.get(str(record["root_run_id"]))
        if projection is None:
            raise ProductError(
                "projection_not_available", "RunProjection is no longer available."
            )
        return projection.to_dict()

    async def cancel_run(self, run_id: str) -> dict[str, Any]:
        record = await self.store.get_run(run_id, self.settings.owner_id)
        if record is None:
            raise ProductError("run_not_found", "Run does not exist.")
        if record.get("status") in TERMINAL_RUN_STATUSES:
            return run_info(record)
        root_run_id = str(record["root_run_id"])
        active = self._active.get(root_run_id)
        if active is None:
            await self._interrupt_run(record, time.time())
            refreshed = await self.store.get_run(run_id, self.settings.owner_id)
            return run_info(refreshed or record)
        active.run.cancel()
        try:
            await asyncio.wait_for(
                asyncio.shield(active.done.wait()),
                timeout=self.settings.cancel_settle_timeout,
            )
        except (asyncio.TimeoutError, asyncio.CancelledError):
            pass
        refreshed = await self.store.get_run(run_id, self.settings.owner_id)
        return run_info(refreshed or record)

    # =================================================================
    # approvals
    # =================================================================
    async def get_approval(self, approval_id: str) -> dict[str, Any]:
        record = await self.store.get_approval(approval_id, self.settings.owner_id)
        if record is None:
            raise ProductError("approval_not_found", "Approval does not exist.")
        return approval_info(record)

    async def resolve_approval(
        self,
        approval_id: str,
        *,
        decision: str,
        arguments_digest: str,
        reason: str | None,
    ) -> dict[str, Any]:
        record = await self.store.get_approval(approval_id, self.settings.owner_id)
        if record is None:
            raise ProductError("approval_not_found", "Approval does not exist.")
        if record.get("arguments_digest") != arguments_digest:
            raise ProductError(
                "approval_digest_mismatch",
                "arguments_digest does not match the approval request.",
            )
        desired = "approved" if decision == "approve" else "denied"
        now = time.time()
        status = str(record.get("status"))
        if status != "pending":
            if status == "expired":
                raise ProductError("approval_expired", "Approval has expired.")
            if status == "cancelled":
                raise ProductError(
                    "approval_already_resolved", "Approval has already been resolved."
                )
            if record.get("decision") == decision:
                return approval_info(record)
            raise ProductError(
                "approval_already_resolved", "Approval has already been resolved."
            )
        if now > float(record["expires_at"]):
            updated = await self.store.terminate_approval(
                approval_id,
                self.settings.owner_id,
                status="expired",
                decision=None,
                decision_reason=None,
                terminal_reason="approval_timeout",
                resolved_at=now,
            )
            if updated is not None:
                await self._emit_approval_record_event(
                    updated, "approval.resolved", approval_resolved_data(updated)
                )
            raise ProductError("approval_expired", "Approval has expired.")
        updated = await self.store.terminate_approval(
            approval_id,
            self.settings.owner_id,
            status=desired,
            decision=decision,
            decision_reason=reason,
            terminal_reason=None,
            resolved_at=now,
        )
        if updated is None:
            raise ProductError(
                "approval_already_resolved", "Approval has already been resolved."
            )
        await self._emit_approval_record_event(
            updated, "approval.resolved", approval_resolved_data(updated)
        )
        if self.approval_provider is not None:
            from roboagent.tool import ApprovalDecision

            self.approval_provider.notify(
                approval_id,
                ApprovalDecision.APPROVE if decision == "approve" else ApprovalDecision.REJECT,
            )
        return approval_info(updated)

    async def _emit_approval_event(
        self, record: Mapping[str, Any], event_type: str, data: dict[str, Any]
    ) -> None:
        await self._emit_approval_record_event(record, event_type, data)

    async def _emit_approval_record_event(
        self, record: Mapping[str, Any], event_type: str, data: dict[str, Any]
    ) -> None:
        root_run_id = str(record.get("root_run_id") or "")
        session_id = str(record.get("session_id") or "")
        if not root_run_id:
            return
        self._evict_expired()
        buffer = self._buffers.get(root_run_id)
        if buffer is None:
            if event_type == "approval.resolved":
                return
            buffer = self._ensure_buffer(root_run_id, session_id)
        self._publish_event(
            buffer,
            session_id=session_id,
            root_run_id=root_run_id,
            source_run_id=str(record.get("source_run_id") or root_run_id),
            parent_run_id=None,
            event_type=event_type,
            data=data,
            timestamp=time.time(),
        )
        projection = self._projections.get(root_run_id)
        if projection is not None:
            projection.approval(record)

    # =================================================================
    # streaming
    # =================================================================
    def get_stream(self, root_run_id: str) -> PublicEventBuffer | None:
        self._evict_expired()
        return self._buffers.get(root_run_id)

    def _ensure_buffer(self, root_run_id: str, session_id: str) -> PublicEventBuffer:
        buffer = self._buffers.get(root_run_id)
        if buffer is None:
            buffer = PublicEventBuffer(
                root_run_id,
                session_id,
                max_events=self.settings.max_replay_events,
                max_bytes=self.settings.max_replay_bytes,
                retention=self.settings.replay_retention,
            )
            self._buffers[root_run_id] = buffer
        return buffer

    def _evict_expired(self) -> None:
        now = time.time()
        for root_run_id, buffer in tuple(self._buffers.items()):
            if buffer.is_expired(now):
                self._buffers.pop(root_run_id, None)
                self._projections.pop(root_run_id, None)

    def _publish_event(
        self,
        buffer: PublicEventBuffer,
        *,
        session_id: str,
        root_run_id: str,
        source_run_id: str,
        parent_run_id: str | None,
        event_type: str,
        data: dict[str, Any],
        timestamp: float,
    ) -> None:
        event = buffer.publish(
            {
                "session_id": session_id,
                "root_run_id": root_run_id,
                "source_run_id": source_run_id,
                "parent_run_id": parent_run_id,
                "type": event_type,
                "timestamp": rfc3339(timestamp),
                "data": data,
            }
        )
        projection = self._projections.get(root_run_id)
        if projection is not None:
            projection.last_sequence = event["sequence"]
            if event_type in _STREAM_TERMINAL:
                projection.status = _STREAM_TERMINAL[event_type]

    # =================================================================
    # run pump
    # =================================================================
    async def _drive_run(
        self,
        *,
        root_run_id: str,
        session: Session,
        run: Run,
        buffer: PublicEventBuffer,
        projection: RunProjection,
        subscription: EventSubscription,
        request_id: str,
    ) -> None:
        try:
            async for event in subscription:
                if event.type in _RUNTIME_TERMINAL:
                    continue
                await self._handle_runtime_event(
                    session_id=session.session_id,
                    root_run_id=root_run_id,
                    buffer=buffer,
                    projection=projection,
                    event=event,
                )
            result = await run.result()
        except asyncio.CancelledError:
            raise
        except Exception:  # pragma: no cover - defensive pump guard
            self._publish_event(
                buffer,
                session_id=session.session_id,
                root_run_id=root_run_id,
                source_run_id=root_run_id,
                parent_run_id=None,
                event_type="run.failed",
                data={
                    "run_id": root_run_id,
                    "error": {
                        "code": "runtime_error",
                        "message": public_message_for("runtime_error"),
                        "request_id": request_id,
                        "details": {},
                    },
                },
                timestamp=time.time(),
            )
            await self.store.update_run_terminal(
                root_run_id,
                status=RUN_STATUS_FAILED,
                ended_at=time.time(),
                error_code="runtime_error",
                error_message=public_message_for("runtime_error"),
            )
            await self.store.set_session_active_run(session.session_id, None, time.time())
            self._finish_active(root_run_id)
            return
        finally:
            subscription.close()
        await self._finalize_run(
            root_run_id=root_run_id,
            session=session,
            buffer=buffer,
            projection=projection,
            result=result,
            request_id=request_id,
        )

    async def _handle_runtime_event(
        self,
        *,
        session_id: str,
        root_run_id: str,
        buffer: PublicEventBuffer,
        projection: RunProjection,
        event: object,
    ) -> None:
        etype = getattr(event, "type")
        payload = dict(getattr(event, "payload", {}) or {})
        timestamp = float(getattr(event, "timestamp", time.time()))
        source_run_id, parent_run_id, agent_tool_name = self._identity(
            projection, root_run_id, getattr(event, "lineage", None)
        )

        def publish(event_type: str, data: dict[str, Any]) -> None:
            self._publish_event(
                buffer,
                session_id=session_id,
                root_run_id=root_run_id,
                source_run_id=source_run_id,
                parent_run_id=parent_run_id,
                event_type=event_type,
                data=data,
                timestamp=timestamp,
            )

        if etype == "run.started":
            await self.store.update_run_started(root_run_id, timestamp)
            publish("run.started", {"run_id": source_run_id})
        elif etype == "child_run.started":
            await self._ensure_child_record(
                session_id,
                root_run_id,
                source_run_id,
                parent_run_id,
                agent_tool_name,
                timestamp,
            )
            projection.child(source_run_id, parent_run_id, agent_tool_name)
            publish(
                "child_run.started",
                {
                    "run_id": source_run_id,
                    "parent_run_id": parent_run_id,
                    "agent_tool_name": agent_tool_name,
                },
            )
        elif etype in _CHILD_TERMINAL:
            status = _CHILD_TERMINAL[etype]
            error_code = payload.get("error_code")
            mapped = map_runtime_error_code(error_code) if error_code else None
            await self.store.update_run_terminal(
                source_run_id,
                status=status,
                ended_at=timestamp,
                error_code=mapped,
                error_message=public_message_for(mapped) if mapped else None,
            )
            projection.child_status(source_run_id, status)
            reason = (
                "run_failed" if status == RUN_STATUS_FAILED else "run_cancelled"
            )
            for entry in projection.open_assistants(source_run_id):
                if projection.assistant_abort(entry["message_id"]):
                    publish(
                        "assistant.aborted",
                        {"message_id": entry["message_id"], "reason": reason},
                    )
            publish(etype, {"run_id": source_run_id})
        elif etype == "model.started":
            message_id = str(payload.get("message_id") or "")
            if message_id:
                projection.assistant_start(message_id, source_run_id)
                publish("assistant.started", {"message_id": message_id})
        elif etype == "model.delta":
            message_id = str(payload.get("message_id") or "")
            text = str(payload.get("text") or "")
            if message_id:
                index = projection.assistant_append_text(
                    message_id, source_run_id, text
                )
                publish(
                    "assistant.delta",
                    {
                        "message_id": message_id,
                        "block_index": index,
                        "block_type": "text",
                        "delta": text,
                    },
                )
        elif etype == "model.tool_call_started":
            mid = payload.get("message_id")
            tool_call_id = str(payload.get("tool_call_id") or "")
            if tool_call_id:
                projection.tool(
                    None if mid is None else str(mid),
                    tool_call_id,
                    str(payload.get("tool_name") or ""),
                )
        elif etype == "tool.started":
            mid = payload.get("message_id")
            tool_call_id = str(payload.get("tool_call_id") or "")
            tool_name = str(payload.get("tool_name") or "")
            if tool_call_id:
                projection.tool_execution(tool_call_id, "running")
                projection.tool(
                    None if mid is None else str(mid),
                    tool_call_id,
                    tool_name,
                )
                publish(
                    "tool.started",
                    {
                        "message_id": mid,
                        "tool_call_id": tool_call_id,
                        "tool_name": tool_name,
                    },
                )
        elif etype in {"tool.completed", "tool.failed", "tool.cancelled"}:
            execution_status = {
                "tool.completed": "completed",
                "tool.failed": "failed",
                "tool.cancelled": "cancelled",
            }[etype]
            tool_call_id = str(payload.get("tool_call_id") or "")
            tool_name = str(payload.get("tool_name") or "")
            if tool_call_id:
                projection.tool_execution(tool_call_id, execution_status)
                publish(
                    "tool.execution_finished",
                    {
                        "message_id": payload.get("message_id"),
                        "tool_call_id": tool_call_id,
                        "tool_name": tool_name,
                        "execution_status": execution_status,
                    },
                )
        elif etype == "tool_batch.committed":
            batch_message_id = payload.get("message_id")
            if batch_message_id is not None and projection.assistant_complete(
                str(batch_message_id)
            ):
                publish("assistant.completed", {"message_id": str(batch_message_id)})
            effects = payload.get("effects") or []
            if isinstance(effects, (list, tuple)):
                for effect in effects:
                    if not isinstance(effect, Mapping):
                        continue
                    tool_call_id = str(effect.get("tool_call_id") or "")
                    if not tool_call_id:
                        continue
                    tool_name = str(effect.get("tool_name") or "")
                    effect_status = str(effect.get("effect_status") or "unknown")
                    certainty = str(effect.get("certainty") or "unknown")
                    projection.tool_effect(tool_call_id, effect_status, certainty)
                    publish(
                        "tool.effect_committed",
                        {
                            "message_id": batch_message_id,
                            "tool_call_id": tool_call_id,
                            "tool_name": tool_name,
                            "effect_status": effect_status,
                            "certainty": certainty,
                        },
                    )

    def _identity(
        self, projection: RunProjection, root_run_id: str, lineage: object
    ) -> tuple[str, str | None, str | None]:
        if lineage is None:
            return root_run_id, None, None
        source_run_id = str(getattr(lineage, "execution_run_id"))
        parent_scope_id = getattr(lineage, "parent_scope_id", None)
        agent_depth = int(getattr(lineage, "agent_depth", 0) or 0)
        parent_run_id: str | None = None
        if source_run_id != root_run_id:
            parent_run_id = (
                projection.scope_to_run.get(str(parent_scope_id)) if parent_scope_id else None
            ) or root_run_id
        scope_id = getattr(lineage, "scope_id", None)
        if scope_id:
            projection.scope_to_run.setdefault(str(scope_id), source_run_id)
        agent_tool_name = (
            getattr(lineage, "agent_tool_name", None) if agent_depth > 0 else None
        )
        return source_run_id, parent_run_id, agent_tool_name

    async def _ensure_child_record(
        self,
        session_id: str,
        root_run_id: str,
        run_id: str,
        parent_run_id: str | None,
        agent_tool_name: str | None,
        timestamp: float,
    ) -> None:
        existing = await self.store.get_run(run_id, self.settings.owner_id)
        if existing is not None:
            return
        await self.store.insert_run(
            {
                "run_id": run_id,
                "owner_id": self.settings.owner_id,
                "session_id": session_id,
                "root_run_id": root_run_id,
                "parent_run_id": parent_run_id,
                "source_run_id": run_id,
                "agent_tool_name": agent_tool_name,
                "status": RUN_STATUS_RUNNING,
                "request_id": None,
                "created_at": timestamp,
                "started_at": timestamp,
                "ended_at": None,
                "effects_json": None,
            }
        )

    async def _finalize_run(
        self,
        *,
        root_run_id: str,
        session: Session,
        buffer: PublicEventBuffer,
        projection: RunProjection,
        result: RunResult,
        request_id: str,
    ) -> None:
        now = time.time()
        product_status = {
            RunStatus.COMPLETED: RUN_STATUS_COMPLETED,
            RunStatus.FAILED: RUN_STATUS_FAILED,
            RunStatus.CANCELLED: RUN_STATUS_CANCELLED,
        }[result.status]
        committed_id = result.output.message_id if result.output is not None else None
        for entry in projection.open_assistants():
            message_id = entry["message_id"]
            if product_status == RUN_STATUS_COMPLETED and message_id == committed_id:
                if projection.assistant_complete(message_id):
                    self._publish_event(
                        buffer,
                        session_id=session.session_id,
                        root_run_id=root_run_id,
                        source_run_id=entry["source_run_id"],
                        parent_run_id=None,
                        event_type="assistant.completed",
                        data={"message_id": message_id},
                        timestamp=now,
                    )
                continue
            reason = {
                RUN_STATUS_FAILED: "run_failed",
                RUN_STATUS_CANCELLED: "run_cancelled",
            }.get(product_status, "runtime_error")
            if projection.assistant_abort(message_id):
                self._publish_event(
                    buffer,
                    session_id=session.session_id,
                    root_run_id=root_run_id,
                    source_run_id=entry["source_run_id"],
                    parent_run_id=None,
                    event_type="assistant.aborted",
                    data={"message_id": message_id, "reason": reason},
                    timestamp=now,
                )

        error_code: str | None = None
        error_message: str | None = None
        if product_status == RUN_STATUS_COMPLETED:
            data = {
                "run_id": root_run_id,
                "usage": usage_dict(result.usage, result.usage_known),
            }
            event_type = "run.completed"
        elif product_status == RUN_STATUS_FAILED:
            error_code = map_runtime_error_code(
                result.error.code if result.error is not None else None
            )
            error_message = public_message_for(error_code)
            data = {
                "run_id": root_run_id,
                "error": {
                    "code": error_code,
                    "message": error_message,
                    "request_id": request_id,
                    "details": {},
                },
            }
            event_type = "run.failed"
        else:
            data = {"run_id": root_run_id, "reason": "user_cancelled"}
            event_type = "run.cancelled"
        self._publish_event(
            buffer,
            session_id=session.session_id,
            root_run_id=root_run_id,
            source_run_id=root_run_id,
            parent_run_id=None,
            event_type=event_type,
            data=data,
            timestamp=now,
        )
        usage = result.usage
        usage_payload = usage_dict(usage, result.usage_known)
        await self.store.update_run_terminal(
            root_run_id,
            status=product_status,
            ended_at=now,
            error_code=error_code,
            error_message=error_message,
            error_retryable=(result.error.retryable if result.error is not None else None),
            input_tokens=usage_payload["input_tokens"],
            output_tokens=usage_payload["output_tokens"],
            total_tokens=usage_payload["total_tokens"],
            usage_known=usage_payload["usage_known"],
            retry_safe=bool(result.retry_safe),
            effects_json=_effects_json(result),
        )
        await self.store.set_session_active_run(session.session_id, None, now)
        await self.store.touch_session(session.session_id, now)
        if self.approval_provider is not None:
            await self.approval_provider.cancel_pending_for_run(
                root_run_id, terminal_reason="run_ended"
            )
        self._finish_active(root_run_id)

    def _finish_active(self, root_run_id: str) -> None:
        active = self._active.pop(root_run_id, None)
        if active is not None:
            active.done.set()

    # =================================================================
    # helpers
    # =================================================================
    async def _open_session(self, session_id: str) -> Session:
        session = self._sessions.get(session_id)
        if session is not None and not session.closed:
            return session
        try:
            session = await Session.open(
                agent=self.agent,
                session_id=session_id,
                repository=self.repository,
                **self._session_kwargs(),
            )
        except SessionNotFoundError as exc:
            raise ProductError("session_not_found", "Session does not exist.") from exc
        self._sessions[session_id] = session
        return session

    async def _load_snapshot(self, session_id: str):
        session = self._sessions.get(session_id)
        if session is not None and not session.closed:
            return await session.snapshot()
        return await self.repository.load(session_id)

    def session_or_none(self, session_id: str) -> Session | None:
        return self._sessions.get(session_id)

    async def wait_for_run(self, root_run_id: str, timeout: float = 5.0) -> None:
        active = self._active.get(root_run_id)
        if active is None:
            return
        try:
            await asyncio.wait_for(asyncio.shield(active.done.wait()), timeout=timeout)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            pass


def _effects_json(result: RunResult) -> str | None:
    import json

    effects = [effect_summary(effect) for effect in result.effects]
    return json.dumps(effects, ensure_ascii=False) if effects else None


def input_receipt_body(
    receipt: object, message: UserMessage, run_id: str | None, kind: str
) -> dict[str, Any]:
    return {
        "input_id": getattr(receipt, "input_id"),
        "session_id": getattr(receipt, "session_id"),
        "run_id": run_id,
        "kind": kind,
        "sequence": getattr(receipt, "sequence"),
        "accepted_at": rfc3339(message.timestamp),
    }


def encode_cursor(updated_at: float, session_id: str) -> str:
    import base64
    import json

    raw = json.dumps([updated_at, session_id], separators=(",", ":")).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def decode_cursor(cursor: str) -> tuple[float, str] | None:
    import base64
    import json

    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        raw = base64.urlsafe_b64decode(padded.encode("ascii"))
        value = json.loads(raw)
        return float(value[0]), str(value[1])
    except Exception:
        return None


__all__ = [
    "ActiveRun",
    "AgentService",
    "RunProjection",
    "decode_cursor",
    "encode_cursor",
    "input_receipt_body",
]
