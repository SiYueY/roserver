"""Robot Product API: discovery, state, control authority and teleoperation.

Docs §4.5 (HTTP + events WS) and §4.7/§4.9 (teleoperation WS).
"""

from __future__ import annotations

import asyncio
from typing import Any
from uuid import uuid4

from fastapi import APIRouter, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import Response
from pydantic import BaseModel

from ..errors import ProductError
from .service import (
    ROBOT_EVENT_TYPES,
    TELEOP_EVENT_TYPES,
    TELEOP_MODE,
    RobotService,
    RobotSubscription,
)
from .images import image_jpeg

router = APIRouter(prefix="/api/v1")


class AcquireControlRequest(BaseModel):
    mode: str | None = None
    holder_id: str | None = None
    ttl_s: float | None = None


class ReleaseControlRequest(BaseModel):
    authority_id: str | None = None


def get_robot_service(request: Request) -> RobotService:
    return request.app.state.robot


def _error_envelope(exc: ProductError) -> dict[str, Any]:
    return exc.to_body(uuid4().hex)


def _origin_allowed(websocket: WebSocket) -> bool:
    app = websocket.scope["app"]
    settings = app.state.settings
    if not settings.ws_origin_validation:
        return True
    origin = websocket.headers.get("origin")
    if origin is None:
        return True
    return origin in settings.resolved_ws_origins


# =====================================================================
# discovery / state (docs §4.5)
# =====================================================================
@router.get("/robots")
async def list_robots(request: Request) -> dict[str, Any]:
    return await get_robot_service(request).list_robots()


@router.get("/robots/{robot_id}")
async def get_robot(robot_id: str, request: Request) -> dict[str, Any]:
    return await get_robot_service(request).get_robot(robot_id)


@router.get("/robots/{robot_id}/state")
async def get_robot_state(robot_id: str, request: Request) -> dict[str, Any]:
    return await get_robot_service(request).get_robot_state(robot_id)


@router.get("/robots/{robot_id}/cameras/{camera_id}/image")
async def camera_image(robot_id: str, camera_id: str, request: Request) -> Response:
    service = get_robot_service(request)
    await service.get_robot(robot_id)
    camera = getattr(service.backend, "camera_snapshot", None) or getattr(service.backend, "camera_frame", None)
    if camera is None:
        raise ProductError("robot_not_ready", "No real camera is available.")
    message = await service._guard(_camera_frame(camera, f"robot_{camera_id}"))
    try:
        data = await asyncio.to_thread(image_jpeg, message)
    except ValueError as exc:
        raise ProductError("unsupported_media_type", str(exc)) from exc
    return Response(data, media_type="image/jpeg", headers={"Cache-Control": "no-store"})


@router.get("/robots/{robot_id}/observations")
async def observations(robot_id: str, request: Request) -> dict[str, Any]:
    service = get_robot_service(request)
    read = getattr(service.backend, "get_observations", None)
    if read is None:
        raise ProductError("robot_not_ready", "Robot observations are unavailable.")
    return await service._guard(read(robot_id))


async def _camera_frame(camera: Any, source: str) -> Any:
    import inspect
    frame = camera(source)
    return await frame if inspect.isawaitable(frame) else frame


@router.post("/robots/{robot_id}/operations", status_code=202)
async def start_operation(robot_id: str, request: Request, body: dict[str, Any]) -> dict[str, Any]:
    return await get_robot_service(request).operations.start(robot_id, body, request.headers.get("Idempotency-Key"))


@router.get("/robots/{robot_id}/operations/{operation_id}")
async def get_operation(robot_id: str, operation_id: str, request: Request) -> dict[str, Any]:
    return get_robot_service(request).operations.get(robot_id, operation_id)


@router.post("/robots/{robot_id}/operations/{operation_id}/cancel")
async def cancel_operation(robot_id: str, operation_id: str, request: Request) -> dict[str, Any]:
    return await get_robot_service(request).operations.cancel(robot_id, operation_id)


@router.post("/robots/{robot_id}/stop")
async def stop_robot(robot_id: str, request: Request) -> dict[str, Any]:
    service = get_robot_service(request)
    for identifier, task in tuple(service.operations._tasks.items()):
        if not task.done() and service.operations.get(robot_id, identifier)["robot_id"] == robot_id:
            await service.operations.cancel(robot_id, identifier)
    stopped = await service._guard(service.backend.stop(robot_id))
    return {"robot_id": robot_id, "stopped": stopped}


# =====================================================================
# control authority (docs §4.6)
# =====================================================================
@router.post("/robots/{robot_id}/control/acquire")
async def acquire_control(
    robot_id: str,
    request: Request,
    body: AcquireControlRequest | None = None,
) -> dict[str, Any]:
    payload = body or AcquireControlRequest()
    return await get_robot_service(request).acquire_authority(
        robot_id,
        mode=payload.mode or TELEOP_MODE,
        holder_id=payload.holder_id,
        ttl_s=payload.ttl_s,
    )


@router.post("/robots/{robot_id}/control/release")
async def release_control(
    robot_id: str,
    request: Request,
    body: ReleaseControlRequest | None = None,
) -> dict[str, Any]:
    authority_id = body.authority_id if body is not None else None
    return await get_robot_service(request).release_authority(
        robot_id, authority_id=authority_id
    )


@router.get("/robots/{robot_id}/control")
async def get_control(robot_id: str, request: Request) -> dict[str, Any]:
    return await get_robot_service(request).get_authority(robot_id)


@router.post("/robots/{robot_id}/control/renew")
async def renew_control(robot_id: str, request: Request, body: ReleaseControlRequest) -> dict[str, Any]:
    if not body.authority_id:
        raise ProductError("control_authority_required", "authority_id is required.")
    return await get_robot_service(request).renew_authority(robot_id, body.authority_id)


# =====================================================================
# WebSocket helpers
# =====================================================================
async def _ws_reader(
    websocket: WebSocket, subscription: RobotSubscription
) -> None:
    """Forward client frames onto the subscription queue; wake it on close."""
    try:
        while True:
            try:
                data = await websocket.receive_json()
            except WebSocketDisconnect:
                break
            except Exception:
                data = None
            subscription.queue.put_nowait(("ws", data))
    finally:
        subscription.queue.put_nowait(("closed", None))


async def _stream(
    websocket: WebSocket,
    subscription: RobotSubscription,
    *,
    event_types: frozenset[str],
    on_message: Any = None,
) -> None:
    reader = asyncio.create_task(_ws_reader(websocket, subscription))
    try:
        while True:
            kind, payload = await subscription.queue.get()
            if kind == "closed":
                return
            if kind == "ws":
                if on_message is None:
                    continue
                await on_message(payload)
                continue
            event = payload
            if event.get("type") not in event_types:
                continue
            await websocket.send_json(event)
    finally:
        reader.cancel()
        await asyncio.gather(reader, return_exceptions=True)


# =====================================================================
# events WS (docs §4.5)
# =====================================================================
@router.websocket("/robots/{robot_id}/events")
async def robot_events(websocket: WebSocket, robot_id: str) -> None:
    if not _origin_allowed(websocket):
        await websocket.close(code=1008)
        return
    await websocket.accept()
    service: RobotService = websocket.scope["app"].state.robot
    try:
        await service.get_robot(robot_id)
    except ProductError as exc:
        await websocket.send_json(_error_envelope(exc))
        await websocket.close(code=1008)
        return
    subscription = service.subscribe(robot_id)
    try:
        state = await service.snapshot_state(robot_id)
        await websocket.send_json(
            {
                "type": "robot.state",
                "timestamp": state["timestamp"],
                "robot_id": robot_id,
                "data": {"robot_id": robot_id, "state": state},
            }
        )
        await _stream(websocket, subscription, event_types=ROBOT_EVENT_TYPES)
    except WebSocketDisconnect:
        return
    finally:
        service.unsubscribe(subscription)


# =====================================================================
# teleoperation WS (docs §4.7 / §4.9)
# =====================================================================
@router.websocket("/robots/{robot_id}/teleoperation")
async def teleoperation(websocket: WebSocket, robot_id: str) -> None:
    if not _origin_allowed(websocket):
        await websocket.close(code=1008)
        return
    await websocket.accept()
    service: RobotService = websocket.scope["app"].state.robot
    try:
        await service.get_robot(robot_id)
    except ProductError as exc:
        await websocket.send_json(_error_envelope(exc))
        await websocket.close(code=1008)
        return
    if not await service.has_authority(robot_id):
        await websocket.send_json(
            {
                "type": "control_lost",
                "robot_id": robot_id,
                "reason": "control_authority_required",
            }
        )
        await websocket.close(code=1008)
        return
    subscription = service.subscribe(robot_id)

    async def on_message(data: Any) -> None:
        if not isinstance(data, dict):
            await websocket.send_json(
                {
                    "type": "command_rejected",
                    "robot_id": robot_id,
                    "reason": "invalid_input",
                    "last_sequence": 0,
                    "accepted": False,
                    "watchdog_remaining_ms": 0,
                }
            )
            return
        feedback = await service.handle_velocity_command(robot_id, data)
        await websocket.send_json(feedback)

    try:
        await _stream(
            websocket,
            subscription,
            event_types=TELEOP_EVENT_TYPES,
            on_message=on_message,
        )
    except WebSocketDisconnect:
        return
    finally:
        await service.on_teleop_disconnect(robot_id)
        service.unsubscribe(subscription)


__all__ = ["get_robot_service", "router"]
