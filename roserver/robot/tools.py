"""Agent Tools use the same RobotService as Product HTTP and manual control.

The selected backend determines the Tool registry: simulation stays explicit;
DCLPY exposes the real Robot SDK tasks, stopping and camera artifacts.
"""

from __future__ import annotations

import asyncio
import time
from datetime import datetime, timezone
from typing import Any

from roboagent.message import FrozenJsonObject
from roboagent.tool import (
    Tool,
    ToolDefinition,
    ToolEffectKind,
    ToolExecutionMode,
    ToolJsonContent,
)

from ..config import Settings
from .service import RobotService

_AGENT_HOLDER = "agent"
# Stay well inside robot_watchdog_timeout (docs §4.8) while streaming commands.
_COMMAND_PERIOD_S = 0.1
_SIMULATED_TRAVEL_S = 0.3


def _now() -> str:
    return (
        datetime.now(timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


def _definitions() -> tuple[ToolDefinition, ToolDefinition]:
    state = ToolDefinition(
        "get_robot_state",
        "Read the current robot state (connection, mode, pose, velocity, battery).",
        FrozenJsonObject(
            {
                "type": "object",
                "properties": {"robot_id": {"type": "string"}},
                "additionalProperties": False,
            }
        ),
    )
    navigate = ToolDefinition(
        "simulate_navigate",
        (
            "SIMULATION ONLY: drive the simulated robot toward a named target. "
            "Real navigation is not available until mfr3duo_nav is implemented."
        ),
        FrozenJsonObject(
            {
                "type": "object",
                "properties": {
                    "target": {"type": "string"},
                    "robot_id": {"type": "string"},
                },
                "required": ["target"],
                "additionalProperties": False,
            }
        ),
    )
    return state, navigate


async def _resolve_robot(service: RobotService, arguments: FrozenJsonObject) -> str:
    explicit = arguments.get("robot_id")
    if isinstance(explicit, str) and explicit:
        return explicit
    listing = await service.list_robots()
    items = listing.get("items") or []
    if not items:
        from ..errors import ProductError

        raise ProductError("robot_not_found", "No robot is available.")
    return str(items[0]["robot_id"])


def robot_tools(
    service: RobotService, settings: Settings
) -> tuple[Tool, ...]:
    """Build the Agent-facing Tool set for the selected robot backend."""
    state_definition, navigate_definition = _definitions()

    async def get_robot_state(arguments: FrozenJsonObject, context: object):
        robot_id = await _resolve_robot(service, arguments)
        return ToolJsonContent(await service.get_robot_state(robot_id))

    if not getattr(service.backend, "is_simulated", True):
        return _real_tools(service, state_definition, get_robot_state)

    async def simulate_navigate(arguments: FrozenJsonObject, context: object):
        target = arguments.get("target")
        if not isinstance(target, str) or not target.strip():
            raise ValueError("target must be a non-empty string.")
        robot_id = await _resolve_robot(service, arguments)

        authority = await service.acquire_authority(robot_id, holder_id=_AGENT_HOLDER)
        authority_id = str(authority["authority_id"])
        sequence = 0
        commands_sent = 0
        limit = max(
            0.05,
            min(
                0.5 * settings.robot_max_linear_velocity,
                settings.robot_max_linear_velocity,
            ),
        )
        try:
            deadline = time.monotonic() + _SIMULATED_TRAVEL_S
            while time.monotonic() < deadline:
                sequence += 1
                feedback = await service.handle_velocity_command(
                    robot_id,
                    {
                        "type": "velocity",
                        "authority_id": authority_id,
                        "sequence": sequence,
                        "client_timestamp": _now(),
                        "linear_x": limit,
                        "linear_y": 0.0,
                        "angular_z": 0.0,
                        "deadman": True,
                    },
                )
                commands_sent += 1
                if not feedback.get("accepted"):
                    raise RuntimeError(
                        f"robot rejected command: {feedback.get('reason')}"
                    )
                await asyncio.sleep(_COMMAND_PERIOD_S)
        finally:
            # Always zero the robot and give the authority back.
            sequence += 1
            try:
                await service.handle_velocity_command(
                    robot_id,
                    {
                        "type": "velocity",
                        "authority_id": authority_id,
                        "sequence": sequence,
                        "client_timestamp": _now(),
                        "linear_x": 0.0,
                        "linear_y": 0.0,
                        "angular_z": 0.0,
                        "deadman": False,
                    },
                )
            finally:
                await service.release_authority(robot_id)

        state: dict[str, Any] = await service.get_robot_state(robot_id)
        return ToolJsonContent(
            {
                "simulated": True,
                "target": target,
                "robot_id": robot_id,
                "commands_sent": commands_sent,
                "pose": state.get("pose"),
                "note": (
                    "SimulationNavigate: no real navigation was performed. "
                    "Real navigation requires mfr3duo_nav."
                ),
            }
        )

    return (
        Tool(
            state_definition,
            get_robot_state,
            execution_mode=ToolExecutionMode.CONCURRENT,
            effect_kind=ToolEffectKind.READ_ONLY,
        ),
        Tool(
            navigate_definition,
            simulate_navigate,
            execution_mode=ToolExecutionMode.SERIAL,
            effect_kind=ToolEffectKind.SIDE_EFFECTING,
        ),
    )


__all__ = ["robot_tools"]


def _real_tools(service: RobotService, state_definition: ToolDefinition, state_handler: Any) -> tuple[Tool, ...]:
    pose = {"type": "object", "properties": {
        "frame_id": {"type": "string"}, "x": {"type": "number"}, "y": {"type": "number"},
        "z": {"type": "number"}, "theta": {"type": "number"}},
        "required": ["x", "y"], "additionalProperties": False}
    properties: dict[str, Any] = {
        "robot_id": {"type": "string"}, "kind": {"type": "string"}, "object_id": {"type": "string"},
        "manipulator": {"type": "string", "enum": ["auto", "left", "right"]},
        "pose": pose, "timeout_s": {"type": "number", "minimum": .001, "maximum": 3600},
        "width": {"type": "number"}, "speed": {"type": "number"}, "force": {"type": "number"},
        "epsilon_inner": {"type": "number"}, "epsilon_outer": {"type": "number"},
        "position": {"type": "number"},
    }
    step_schema = {"type": "object", "properties": {k: v for k, v in properties.items() if k != "robot_id"},
                   "required": ["kind"], "additionalProperties": False}
    properties["steps"] = {"type": "array", "items": step_schema, "minItems": 1, "maxItems": 32}

    async def execute(arguments: FrozenJsonObject, context: Any):
        robot_id = await _resolve_robot(service, arguments)
        from .operations import validate_operation
        # Frozen JSON mappings/tuples become plain Product JSON before validation.
        from roboagent.message import thaw_json
        payload = thaw_json(arguments)
        payload.pop("robot_id", None)
        payload = validate_operation(payload, service.settings.robot_operation_timeout)
        operation = await service.operations.start(robot_id, payload)
        record = await service.operations.wait(robot_id, operation["operation_id"], getattr(context, "cancellation", None))
        return ToolJsonContent(record)

    async def stop(arguments: FrozenJsonObject, context: Any):
        robot_id = await _resolve_robot(service, arguments)
        for identifier in tuple(service.operations._tasks):
            await service.operations.cancel(robot_id, identifier)
        return ToolJsonContent({"robot_id": robot_id, "stopped": await service._guard(service.backend.stop(robot_id))})

    async def camera(arguments: FrozenJsonObject, context: Any):
        from roboagent.tool import BinaryToolContent, RawToolResult
        from .images import image_jpeg
        robot_id = await _resolve_robot(service, arguments)
        await service.get_robot(robot_id)
        source = str(arguments.get("video_source", "robot_head"))
        from .api import _camera_frame
        read = getattr(service.backend, "camera_snapshot", None) or getattr(service.backend, "camera_frame")
        message = await service._guard(_camera_frame(read, source))
        data = await asyncio.to_thread(image_jpeg, message)
        return RawToolResult((BinaryToolContent(data, "image/jpeg"),))

    async def observations(arguments: FrozenJsonObject, context: Any):
        robot_id = await _resolve_robot(service, arguments)
        return ToolJsonContent(await service._guard(getattr(service.backend, "get_observations")(robot_id)))

    schema = FrozenJsonObject({"type": "object", "properties": properties,
                               "required": ["kind"], "additionalProperties": False})
    def task_timeout(arguments: FrozenJsonObject) -> float:
        # The Agent must wait for the robot deadline plus terminal confirmation,
        # rather than cancelling a 180-second task at its generic 60-second limit.
        requested = arguments.get("timeout_s", service.settings.robot_operation_timeout)
        if not isinstance(requested, (int, float)) or isinstance(requested, bool):
            raise ValueError("A numeric timeout_s is required.")
        return float(requested) + service.settings.robot_terminal_timeout + 5.
    return (
        Tool(state_definition, state_handler, execution_mode=ToolExecutionMode.CONCURRENT, effect_kind=ToolEffectKind.READ_ONLY),
        Tool(ToolDefinition("get_robot_observations", "Read fresh observed object IDs/poses, tool poses and Robot SDK readiness/recovery diagnostics. Current MuJoCo observations are simulation ground truth.",
                            FrozenJsonObject({"type": "object", "properties": {"robot_id": {"type": "string"}}, "additionalProperties": False})),
             observations, execution_mode=ToolExecutionMode.CONCURRENT, effect_kind=ToolEffectKind.READ_ONLY),
        Tool(ToolDefinition("execute_robot_task", "Execute navigate, pick, place, sequence, gripper_move, gripper_grasp or recover. scene_joint drives a bounded simulation fixture actuator: object_id names its joint and position is its target; it does not perform a robot arm handle grasp. Pick/Place require observed objects and verified physical results. Navigation while holding an object is unsupported.", schema),
             execute, execution_mode=ToolExecutionMode.SERIAL, effect_kind=ToolEffectKind.SIDE_EFFECTING,
             timeout_resolver=task_timeout),
        Tool(ToolDefinition("stop_robot", "Cancel owned robot operations and stop the base; uncertain termination requires recovery.",
                            FrozenJsonObject({"type": "object", "properties": {"robot_id": {"type": "string"}}, "additionalProperties": False})),
             stop, execution_mode=ToolExecutionMode.CONCURRENT, effect_kind=ToolEffectKind.SIDE_EFFECTING),
        Tool(ToolDefinition("get_camera_image", "Read a fresh robot camera image for visual inspection.",
                            FrozenJsonObject({"type": "object", "properties": {"robot_id": {"type": "string"}, "video_source": {"type": "string"}}, "additionalProperties": False})),
             camera, execution_mode=ToolExecutionMode.CONCURRENT, effect_kind=ToolEffectKind.READ_ONLY),
    )
