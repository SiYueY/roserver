"""ProductApprovalProvider: roboagent ApprovalProvider backed by ApplicationStore.

Docs §3.11.  The Product ``approval.requested`` event is produced by this
provider (not by Runtime event projection); Product ``approval.resolved`` is
emitted whenever the ApprovalRecord transitions out of ``pending``.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Mapping, Sequence
from typing import Any, Awaitable, Callable

from roboagent.tool import (
    ApprovalDecision,
    ApprovalRequest,
    ApprovalResponse,
)

from ..store.application import ApplicationStore

EmitApprovalEvent = Callable[[Mapping[str, Any], str, dict[str, Any]], Awaitable[None]]

TERMINAL_APPROVAL_STATUSES = frozenset(
    {"approved", "denied", "expired", "cancelled"}
)


def summarize_request(tool_name: str, reason: str | None) -> str:
    if reason:
        return f"{tool_name}: {reason}"
    return f"Approve tool call: {tool_name}"


class ProductApprovalProvider:
    def __init__(
        self,
        store: ApplicationStore,
        *,
        owner_id: str,
        ttl: float,
        emit: EmitApprovalEvent | None = None,
    ) -> None:
        self.store = store
        self.owner_id = owner_id
        self.ttl = ttl
        self._emit = emit
        self._pending: dict[str, asyncio.Future[ApprovalDecision]] = {}

    def set_emitter(self, emit: EmitApprovalEvent) -> None:
        self._emit = emit

    @property
    def pending_ids(self) -> tuple[str, ...]:
        return tuple(self._pending)

    async def request(
        self, request: ApprovalRequest, cancellation: object
    ) -> ApprovalResponse:
        created_at = time.time()
        expires_at = created_at + self.ttl
        root_run_id = (
            request.lineage.root_run_id
            if request.lineage is not None
            else request.run_id
        )
        record = {
            "approval_id": request.approval_id,
            "owner_id": self.owner_id,
            "session_id": request.session_id,
            "root_run_id": root_run_id,
            "source_run_id": request.run_id,
            "tool_call_id": request.tool_call_id,
            "tool_name": request.tool_name,
            "arguments": _thaw(request.arguments),
            "arguments_digest": request.arguments_digest,
            "reason": request.reason,
            "effect_capability": request.effect_capability,
            "summary": summarize_request(request.tool_name, request.reason),
            "status": "pending",
            "created_at": created_at,
            "expires_at": expires_at,
        }
        await self.store.insert_approval(record)
        loop = asyncio.get_running_loop()
        future: asyncio.Future[ApprovalDecision] = loop.create_future()
        self._pending[request.approval_id] = future
        await self._emit_event(record, "approval.requested", approval_requested_data(record))

        cancelled = asyncio.ensure_future(_wait_cancelled(cancellation))
        try:
            remaining = max(0.0, expires_at - time.time())
            done, _ = await asyncio.wait(
                {future, cancelled}, timeout=remaining, return_when=asyncio.FIRST_COMPLETED
            )
            if future in done:
                return ApprovalResponse(
                    request.approval_id, request.arguments_digest, future.result()
                )
            if cancelled in done:
                # Run cancellation: the owning Run finalizes pending records.
                raise asyncio.CancelledError("approval cancelled")
            await self._terminate(
                request.approval_id,
                status="expired",
                decision=None,
                terminal_reason="approval_timeout",
            )
            return ApprovalResponse(
                request.approval_id, request.arguments_digest, ApprovalDecision.REJECT
            )
        finally:
            cancelled.cancel()
            await asyncio.gather(cancelled, return_exceptions=True)
            self._pending.pop(request.approval_id, None)

    def notify(self, approval_id: str, decision: ApprovalDecision) -> bool:
        future = self._pending.get(approval_id)
        if future is None or future.done():
            return False
        future.set_result(decision)
        return True

    async def _terminate(
        self,
        approval_id: str,
        *,
        status: str,
        decision: str | None,
        terminal_reason: str | None,
    ) -> dict[str, Any] | None:
        updated = await self.store.terminate_approval(
            approval_id,
            self.owner_id,
            status=status,
            decision=decision,
            decision_reason=None,
            terminal_reason=terminal_reason,
            resolved_at=time.time(),
        )
        if updated is not None:
            await self._emit_event(
                updated, "approval.resolved", approval_resolved_data(updated)
            )
        return updated

    async def cancel_pending_for_run(
        self, root_run_id: str, *, terminal_reason: str
    ) -> int:
        rows = await self.store.list_pending_approvals_for_run(
            root_run_id, self.owner_id
        )
        return await self._cancel_rows(rows, terminal_reason)

    async def cancel_pending_for_session(
        self, session_id: str, *, terminal_reason: str
    ) -> int:
        rows = await self.store.list_approvals_for_session(session_id, self.owner_id)
        pending = [row for row in rows if row["status"] == "pending"]
        return await self._cancel_rows(pending, terminal_reason)

    async def shutdown(self) -> None:
        rows = await self.store.list_pending_approvals(self.owner_id)
        await self._cancel_rows(rows, "server_restarted")
        for future in tuple(self._pending.values()):
            if not future.done():
                future.set_result(ApprovalDecision.REJECT)

    async def _cancel_rows(
        self, rows: Sequence[Mapping[str, Any]], terminal_reason: str
    ) -> int:
        count = 0
        for row in rows:
            updated = await self._terminate(
                str(row["approval_id"]),
                status="cancelled",
                decision=None,
                terminal_reason=terminal_reason,
            )
            if updated is not None:
                count += 1
            future = self._pending.get(str(row["approval_id"]))
            if future is not None and not future.done():
                future.set_result(ApprovalDecision.REJECT)
        return count

    async def _emit_event(
        self, record: Mapping[str, Any], event_type: str, data: dict[str, Any]
    ) -> None:
        if self._emit is None:
            return
        await self._emit(record, event_type, data)


async def _wait_cancelled(cancellation: object) -> None:
    wait = getattr(cancellation, "wait_cancelled", None)
    if wait is None:  # pragma: no cover - defensive
        return
    await wait()


def approval_requested_data(record: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "approval_id": record.get("approval_id"),
        "session_id": record.get("session_id"),
        "root_run_id": record.get("root_run_id"),
        "source_run_id": record.get("source_run_id"),
        "tool_call_id": record.get("tool_call_id"),
        "tool_name": record.get("tool_name"),
        "summary": record.get("summary"),
        "arguments": _arguments(record),
        "arguments_digest": record.get("arguments_digest"),
        "effect_capability": record.get("effect_capability"),
        "expires_at": _rfc3339(record.get("expires_at")),
    }


def approval_resolved_data(record: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "approval_id": record.get("approval_id"),
        "session_id": record.get("session_id"),
        "root_run_id": record.get("root_run_id"),
        "tool_call_id": record.get("tool_call_id"),
        "status": record.get("status"),
        "decision": record.get("decision"),
        "terminal_reason": record.get("terminal_reason"),
        "resolved_at": _rfc3339(record.get("resolved_at")),
    }


def _rfc3339(value: object) -> str | None:
    if value is None:
        return None
    from .schema import rfc3339

    return rfc3339(float(value))  # type: ignore[arg-type]


def _arguments(record: Mapping[str, Any]) -> object:
    value = record.get("arguments")
    if value is not None:
        return value
    import json

    raw = record.get("arguments_json")
    if raw is None:
        return {}
    if isinstance(raw, (dict, list)):
        return raw
    try:
        return json.loads(str(raw))
    except ValueError:  # pragma: no cover - defensive
        return {}


def _thaw(value: object) -> object:
    from roboagent.message import thaw_json

    if hasattr(value, "items"):
        return thaw_json(value)  # type: ignore[arg-type]
    return value


__all__ = [
    "ProductApprovalProvider",
    "TERMINAL_APPROVAL_STATUSES",
    "approval_requested_data",
    "approval_resolved_data",
    "summarize_request",
]
