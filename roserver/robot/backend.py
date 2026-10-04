"""robot backend boundary and a deterministic simulated robot (docs §4.1-§4.4).

项目当前**尚未进入机器人集成阶段**，因此 roserver 只定义抽象边界，并只提供
一个离线、确定性的模拟机器人：

    RobotBackend            抽象接口（今天的唯一实现见下）
    SimulatedRobotBackend   模拟实现，测试与开发使用

未来接入真实机器人时，实现该接口的将是 **ROS2** 通道（rclpy 节点或复用
mfr3duo 既有 ROS2 栈），**不引入 gRPC**，也不新增 C++ 网关进程。

机器人后端失败按 §7.9 映射到封闭的 Product 错误码：

    超时                -> robot_timeout
    后端不可达          -> robot_unavailable
    机器人未就绪        -> robot_not_ready
    未持有控制权        -> control_authority_required
    机器人不存在        -> robot_not_found

模拟器是确定性的：内存中维护机器人状态，用可注入时钟把速度积分成位姿，
不访问网络。
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator, Callable
from typing import Any, Protocol, runtime_checkable

from ..errors import ProductError

DEFAULT_ROBOT_ID = "robot_1"
DEFAULT_TIMEOUT_MS = 1000

# Camera sources exposed by the simulated robot (docs §5.9).  ``head`` is
# streamable, ``wrist`` exists but is not currently streamable, so both
# ``available`` branches are real and testable.
_DEFAULT_CAMERAS: tuple[dict[str, Any], ...] = (
    {
        "camera_id": "head",
        "name": "Head camera",
        "video_source": "robot_head",
        "available": True,
    },
    {
        "camera_id": "wrist",
        "name": "Wrist camera",
        "video_source": "robot_wrist",
        "available": False,
    },
)


# ---------------------------------------------------------------------
# robot-backend errors -> Product error codes (docs §7.9)
# ---------------------------------------------------------------------
class RobotBackendError(Exception):
    """Base class for robot backend failures carrying a Product error code."""

    code = "internal_error"

    def __init__(self, message: str) -> None:
        self.message = message
        super().__init__(message)


class RobotUnavailableError(RobotBackendError):
    code = "robot_unavailable"


class RobotTimeoutError(RobotBackendError):
    code = "robot_timeout"


class RobotNotReadyError(RobotBackendError):
    code = "robot_not_ready"


class RobotNotFoundError(RobotBackendError):
    code = "robot_not_found"


class ControlAuthorityRequiredError(RobotBackendError):
    code = "control_authority_required"


class ControlAuthorityConflictError(RobotBackendError):
    code = "control_authority_conflict"


def to_product_error(exc: RobotBackendError) -> ProductError:
    """Map a robot-backend failure onto the Product error envelope."""
    return ProductError(exc.code, exc.message)


# ---------------------------------------------------------------------
# robot backend boundary (future real implementation: ROS2)
# ---------------------------------------------------------------------
@runtime_checkable
class RobotBackend(Protocol):
    """Robot backend boundary.

    今天的实现是 :class:`SimulatedRobotBackend`；未来接入真实机器人时由
    **ROS2** 实现填充该接口（不引入 gRPC，也不新增独立网关进程）。

    每个非流式调用都带显式 ``timeout_ms``；v1 不包含长期运行操作
    （docs Phase 3A「超时」）。
    """

    async def list_robots(
        self, *, timeout_ms: int = DEFAULT_TIMEOUT_MS
    ) -> list[dict[str, Any]]: ...

    async def get_robot_info(
        self, robot_id: str, *, timeout_ms: int = DEFAULT_TIMEOUT_MS
    ) -> dict[str, Any]: ...

    async def get_robot_state(
        self, robot_id: str, *, timeout_ms: int = DEFAULT_TIMEOUT_MS
    ) -> dict[str, Any]: ...

    def watch_robot_state(
        self, robot_id: str, *, interval_s: float = 0.05
    ) -> AsyncIterator[dict[str, Any]]: ...

    async def acquire_control(
        self,
        robot_id: str,
        authority_id: str,
        mode: str,
        *,
        ttl_s: float,
        timeout_ms: int = DEFAULT_TIMEOUT_MS,
    ) -> dict[str, Any]: ...

    async def release_control(
        self,
        robot_id: str,
        authority_id: str,
        *,
        timeout_ms: int = DEFAULT_TIMEOUT_MS,
    ) -> bool: ...

    async def send_velocity(
        self,
        robot_id: str,
        authority_id: str,
        command: dict[str, Any],
        *,
        timeout_ms: int = DEFAULT_TIMEOUT_MS,
    ) -> dict[str, Any]: ...

    async def stop(
        self,
        robot_id: str,
        authority_id: str | None = None,
        *,
        timeout_ms: int = DEFAULT_TIMEOUT_MS,
    ) -> bool: ...


# ---------------------------------------------------------------------
# deterministic simulator
# ---------------------------------------------------------------------
class _SimRobot:
    def __init__(
        self,
        robot_id: str,
        name: str,
        model: str,
        protocol_version: str,
        capabilities: list[str],
        cameras: list[dict[str, Any]] | None = None,
    ) -> None:
        self.robot_id = robot_id
        self.name = name
        self.model = model
        self.protocol_version = protocol_version
        self.capabilities = capabilities
        self.cameras: list[dict[str, Any]] = (
            [dict(camera) for camera in cameras] if cameras else []
        )
        self.connection = "online"
        self.mode = "idle"
        self.battery_level: float | None = 82.0
        self.charging = False
        self.pose: dict[str, Any] = {
            "frame_id": "map",
            "x": 0.0,
            "y": 0.0,
            "z": 0.0,
            "theta": 0.0,
        }
        self.velocity: dict[str, Any] = {
            "linear_x": 0.0,
            "linear_y": 0.0,
            "angular_z": 0.0,
        }
        self.joints: list[dict[str, Any]] = [
            {"name": "left_arm_joint_1", "position": 0.0, "velocity": 0.0, "effort": None},
            {"name": "right_arm_joint_1", "position": 0.0, "velocity": 0.0, "effort": None},
        ]
        self.faults: list[dict[str, Any]] = []
        self.authority_id: str | None = None
        self.authority_expires_at: float | None = None
        self.authority_mode: str | None = None
        self.last_seen_at: float | None = None
        self.last_update: float = 0.0


class SimulatedRobotBackend:
    """Offline, in-memory :class:`RobotBackend` used by tests and dev.

    Seeded with one robot whose capabilities are exactly
    ``["state", "teleoperation"]``: navigation and manipulation are *not*
    advertised (docs §4.4).  It exposes two camera sources (docs §5.9): a
    streamable head camera and a wrist camera that exists but is not currently
    streamable.
    """

    def __init__(
        self,
        *,
        clock: Callable[[], float] = time.time,
        watch_interval_s: float = 0.05,
        connection: str = "online",
    ) -> None:
        self._clock = clock
        self._watch_interval_s = watch_interval_s
        self._robots: dict[str, _SimRobot] = {
            DEFAULT_ROBOT_ID: self._seed(DEFAULT_ROBOT_ID, connection)
        }
        for robot in self._robots.values():
            robot.last_update = self._clock()
        # One-shot fault injection for deadline / unavailability mapping tests.
        self._fail_next: str | None = None

    @staticmethod
    def _seed(robot_id: str, connection: str) -> _SimRobot:
        robot = _SimRobot(
            robot_id=robot_id,
            name="MFR3Duo",
            model="mfr3duo",
            protocol_version="1.0",
            capabilities=["state", "teleoperation"],
            cameras=[dict(camera) for camera in _DEFAULT_CAMERAS],
        )
        robot.connection = connection
        return robot

    # -- test/dev control surface -------------------------------------
    def add_robot(
        self,
        robot_id: str,
        *,
        name: str = "MFR3Duo",
        model: str = "mfr3duo",
        connection: str = "online",
        cameras: list[dict[str, Any]] | None = None,
    ) -> None:
        robot = _SimRobot(
            robot_id,
            name,
            model,
            "1.0",
            ["state", "teleoperation"],
            cameras=cameras,
        )
        robot.connection = connection
        robot.last_update = self._clock()
        self._robots[robot_id] = robot

    def fail_next(self, kind: str) -> None:
        """Make the next backend call fail with ``timeout``/``unavailable``/``not_ready``."""
        self._fail_next = kind

    def set_connection(self, robot_id: str, connection: str) -> None:
        self._require(robot_id).connection = connection

    def set_battery(self, robot_id: str, level: float | None, charging: bool = False) -> None:
        robot = self._require(robot_id)
        robot.battery_level = level
        robot.charging = charging

    def inject_fault(self, robot_id: str, code: str, severity: str, message: str) -> None:
        self._require(robot_id).faults.append(
            {"code": code, "severity": severity, "message": message}
        )

    def clear_faults(self, robot_id: str) -> None:
        self._require(robot_id).faults.clear()

    # -- internals ----------------------------------------------------
    def _require(self, robot_id: str) -> _SimRobot:
        robot = self._robots.get(robot_id)
        if robot is None:
            raise RobotNotFoundError(f"Robot {robot_id!r} is unknown.")
        return robot

    def _check_timeout(self, timeout_ms: int) -> None:
        if timeout_ms <= 0:
            raise RobotTimeoutError("robot backend timeout exceeded.")

    def _maybe_fail(self) -> None:
        kind = self._fail_next
        if kind is None:
            return
        self._fail_next = None
        if kind == "timeout":
            raise RobotTimeoutError("robot backend timeout exceeded.")
        if kind == "unavailable":
            raise RobotUnavailableError("robot backend is unreachable.")
        if kind == "not_ready":
            raise RobotNotReadyError("Robot is not ready.")
        raise RobotBackendError(f"Unknown injected failure {kind!r}.")

    def _advance(self, robot: _SimRobot, now: float) -> None:
        dt = now - robot.last_update
        if dt > 0:
            robot.pose["x"] += robot.velocity["linear_x"] * dt
            robot.pose["y"] += robot.velocity["linear_y"] * dt
            robot.pose["theta"] += robot.velocity["angular_z"] * dt
        robot.last_update = now

    def _live_authority(self, robot: _SimRobot, now: float) -> str | None:
        if robot.authority_id is None:
            return None
        if robot.authority_expires_at is not None and robot.authority_expires_at <= now:
            robot.authority_id = None
            robot.authority_expires_at = None
            robot.authority_mode = None
            robot.mode = "idle"
            return None
        return robot.authority_id

    def _snapshot(self, robot: _SimRobot, now: float) -> dict[str, Any]:
        return {
            "robot_id": robot.robot_id,
            "timestamp": now,
            "connection": robot.connection,
            "mode": robot.mode,
            "battery": {"level": robot.battery_level, "charging": robot.charging},
            "pose": dict(robot.pose),
            "velocity": dict(robot.velocity),
            "joints": [dict(joint) for joint in robot.joints],
            "faults": [dict(fault) for fault in robot.faults],
        }

    # -- RobotBackend -------------------------------------------
    async def list_robots(
        self, *, timeout_ms: int = DEFAULT_TIMEOUT_MS
    ) -> list[dict[str, Any]]:
        self._check_timeout(timeout_ms)
        self._maybe_fail()
        return [
            await self.get_robot_info(robot.robot_id, timeout_ms=timeout_ms)
            for robot in self._robots.values()
        ]

    async def get_robot_info(
        self, robot_id: str, *, timeout_ms: int = DEFAULT_TIMEOUT_MS
    ) -> dict[str, Any]:
        self._check_timeout(timeout_ms)
        self._maybe_fail()
        robot = self._require(robot_id)
        now = self._clock()
        self._advance(robot, now)
        if robot.connection == "online":
            robot.last_seen_at = now
        return {
            "robot_id": robot.robot_id,
            "name": robot.name,
            "model": robot.model,
            "protocol_version": robot.protocol_version,
            "connection": robot.connection,
            "mode": robot.mode,
            "capabilities": list(robot.capabilities),
            "cameras": [dict(camera) for camera in robot.cameras],
            "last_seen_at": robot.last_seen_at,
        }

    async def get_robot_state(
        self, robot_id: str, *, timeout_ms: int = DEFAULT_TIMEOUT_MS
    ) -> dict[str, Any]:
        self._check_timeout(timeout_ms)
        self._maybe_fail()
        robot = self._require(robot_id)
        now = self._clock()
        self._advance(robot, now)
        if robot.connection == "online":
            robot.last_seen_at = now
        return self._snapshot(robot, now)

    async def watch_robot_state(
        self, robot_id: str, *, interval_s: float | None = None
    ) -> AsyncIterator[dict[str, Any]]:
        self._require(robot_id)
        interval = self._watch_interval_s if interval_s is None else interval_s
        while True:
            yield await self.get_robot_state(robot_id)
            await asyncio.sleep(interval)

    async def acquire_control(
        self,
        robot_id: str,
        authority_id: str,
        mode: str,
        *,
        ttl_s: float,
        timeout_ms: int = DEFAULT_TIMEOUT_MS,
    ) -> dict[str, Any]:
        self._check_timeout(timeout_ms)
        self._maybe_fail()
        robot = self._require(robot_id)
        now = self._clock()
        self._advance(robot, now)
        if robot.connection != "online":
            raise RobotNotReadyError("Robot is not online.")
        live = self._live_authority(robot, now)
        if live is not None and live != authority_id:
            raise ControlAuthorityConflictError(
                "Control authority is held by another holder."
            )
        robot.authority_id = authority_id
        robot.authority_mode = mode
        robot.authority_expires_at = now + ttl_s
        robot.mode = "teleoperation"
        return {
            "authority_id": authority_id,
            "robot_id": robot_id,
            "mode": mode,
            "expires_at": robot.authority_expires_at,
        }

    async def release_control(
        self,
        robot_id: str,
        authority_id: str,
        *,
        timeout_ms: int = DEFAULT_TIMEOUT_MS,
    ) -> bool:
        self._check_timeout(timeout_ms)
        self._maybe_fail()
        robot = self._require(robot_id)
        now = self._clock()
        self._advance(robot, now)
        if self._live_authority(robot, now) != authority_id:
            return False
        robot.authority_id = None
        robot.authority_expires_at = None
        robot.authority_mode = None
        robot.velocity = {"linear_x": 0.0, "linear_y": 0.0, "angular_z": 0.0}
        robot.mode = "idle"
        return True

    async def send_velocity(
        self,
        robot_id: str,
        authority_id: str,
        command: dict[str, Any],
        *,
        timeout_ms: int = DEFAULT_TIMEOUT_MS,
    ) -> dict[str, Any]:
        self._check_timeout(timeout_ms)
        self._maybe_fail()
        robot = self._require(robot_id)
        now = self._clock()
        self._advance(robot, now)
        if robot.connection != "online":
            raise RobotNotReadyError("Robot is not online.")
        if self._live_authority(robot, now) != authority_id:
            raise ControlAuthorityRequiredError("Control authority is not held.")
        robot.velocity = {
            "linear_x": float(command.get("linear_x", 0.0)),
            "linear_y": float(command.get("linear_y", 0.0)),
            "angular_z": float(command.get("angular_z", 0.0)),
        }
        return {"robot_id": robot_id, "applied": True, "velocity": dict(robot.velocity)}

    async def stop(
        self,
        robot_id: str,
        authority_id: str | None = None,
        *,
        timeout_ms: int = DEFAULT_TIMEOUT_MS,
    ) -> bool:
        self._check_timeout(timeout_ms)
        self._maybe_fail()
        robot = self._require(robot_id)
        now = self._clock()
        self._advance(robot, now)
        robot.velocity = {"linear_x": 0.0, "linear_y": 0.0, "angular_z": 0.0}
        return True


__all__ = [
    "ControlAuthorityConflictError",
    "ControlAuthorityRequiredError",
    "DEFAULT_TIMEOUT_MS",
    "DEFAULT_ROBOT_ID",
    "RobotBackendError",
    "RobotBackend",
    "RobotNotReadyError",
    "RobotNotFoundError",
    "RobotTimeoutError",
    "RobotUnavailableError",
    "SimulatedRobotBackend",
    "to_product_error",
]
