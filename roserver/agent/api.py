"""FastAPI routers for the Agent Product Protocol (docs §3)."""

from __future__ import annotations

import asyncio
import json
import time
from typing import Any, Awaitable, Callable, NamedTuple

from fastapi import APIRouter, Request, Response, WebSocket, WebSocketDisconnect
from pydantic import BaseModel
from starlette.responses import JSONResponse

from roboagent.message import (
    JsonContent,
    TextContent,
    UserMessage,
    canonical_json_digest,
)

from ..artifact.service import is_artifact_id
from ..errors import ProductError
from .schema import rfc3339
from .service import AgentService, decode_cursor

router = APIRouter(prefix="/api/v1")


class CreateSessionRequest(BaseModel):
    title: str | None = None


class UpdateSessionRequest(BaseModel):
    title: str


class ApprovalResolveRequest(BaseModel):
    decision: str
    arguments_digest: str
    reason: str | None = None


class ParsedInput(NamedTuple):
    payload: dict[str, Any]
    client_message_id: str | None
    message: UserMessage


def get_service(request: Request) -> AgentService:
    return request.app.state.service


def request_id_of(request: Request) -> str:
    return getattr(request.state, "request_id", "")


def _response(status: int, body: dict[str, Any], replayed: bool = False) -> JSONResponse:
    headers = {"Idempotency-Replayed": "true"} if replayed else None
    return JSONResponse(status_code=status, content=body, headers=headers)


# =====================================================================
# content negotiation (docs §3.4)
# =====================================================================
async def parse_agent_input(request: Request) -> ParsedInput:
    settings = request.app.state.settings
    content_type = request.headers.get("content-type", "")
    if not content_type.lower().startswith("application/json"):
        raise ProductError(
            "unsupported_media_type",
            "Request body must be application/json.",
        )
    declared = request.headers.get("content-length")
    if declared is not None:
        try:
            if int(declared) > settings.max_agent_input_bytes:
                raise ProductError("payload_too_large", "AgentInput is too large.")
        except ValueError:
            raise ProductError("invalid_input", "Invalid Content-Length.") from None
    raw = await request.body()
    if len(raw) > settings.max_agent_input_bytes:
        raise ProductError("payload_too_large", "AgentInput is too large.")
    try:
        payload = json.loads(raw)
    except ValueError as exc:
        raise ProductError("invalid_input", "Request body is not valid JSON.") from exc
    if not isinstance(payload, dict):
        raise ProductError("invalid_input", "Request body must be a JSON object.")
    input_block = payload.get("input")
    if not isinstance(input_block, dict):
        raise ProductError("invalid_input", "input is required.")
    client_message_id = input_block.get("client_message_id")
    if client_message_id is not None and not isinstance(client_message_id, str):
        raise ProductError("invalid_input", "client_message_id must be a string.")
    content = input_block.get("content")
    if not isinstance(content, list):
        raise ProductError("invalid_input", "input.content must be an array.")
    if len(content) == 0:
        raise ProductError(
            "unsupported_content_type", "content must contain at least one block."
        )
    if len(content) > settings.max_content_blocks:
        raise ProductError("payload_too_large", "Too many content blocks.")
    blocks = []
    artifacts = getattr(request.app.state, "artifacts", None)
    for item in content:
        if not isinstance(item, dict):
            raise ProductError("invalid_input", "content blocks must be objects.")
        blocks.append(await _content_block(item, artifacts))
    try:
        message = UserMessage(tuple(blocks))
    except ProductError:
        raise
    except Exception as exc:
        raise ProductError("invalid_input", "Invalid message content.") from exc
    return ParsedInput(payload, client_message_id, message)


async def _content_block(item: dict[str, Any], artifacts: Any) -> Any:
    """Map one Product content block onto canonical roboagent content (§2.8)."""
    kind = item.get("type")
    if kind == "text":
        text = item.get("text")
        if not isinstance(text, str):
            raise ProductError("invalid_input", "text block requires a text string.")
        return TextContent(text)
    if kind == "json":
        return JsonContent(item.get("value"))
    if kind in ("image", "audio", "file", "artifact_reference"):
        artifact_id = item.get("artifact_id")
        if not is_artifact_id(artifact_id):
            raise ProductError("invalid_input", "artifact_id must be sha256:<64 hex>.")
        if artifacts is None:  # pragma: no cover - create_app always wires this
            raise ProductError("internal_error", "Artifact service is not configured.")
        record = await artifacts.get(artifact_id)
        return artifacts.to_reference(record)
    raise ProductError(
        "unsupported_content_type",
        f"Content type {kind!r} is not supported.",
    )


# =====================================================================
# idempotency (docs §3.5)
# =====================================================================
async def idempotent_write(
    request: Request,
    service: AgentService,
    *,
    operation: str,
    scope_id: str,
    payload: dict[str, Any],
    execute: Callable[[], Awaitable[tuple[int, dict[str, Any], str | None]]],
) -> JSONResponse:
    key = request.headers.get("idempotency-key")
    if not key:
        status, body, _ = await execute()
        return _response(status, body)
    digest = canonical_json_digest(payload)
    owner = service.settings.owner_id
    record = await service.store.get_idempotency(owner, operation, scope_id, key)
    if record is not None:
        if record.get("state") == "completed":
            if record.get("request_digest") != digest:
                raise ProductError(
                    "idempotency_conflict",
                    "Idempotency-Key was reused with a different payload.",
                )
            stored = record.get("response_body")
            parsed = json.loads(stored) if isinstance(stored, str) else stored
            if not isinstance(parsed, dict):
                parsed = {}
            return _response(int(record["response_status"]), parsed, replayed=True)
        raise ProductError(
            "idempotency_in_progress", "An identical request is still in progress."
        )
    now = time.time()
    await service.store.insert_idempotency(
        owner_id=owner,
        operation=operation,
        scope_id=scope_id,
        key=key,
        request_digest=digest,
        created_at=now,
        expires_at=now + service.settings.idempotency_retention,
    )
    try:
        status, body, reference = await execute()
    except ProductError as exc:
        await service.store.complete_idempotency(
            owner_id=owner,
            operation=operation,
            scope_id=scope_id,
            key=key,
            response_status=exc.status,
            response_body=exc.to_body(request_id_of(request)),
            result_reference=None,
        )
        raise
    await service.store.complete_idempotency(
        owner_id=owner,
        operation=operation,
        scope_id=scope_id,
        key=key,
        response_status=status,
        response_body=body,
        result_reference=reference,
    )
    return _response(status, body)


# =====================================================================
# sessions
# =====================================================================
@router.post("/sessions", status_code=201)
async def create_session(
    request: Request, body: CreateSessionRequest | None = None
) -> dict[str, Any]:
    service = get_service(request)
    return await service.create_session(title=None if body is None else body.title)


@router.get("/sessions")
async def list_sessions(
    request: Request,
    limit: int = 20,
    cursor: str | None = None,
    order: str = "updated_desc",
) -> dict[str, Any]:
    if order != "updated_desc":
        raise ProductError("invalid_input", "order must be updated_desc.")
    if limit < 1 or limit > 100:
        raise ProductError("invalid_input", "limit must be in 1..100.")
    decoded = None
    if cursor is not None:
        decoded = decode_cursor(cursor)
        if decoded is None:
            raise ProductError("invalid_input", "Malformed cursor.")
    service = get_service(request)
    return await service.list_sessions(limit=limit, cursor=decoded)


@router.get("/sessions/{session_id}")
async def get_session(session_id: str, request: Request) -> dict[str, Any]:
    return await get_service(request).get_session(session_id)


@router.patch("/sessions/{session_id}")
async def update_session(
    session_id: str, body: UpdateSessionRequest, request: Request
) -> dict[str, Any]:
    return await get_service(request).update_session(session_id, title=body.title)


@router.delete("/sessions/{session_id}", status_code=204)
async def delete_session(session_id: str, request: Request) -> Response:
    await get_service(request).delete_session(session_id)
    return Response(status_code=204)


# =====================================================================
# runs
# =====================================================================
@router.post("/sessions/{session_id}/runs")
async def start_run(session_id: str, request: Request) -> JSONResponse:
    service = get_service(request)
    parsed = await parse_agent_input(request)
    rid = request_id_of(request)

    async def execute() -> tuple[int, dict[str, Any], str | None]:
        await service.record_artifact_references(session_id, parsed.message)
        body, run_id = await service.start_run(
            session_id,
            parsed.message,
            client_message_id=parsed.client_message_id,
            request_id=rid,
        )
        return 201, body, run_id

    return await idempotent_write(
        request,
        service,
        operation="create_run",
        scope_id=session_id,
        payload=parsed.payload,
        execute=execute,
    )


@router.get("/runs/{run_id}")
async def get_run(run_id: str, request: Request) -> dict[str, Any]:
    return await get_service(request).get_run(run_id)


@router.get("/runs/{run_id}/projection")
async def get_run_projection(run_id: str, request: Request) -> dict[str, Any]:
    return await get_service(request).get_projection(run_id)


@router.post("/runs/{run_id}/cancel")
async def cancel_run(run_id: str, request: Request) -> dict[str, Any]:
    return await get_service(request).cancel_run(run_id)


@router.post("/runs/{run_id}/steer")
async def steer_run(run_id: str, request: Request) -> JSONResponse:
    service = get_service(request)
    parsed = await parse_agent_input(request)
    rid = request_id_of(request)

    record = await service.store.get_run(run_id, service.settings.owner_id)
    scope_id = str(record["session_id"]) if record is not None else run_id

    async def execute() -> tuple[int, dict[str, Any], str | None]:
        await service.record_artifact_references(scope_id, parsed.message)
        body, input_id = await service.steer(run_id, parsed.message, request_id=rid)
        return 200, body, input_id

    return await idempotent_write(
        request,
        service,
        operation="steer",
        scope_id=scope_id,
        payload=parsed.payload,
        execute=execute,
    )


@router.post("/sessions/{session_id}/follow-ups")
async def follow_up(session_id: str, request: Request) -> JSONResponse:
    service = get_service(request)
    parsed = await parse_agent_input(request)
    rid = request_id_of(request)

    async def execute() -> tuple[int, dict[str, Any], str | None]:
        await service.record_artifact_references(session_id, parsed.message)
        body, input_id = await service.follow_up(
            session_id, parsed.message, request_id=rid
        )
        return 200, body, input_id

    return await idempotent_write(
        request,
        service,
        operation="follow_up",
        scope_id=session_id,
        payload=parsed.payload,
        execute=execute,
    )


# =====================================================================
# approvals
# =====================================================================
@router.get("/approvals/{approval_id}")
async def get_approval(approval_id: str, request: Request) -> dict[str, Any]:
    return await get_service(request).get_approval(approval_id)


@router.post("/approvals/{approval_id}/resolve")
async def resolve_approval(
    approval_id: str, body: ApprovalResolveRequest, request: Request
) -> JSONResponse:
    service = get_service(request)
    if body.decision not in {"approve", "deny"}:
        raise ProductError("invalid_input", "decision must be approve or deny.")
    if not body.arguments_digest:
        raise ProductError("invalid_input", "arguments_digest is required.")
    payload = body.model_dump()

    async def execute() -> tuple[int, dict[str, Any], str | None]:
        info = await service.resolve_approval(
            approval_id,
            decision=body.decision,
            arguments_digest=body.arguments_digest,
            reason=body.reason,
        )
        return 200, info, approval_id

    return await idempotent_write(
        request,
        service,
        operation="approval_resolve",
        scope_id=approval_id,
        payload=payload,
        execute=execute,
    )


# =====================================================================
# WebSocket event stream (docs §3.8, §3.13, §3.14)
# =====================================================================
def _origin_allowed(websocket: WebSocket) -> bool:
    app = websocket.scope["app"]
    settings = app.state.settings
    if not settings.ws_origin_validation:
        return True
    origin = websocket.headers.get("origin")
    if origin is None:
        return True
    return origin in settings.resolved_ws_origins


@router.websocket("/runs/{run_id}/events")
async def run_events(websocket: WebSocket, run_id: str) -> None:
    if not _origin_allowed(websocket):
        await websocket.close(code=1008)
        return
    await websocket.accept()
    app = websocket.scope["app"]
    settings = app.state.settings
    service: AgentService = app.state.service
    record = await service.store.get_run(run_id, settings.owner_id)
    if record is None:
        await websocket.send_json(
            _resync_envelope(
                session_id="",
                root_run_id=run_id,
                source_run_id=run_id,
                requested=None,
                oldest=None,
                last=None,
            )
        )
        await websocket.close(code=1008)
        return
    root_run_id = str(record["root_run_id"])
    session_id = str(record["session_id"])
    after_param = websocket.query_params.get("after_sequence")
    after: int | None = None
    if after_param is not None:
        try:
            after = int(after_param)
            if after < 0:
                raise ValueError
        except ValueError:
            await websocket.close(code=1008)
            return
    buffer = service.get_stream(root_run_id)
    subscription = buffer.subscribe() if buffer is not None else None
    try:
        if after is not None:
            if buffer is None:
                await websocket.send_json(
                    _resync_envelope(
                        session_id=session_id,
                        root_run_id=root_run_id,
                        source_run_id=str(record["source_run_id"]),
                        requested=after,
                        oldest=None,
                        last=None,
                    )
                )
                await websocket.close(code=1008)
                return
            events, needs_resync = buffer.replay(after)
            if needs_resync:
                await websocket.send_json(
                    buffer.resync_event(
                        requested_after_sequence=after,
                        sequence=buffer.last_sequence,
                        timestamp=rfc3339(time.time()) or "",
                    )
                )
            else:
                for event in events:
                    await websocket.send_json(event)
        if buffer is None or subscription is None:
            await websocket.close(code=1008)
            return
        await _pump_websocket(websocket, buffer, subscription, after)
    except WebSocketDisconnect:
        return
    finally:
        if buffer is not None and subscription is not None:
            buffer.unsubscribe(subscription)
            subscription.close()


async def _pump_websocket(
    websocket: WebSocket,
    buffer: Any,
    subscription: Any,
    after: int | None,
) -> None:
    async def reader() -> None:
        while True:
            message = await websocket.receive()
            if message.get("type") == "websocket.disconnect":
                return

    async def writer() -> None:
        while True:
            item = await subscription.queue.get()
            if item is None:
                return
            if subscription.lost:
                subscription.lost = False
                await websocket.send_json(
                    buffer.resync_event(
                        requested_after_sequence=after,
                        sequence=int(item.get("sequence", buffer.last_sequence)),
                        timestamp=rfc3339(time.time()) or "",
                    )
                )
                continue
            await websocket.send_json(item)

    read_task = asyncio.create_task(reader())
    write_task = asyncio.create_task(writer())
    try:
        done, pending = await asyncio.wait(
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


def _resync_envelope(
    *,
    session_id: str,
    root_run_id: str,
    source_run_id: str,
    requested: int | None,
    oldest: int | None,
    last: int | None,
) -> dict[str, Any]:
    return {
        "session_id": session_id,
        "root_run_id": root_run_id,
        "source_run_id": source_run_id,
        "parent_run_id": None,
        "sequence": 0,
        "type": "stream.resync_required",
        "timestamp": rfc3339(time.time()) or "",
        "data": {
            "requested_after_sequence": requested,
            "oldest_available_sequence": oldest,
            "last_sequence": last,
        },
    }


__all__ = ["get_service", "idempotent_write", "parse_agent_input", "router"]
