"""Agent robot Tools backed by RobotService (docs Phase 3B).

Phase 3B requires that rodesk Robot State, rodesk manual commands and the
**Agent Tool** path all go through ``RobotService``.  Until ``mfr3duo_nav``
passes its Gate the only honest navigation tools are the documented
``DummyNavigate`` / ``SimulationNavigate`` variants, so the side-effecting tool
here is explicitly named and reported as a simulation.

Both tools are thin wrappers: they contain no robot semantics of their own and
call ``RobotService`` — the same capability layer the manual UI uses.
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
) -> tuple[Tool, Tool]:
    """The Agent-facing robot Tool set for the current (simulated) stage."""
    state_definition, navigate_definition = _definitions()

    async def get_robot_state(arguments: FrozenJsonObject, context: object):
        robot_id = await _resolve_robot(service, arguments)
        return ToolJsonContent(await service.get_robot_state(robot_id))

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
