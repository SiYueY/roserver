"""Loop-owned ROS 2 backend. Native imports happen during isolated startup.

Low-level state and velocity use DDS directly. Whole-robot operations use the
existing Robot SDK's ROS Task Action, preserving physical validation/recovery.
"""
from __future__ import annotations

import asyncio
import math
import time
import uuid
from collections.abc import AsyncIterator, Callable
from typing import Any

from ..config import Settings
from .backend import (
    DEFAULT_TIMEOUT_MS, ControlAuthorityConflictError, ControlAuthorityRequiredError,
    RobotBackendError, RobotExecutionError, RobotNotFoundError, RobotNotReadyError,
    RobotRecoveryRequiredError, RobotTimeoutError, RobotUnavailableError,
)

CAMERAS = {
    "head": "head_zed_left", "front": "front_color", "rear": "rear_color",
    "left": "left_color", "right": "right_color", "wrist": "left_wrist_color",
    "right_wrist": "right_wrist_color",
}


class DclpyRobotBackend:
    is_simulated = False

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.context: Any = None
        self.node: Any = None
        self.executor: Any = None
        self._types: dict[str, Any] = {}
        self._failure: str | None = None
        self._closing = False
        self._odom: Any = None
        self._odom_seen = 0.0
        self._last_seen: float | None = None
        self._joints: dict[str, tuple[dict[str, Any], float]] = {}
        self._faults: dict[str, tuple[dict[str, Any], float]] = {}
        self._battery: Any = None
        self._battery_seen = 0.0
        self._status: Any = None
        self._status_seen = 0.0
        self._images: dict[str, tuple[Any, float]] = {}
        self._camera_subscriptions: dict[str, Any] = {}
        self._camera_users: dict[str, int] = {}
        self._observations: dict[str, tuple[Any, float]] = {}
        self._scene_joints: tuple[Any, float] | None = None
        self._observation_topics: set[str] = set()
        self._authority: str | None = None
        self._authority_deadline = 0.0
        self._operation_active = False
        self._uncertain = False
        self._active_handle: Any = None
        self._goal_future: Any = None
        self._result_future: Any = None
        self._active_runner: asyncio.Task | None = None
        self._lease_remote = False
        self._velocity: Any = None
        self._lease_client: Any = None
        self._task_client: Any = None
        self._grippers: dict[tuple[str, str], Any] = {}
        self._unconfirmed_gripper: tuple[str, dict[str, Any]] | None = None

    def _name(self, name: str) -> str:
        return self.settings.robot_namespace.rstrip("/") + "/" + name.lstrip("/")

    def _require(self, robot_id: str) -> None:
        if robot_id != self.settings.robot_id:
            raise RobotNotFoundError(f"Robot {robot_id!r} is unknown.")
        if self.context is None or self._failure or self._closing:
            raise RobotUnavailableError(self._failure or "Robot backend is not running.")

    async def startup(self) -> None:
        try:
            from dclpy import ActionClient, AsyncIOExecutor, Context, Node
            from dclpy.qos import qos_profile_sensor_data
            from diagnostic_msgs_dclpy.msg import DiagnosticArray
            from geometry_msgs_dclpy.msg import PoseStamped, Twist
            from mfr3duo_msgs_dclpy.action import ExecuteTask, Grasp, Move
            from mfr3duo_msgs_dclpy.msg import RobotStatus, TaskStep
            from mfr3duo_msgs_dclpy.srv import ControlLease
            from nav_msgs_dclpy.msg import Odometry
            from sensor_msgs_dclpy.msg import BatteryState, Image, JointState

            self._types.update(Image=Image, Twist=Twist, PoseStamped=PoseStamped, ExecuteTask=ExecuteTask, TaskStep=TaskStep,
                               ControlLease=ControlLease, Move=Move, Grasp=Grasp)
            self.context = Context(domain_id=self.settings.robot_domain_id,
                                   participant_name="roserver")
            self.node = Node("roserver", context=self.context,
                             namespace=self.settings.robot_namespace)
            self.executor = AsyncIOExecutor(self.context)
            self._velocity = self.node.create_publisher(Twist, self._name("tmr_controller/cmd_vel"), 1)
            self.node.create_subscription(Odometry, self._name("tmr_controller/odom"), self._on_odom, 10)
            self.node.create_subscription(JointState, self._name("joint_states"), self._on_joints, 10)
            self.node.create_subscription(JointState, self._name("simulation/scene/joint_states"),
                self._on_scene_joints, 10)
            self.node.create_subscription(DiagnosticArray, self._name("diagnostics"), self._on_faults, 10)
            self.node.create_subscription(BatteryState, self._name("battery_state"), self._on_battery,
                                          qos_profile_sensor_data)
            self.node.create_subscription(RobotStatus, self._name("robot/status"), self._on_status, 10)
            self.node.create_graph_event(lambda _: self._discover_observations())
            self._discover_observations()
            # Image readers are acquired by media sessions/snapshots. Their DDS
            # matches enable only the requested camera at the ROS adapter.
            self._lease_client = self.node.create_client(ControlLease, self._name("robot/control"))
            self._task_client = ActionClient(self.node, ExecuteTask, self._name("robot/execute_task"))
            for side in ("left", "right"):
                for kind, action in (("move", Move), ("grasp", Grasp)):
                    self._grippers[side, kind] = ActionClient(
                        self.node, action, self._name(f"{side}_gripper_controller/{kind}"))
            self.executor.add_node(self.node)
            self.executor.start()
            # DDS may become ready after application startup. Keep subscriptions
            # alive and expose offline until measurements arrive; no fake fallback.
        except Exception as exc:
            self._failure = f"DCLPY startup failed: {exc}"
            await self.close()
            raise RobotUnavailableError(self._failure) from exc

    def _on_odom(self, message: Any) -> None:
        self._odom = message
        self._odom_seen = time.monotonic()
        self._last_seen = time.time()

    def _on_joints(self, message: Any) -> None:
        seen = time.monotonic()
        for index, name in enumerate(message.name):
            def value(values: Any, index: int = index) -> float | None:
                return float(values[index]) if index < len(values) and math.isfinite(values[index]) else None
            self._joints[name] = ({"name": name, "position": value(message.position),
                                   "velocity": value(message.velocity), "effort": value(message.effort)}, seen)

    def _on_scene_joints(self, message: Any) -> None:
        count = len(message.name)
        if not 0 < count <= 64 or len(message.position) != count or len(message.velocity) != count:
            return
        if len(set(message.name)) != count or any(not name for name in message.name):
            return
        if any(not math.isfinite(v) for v in (*message.position, *message.velocity)):
            return
        self._scene_joints = message, time.monotonic()

    def _on_faults(self, message: Any) -> None:
        for status in message.status:
            key = str(status.name)
            if status.level == 0:
                self._faults.pop(key, None)
            else:
                self._faults[key] = ({"code": key, "severity": "critical" if status.level >= 2 else "warning",
                                      "message": status.message}, time.monotonic())

    def _on_battery(self, message: Any) -> None:
        percentage = float(message.percentage)
        self._battery = {"level": percentage * 100 if math.isfinite(percentage) and 0 <= percentage <= 1 else None,
                         "charging": message.power_supply_status == 1}
        self._battery_seen = time.monotonic()

    def _on_status(self, message: Any) -> None:
        self._status, self._status_seen = message, time.monotonic()

    def _on_image(self, camera: str, message: Any) -> None:
        self._images[camera] = message, time.monotonic()

    def _discover_observations(self) -> None:
        prefix = self._name("perception") + "/"
        for endpoint in self.context.get_graph_snapshot().topic_endpoints:
            topic = endpoint.topic_name
            relative = topic.removeprefix(prefix)
            parts = relative.split("/")
            if (not topic.startswith(prefix) or len(parts) != 3 or parts[0] not in {"objects", "tools"}
                    or parts[2] != "pose" or topic in self._observation_topics
                    or "PoseStamped" not in endpoint.wire_type or len(self._observation_topics) >= 34):
                continue
            self._observation_topics.add(topic)
            self.node.create_subscription(self._types["PoseStamped"], topic,
                lambda message, key=relative: self._observations.__setitem__(key, (message, time.monotonic())), 10)

    async def get_observations(self, robot_id: str) -> dict[str, Any]:
        self._require(robot_id)
        observations = []
        for key, (message, seen) in self._observations.items():
            if not self._fresh(seen):
                continue
            kind, identifier, _ = key.split("/")
            p, q = message.pose.position, message.pose.orientation
            observations.append({"kind": "object" if kind == "objects" else "tool", "id": identifier,
                "pose": {"frame_id": message.header.frame_id, "x": p.x, "y": p.y, "z": p.z,
                         "orientation": {"x": q.x, "y": q.y, "z": q.z, "w": q.w}}})
        if self._scene_joints and self._fresh(self._scene_joints[1]):
            message = self._scene_joints[0]
            for i, name in enumerate(message.name[:64]):
                if i < len(message.position) and i < len(message.velocity):
                    observations.append({"kind": "scene_joint", "id": name,
                                         "position": message.position[i], "velocity": message.velocity[i]})
        sdk = None
        if self._fresh(self._status_seen):
            sdk = {"ready": self._status.ready, "busy": self._status.busy,
                   "state": int(self._status.state), "mode": self._status.mode, "diagnostic": self._status.diagnostic}
        return {"robot_id": robot_id, "source": "simulation_ground_truth", "observations": observations,
                "task_server": sdk, "recovery_required": self._uncertain or (sdk is not None and sdk["state"] == 3)}

    def _fresh(self, seen: float) -> bool:
        return seen > 0 and time.monotonic() - seen <= self.settings.robot_state_timeout

    def _task_available(self) -> bool:
        return (self._fresh(self._status_seen) and self._status.ready and
                self._task_client.server_is_ready() and self._lease_client.service_is_ready())

    async def _wait_for_task_ready(self) -> bool:
        """Bridge the status tick between one terminal action and the next lease."""
        deadline = time.monotonic() + self.settings.robot_startup_timeout
        while time.monotonic() < deadline:
            if self._task_available():
                return True
            # A terminal recovery state cannot become usable without an explicit recover.
            if self._fresh(self._status_seen) and self._status.state == 3:
                return False
            await asyncio.sleep(.02)
        return self._task_available()

    async def list_robots(self, *, timeout_ms: int = DEFAULT_TIMEOUT_MS) -> list[dict[str, Any]]:
        return [await self.get_robot_info(self.settings.robot_id, timeout_ms=timeout_ms)]

    async def get_robot_info(self, robot_id: str, *, timeout_ms: int = DEFAULT_TIMEOUT_MS) -> dict[str, Any]:
        self._require(robot_id)
        if timeout_ms <= 0:
            raise RobotTimeoutError("Robot deadline expired.")
        available = self._task_available()
        capabilities = ["state", "teleoperation"]
        if available:
            capabilities += ["navigation", "manipulation", "task_sequence"]
            if self._scene_joints and self._fresh(self._scene_joints[1]):
                capabilities.append("scene_interaction")
        if any(client.server_is_ready() for client in self._grippers.values()):
            capabilities.append("gripper")
        state = await self.get_robot_state(robot_id, timeout_ms=timeout_ms)
        return {"robot_id": robot_id, "name": "MFR3Duo", "model": "mfr3duo", "protocol_version": "1.0",
                "connection": state["connection"], "mode": state["mode"], "capabilities": capabilities,
                "last_seen_at": self._last_seen,
                "cameras": [{"camera_id": camera, "name": source, "video_source": f"robot_{camera}",
                             "available": state["connection"] == "online"}
                            for camera, source in CAMERAS.items()] if self.settings.robot_camera_enabled else []}

    async def get_robot_state(self, robot_id: str, *, timeout_ms: int = DEFAULT_TIMEOUT_MS) -> dict[str, Any]:
        self._require(robot_id)
        if timeout_ms <= 0:
            raise RobotTimeoutError("Robot deadline expired.")
        fresh = self._fresh(self._odom_seen)
        pose = velocity = None
        if fresh:
            odom = self._odom
            q = odom.pose.pose.orientation
            pose = {"frame_id": odom.header.frame_id, "x": odom.pose.pose.position.x,
                    "y": odom.pose.pose.position.y, "z": odom.pose.pose.position.z,
                    "theta": math.atan2(2 * (q.w * q.z + q.x * q.y), 1 - 2 * (q.y * q.y + q.z * q.z))}
            velocity = {"linear_x": odom.twist.twist.linear.x, "linear_y": odom.twist.twist.linear.y,
                        "angular_z": odom.twist.twist.angular.z}
        # Diagnostics are latched until an explicit OK update; slow diagnostic
        # publishers must not make an unresolved fault disappear between ticks.
        faults = [value for value, _ in self._faults.values()]
        fault = self._uncertain or (self._fresh(self._status_seen) and self._status.state == 3)
        if fault:
            faults.append({"code": "robot_recovery_required", "severity": "critical",
                           "message": "Task termination or physical holding requires recovery."})
        return {"robot_id": robot_id, "timestamp": self._last_seen or time.time(),
                "connection": "online" if fresh else "offline",
                "mode": "fault" if fault else "agent" if self._operation_active else "teleoperation" if self._authority else "idle",
                "battery": self._battery if self._fresh(self._battery_seen) else None,
                "pose": pose, "velocity": velocity,
                "joints": [value for value, seen in self._joints.values() if self._fresh(seen)], "faults": faults}

    async def watch_robot_state(self, robot_id: str, *, interval_s: float = 0.05) -> AsyncIterator[dict[str, Any]]:
        while not self._closing:
            yield await self.get_robot_state(robot_id)
            await asyncio.sleep(interval_s)

    async def _lease(self, command: str, authority: str, mode: str = "teleoperation", ttl: float = 30,
                     timeout: float = 1) -> None:
        request = self._types["ControlLease"].Request(
            command=command, authority_id=authority, mode=mode, ttl_s=ttl,
            timeout_s=self.settings.robot_operation_timeout if command == "initialize" else timeout)
        response = await self._await_io(self._lease_client.call_async(request), timeout)
        if not response.success:
            error = RobotBackendError(response.message or "Robot rejected control command.")
            error.code = response.error_code or "robot_not_ready"
            raise error

    async def acquire_control(self, robot_id: str, authority_id: str, mode: str, *, ttl_s: float,
                              timeout_ms: int = DEFAULT_TIMEOUT_MS) -> dict[str, Any]:
        self._require(robot_id)
        if self._operation_active or self._uncertain:
            raise ControlAuthorityConflictError("Robot task has not terminated.")
        if self._authority and time.monotonic() < self._authority_deadline:
            raise ControlAuthorityConflictError("Robot control is already held.")
        if not self._fresh(self._odom_seen):
            raise RobotNotReadyError("Fresh odometry is required.")
        self._authority = authority_id  # Reserve across the service await.
        self._authority_deadline = time.monotonic() + ttl_s
        try:
            self._lease_remote = self._lease_client.service_is_ready()
            if self._lease_remote:
                await self._lease("acquire", authority_id, mode, ttl_s, timeout_ms / 1000)
        except BaseException:
            try:
                if self._lease_remote:
                    await self._release_lease(authority_id)
            finally:
                self._authority = None
            raise
        return {"authority_id": authority_id, "robot_id": robot_id, "mode": mode,
                "expires_at": time.time() + ttl_s}

    async def release_control(self, robot_id: str, authority_id: str, *, timeout_ms: int = DEFAULT_TIMEOUT_MS) -> bool:
        self._require(robot_id)
        if authority_id != self._authority:
            return False
        await self.stop(robot_id, authority_id, timeout_ms=timeout_ms)
        try:
            if self._lease_remote:
                await self._lease("release", authority_id, timeout=timeout_ms / 1000)
        finally:
            self._authority = None
        return True

    async def renew_control(self, robot_id: str, authority_id: str, *, ttl_s: float) -> None:
        self._require(robot_id)
        if self._authority != authority_id or time.monotonic() >= self._authority_deadline:
            raise ControlAuthorityRequiredError("Control lease is expired.")
        if self._lease_remote:
            await self._lease("renew", authority_id, ttl=ttl_s)
        self._authority_deadline = time.monotonic() + ttl_s

    async def _release_lease(self, authority: str) -> None:
        try:
            await self._lease("release", authority)
        except RobotBackendError as exc:
            # An expired/absent lease is already released. A lost service
            # response cannot prove release and must preserve the recovery gate.
            if exc.code != "control_authority_required":
                self._uncertain = True
        except BaseException:
            self._uncertain = True
            raise

    async def discard_persisted_authority(self, robot_id: str, authority_id: str) -> None:
        self._require(robot_id)
        # A previous browser connection is gone. Never resume its velocity or
        # grant its saved lease to a new connection after process restart.
        await self.stop(robot_id)
        deadline = time.monotonic() + self.settings.robot_startup_timeout
        while not self._lease_client.service_is_ready() and time.monotonic() < deadline:
            await asyncio.sleep(.1)
        if self._lease_client.service_is_ready():
            await self._lease("release", authority_id)

    async def send_velocity(self, robot_id: str, authority_id: str, command: dict[str, Any], *,
                            timeout_ms: int = DEFAULT_TIMEOUT_MS) -> dict[str, Any]:
        self._require(robot_id)
        if authority_id != self._authority or time.monotonic() >= self._authority_deadline:
            raise ControlAuthorityRequiredError("Control authority is absent or expired.")
        if self._operation_active or self._uncertain or not self._fresh(self._odom_seen):
            raise RobotNotReadyError("Robot is busy, recovering or offline.")
        values = [command[key] for key in ("linear_x", "linear_y", "angular_z")]
        if (not all(math.isfinite(v) for v in values) or math.hypot(*values[:2]) > min(.3, self.settings.robot_max_linear_velocity)
                or abs(values[2]) > min(.5, self.settings.robot_max_angular_velocity)):
            raise RobotNotReadyError("Velocity exceeds the installed controller limits.")
        message = self._types["Twist"]()
        message.linear.x, message.linear.y, message.angular.z = values
        await self._await_io(self._velocity.publish_async(message), timeout_ms / 1000)
        return {"robot_id": robot_id, "submitted": True}

    async def _await_io(self, future: Any, timeout: float) -> Any:
        try:
            return await asyncio.wait_for(future._wait_async(), timeout)
        except TimeoutError as exc:
            future.cancel()
            raise RobotTimeoutError("DDS operation deadline expired.") from exc
        except RobotBackendError:
            raise
        except Exception as exc:
            raise RobotUnavailableError(str(exc)) from exc

    async def stop(self, robot_id: str, authority_id: str | None = None, *,
                   timeout_ms: int = DEFAULT_TIMEOUT_MS) -> bool:
        self._require(robot_id)
        await self._await_io(self._velocity.publish_async(self._types["Twist"]()), timeout_ms / 1000)
        if self._active_handle is not None:
            await self._cancel_action()
        return not self._uncertain

    def camera_frame(self, source: str) -> Any:
        camera = source.removeprefix("robot_")
        if camera not in self._images or not self._fresh(self._images[camera][1]):
            raise RobotNotReadyError("No fresh camera image is available.")
        return self._images[camera][0]

    async def open_camera(self, source: str) -> None:
        from dclpy.qos import QoSProfile, ReliabilityPolicy
        self._require(self.settings.robot_id)
        camera = source.removeprefix("robot_")
        if not self.settings.robot_camera_enabled or camera not in CAMERAS:
            raise RobotNotReadyError("Camera source is unavailable.")
        if camera not in self._camera_subscriptions:
            self._images.pop(camera, None)
            self._camera_subscriptions[camera] = self.node.create_subscription(
                self._types["Image"], self._name(f"sensors/{CAMERAS[camera]}/image_raw"),
                lambda message: self._on_image(camera, message),
                QoSProfile(depth=1, reliability=ReliabilityPolicy.BEST_EFFORT))
        self._camera_users[camera] = self._camera_users.get(camera, 0) + 1
        try:
            deadline = time.monotonic() + self.settings.robot_startup_timeout
            while camera not in self._images or not self._fresh(self._images[camera][1]):
                self._require(self.settings.robot_id)
                if time.monotonic() >= deadline:
                    raise RobotNotReadyError("Camera did not deliver a fresh frame.")
                await asyncio.sleep(.02)
        except BaseException:
            self.close_camera(source)
            raise

    def close_camera(self, source: str) -> None:
        camera = source.removeprefix("robot_")
        users = self._camera_users.get(camera, 0)
        if users > 1:
            self._camera_users[camera] = users - 1
        elif users == 1:
            self._camera_users.pop(camera, None)
            subscription = self._camera_subscriptions.pop(camera, None)
            if subscription is not None:
                subscription.close()

    async def camera_snapshot(self, source: str) -> Any:
        await self.open_camera(source)
        try:
            return self.camera_frame(source)
        finally:
            self.close_camera(source)

    async def recover(self, robot_id: str) -> dict[str, Any]:
        self._require(robot_id)
        if self._operation_active or self._authority:
            raise ControlAuthorityConflictError("Release control and finish the current operation first.")
        self._operation_active = True
        self._active_runner = asyncio.current_task()
        try:
            if self._unconfirmed_gripper is not None:
                identifier, arguments = self._unconfirmed_gripper
                await self.reconcile_operation(identifier, arguments)
                if self._unconfirmed_gripper is not None:
                    raise RobotRecoveryRequiredError("The previous gripper Action still has no confirmed terminal result.")
            await self._lease("initialize", "", timeout=1)
            requested = time.monotonic()
            deadline = requested + self.settings.robot_operation_timeout
            while time.monotonic() < deadline:
                if self._status_seen > requested and self._task_available() and not self._status.busy:
                    self._uncertain = False
                    return {"robot_id": robot_id, "ready": True}
                await asyncio.sleep(.1)
            raise RobotTimeoutError("Robot recovery did not become ready.")
        except BaseException:
            self._uncertain = True
            raise
        finally:
            self._operation_active = False
            self._active_runner = None

    def _step(self, arguments: dict[str, Any]) -> Any:
        step = self._types["TaskStep"]()
        step.kind = arguments["kind"]
        step.object_id = arguments.get("object_id", "")
        step.manipulator = {"auto": 0, "left": 1, "right": 2}[arguments.get("manipulator", "auto")]
        step.timeout_s = float(arguments.get("timeout_s", self.settings.robot_operation_timeout))
        if step.kind == "scene_joint":
            step.position = float(arguments["position"])
        pose = arguments.get("pose")
        if pose is not None:
            step.has_pose = True
            step.pose.header.frame_id = pose.get("frame_id", "map" if step.kind == "navigate" else "simulation_world")
            step.pose.pose.position.x, step.pose.pose.position.y = pose["x"], pose["y"]
            step.pose.pose.position.z = pose.get("z", 0.0)
            theta = pose.get("theta", 0.0)
            step.pose.pose.orientation.z, step.pose.pose.orientation.w = math.sin(theta / 2), math.cos(theta / 2)
        return step

    async def execute_operation(self, robot_id: str, arguments: dict[str, Any],
                                feedback: Callable[[dict[str, Any]], None]) -> dict[str, Any]:
        self._require(robot_id)
        if self._authority or self._operation_active:
            raise ControlAuthorityConflictError("Control is held by teleoperation or another task.")
        if self._uncertain:
            raise RobotRecoveryRequiredError("Recover the robot before starting another operation.")
        kind = arguments["kind"]
        task_kind = kind in {"navigate", "pick", "place", "scene_joint", "sequence"}
        if task_kind and not await self._wait_for_task_ready():
            raise RobotNotReadyError("The whole-robot Task Action is not ready.")
        self._operation_active = True
        self._active_runner = asyncio.current_task()
        identifier = arguments.get("_operation_id", f"op_{uuid.uuid4().hex}")
        authority = f"task_{identifier}"
        lease = False
        timeout = float(arguments.get("timeout_s", self.settings.robot_operation_timeout))
        try:
            if self._lease_client.service_is_ready():
                lease = True  # Also clean up a submitted acquire with lost ACK.
                await self._lease("acquire", authority, "task", min(3600, timeout + self.settings.robot_terminal_timeout + 5))
            if task_kind:
                goal = self._types["ExecuteTask"].Goal(
                    authority_id=authority, timeout_s=timeout,
                    steps=[self._step(step) for step in arguments.get("steps", [arguments])])
                client = self._task_client
            else:
                side, action_kind = arguments.get("manipulator", "left"), kind.removeprefix("gripper_")
                client = self._grippers[side, action_kind]
                if not client.server_is_ready():
                    raise RobotNotReadyError("Gripper Action is unavailable.")
                goal = self._types["Move" if action_kind == "move" else "Grasp"].Goal()
                goal.width, goal.speed = arguments["width"], arguments["speed"]
                if action_kind == "grasp":
                    goal.force = arguments["force"]
                    goal.epsilon.inner = arguments.get("epsilon_inner", .005)
                    goal.epsilon.outer = arguments.get("epsilon_outer", .005)
            def on_feedback(message: Any) -> None:
                value = message.feedback
                feedback({"phase": getattr(value, "phase", kind),
                          "current_width": getattr(value, "current_width", None)})
            self._goal_future = client.send_goal_async(goal, feedback_callback=on_feedback,
                                                       goal_id=uuid.UUID(hex=identifier.removeprefix("op_")))
            if not task_kind:
                self._unconfirmed_gripper = identifier, dict(arguments)
            self._active_handle = await asyncio.wait_for(self._goal_future._wait_async(), 5)
            if not self._active_handle.accepted:
                self._unconfirmed_gripper = None
                raise RobotNotReadyError("Robot rejected the operation goal.")
            self._result_future = self._active_handle.get_result_async()
            terminal = await asyncio.wait_for(self._result_future._wait_async(), timeout)
            result = terminal.result
            if int(terminal.status) in {4, 5, 6}:
                self._unconfirmed_gripper = None
            confirmed = int(terminal.status) in {4, 5, 6} and bool(getattr(result, "termination_confirmed", True))
            if not confirmed:
                self._uncertain = True
                raise RobotRecoveryRequiredError(result.message or "Robot termination is uncertain.")
            if not result.success:
                error = RobotExecutionError(getattr(result, "message", "") or getattr(result, "error", "") or "Robot operation failed.")
                error.code = getattr(result, "error_code", "") or error.code
                raise error
            return {"robot_id": robot_id, "kind": kind, "success": True, "termination_confirmed": True,
                    "message": getattr(result, "message", ""), "action_status": int(terminal.status)}
        except (TimeoutError, asyncio.CancelledError) as exc:
            await self._cancel_action()
            if isinstance(exc, asyncio.CancelledError):
                raise
            raise RobotTimeoutError("Robot operation exceeded its deadline.") from exc
        except RobotBackendError:
            if self._goal_future is not None and self._result_future is None:
                await self._cancel_action()
            raise
        except Exception as exc:
            if self._goal_future is not None:
                await self._cancel_action()
            raise RobotUnavailableError(str(exc)) from exc
        finally:
            try:
                if lease:
                    await self._release_lease(authority)
            finally:
                self._active_handle = self._goal_future = self._result_future = None
                self._active_runner = None
                self._operation_active = False

    async def _cancel_action(self) -> None:
        try:
            if self._active_handle is None and self._goal_future is not None:
                self._active_handle = await asyncio.wait_for(self._goal_future._wait_async(), self.settings.robot_terminal_timeout)
            handle = self._active_handle
            if handle is None or not handle.accepted:
                self._unconfirmed_gripper = None
                return
            if self._result_future is None:
                self._result_future = handle.get_result_async()
            deadline = time.monotonic() + self.settings.robot_terminal_timeout
            await asyncio.wait_for(handle.cancel_goal_async()._wait_async(), max(.01, deadline - time.monotonic()))
            result = await asyncio.wait_for(self._result_future._wait_async(), max(.01, deadline - time.monotonic()))
            if int(result.status) in {4, 5, 6}:
                self._unconfirmed_gripper = None
            if int(result.status) not in {4, 5, 6} or not getattr(result.result, "termination_confirmed", True):
                self._uncertain = True
        except asyncio.CancelledError:
            self._uncertain = True
            raise
        except Exception:
            self._uncertain = True

    async def reconcile_operation(self, identifier: str, arguments: dict[str, Any]) -> None:
        # Never replay a physical operation. Query/cancel only its persisted UUID.
        from dclpy.action import ClientGoalHandle
        kind = arguments["kind"]
        if kind == "recover":
            self._uncertain = True
            return
        if kind.startswith("gripper_"):
            self._unconfirmed_gripper = identifier, dict(arguments)
        client = self._grippers[arguments.get("manipulator", "left"), kind.removeprefix("gripper_")] if kind.startswith("gripper_") else self._task_client
        deadline = time.monotonic() + self.settings.robot_startup_timeout
        while not client.server_is_ready() and time.monotonic() < deadline:
            await asyncio.sleep(.1)
        self._uncertain = True
        if client.server_is_ready():
            self._active_handle = ClientGoalHandle(client, uuid.UUID(hex=identifier.removeprefix("op_")), accepted=True)
            await self._cancel_action()
            try:
                if self._lease_client.service_is_ready():
                    await self._lease("release", f"task_{identifier}")
            except RobotBackendError:
                pass
            finally:
                self._active_handle = self._result_future = None

    async def close(self) -> None:
        if self.context is None:
            return
        try:
            if self._active_runner is not None and self._active_runner is not asyncio.current_task():
                self._active_runner.cancel()
                await asyncio.gather(self._active_runner, return_exceptions=True)
            if self._velocity is not None and self.executor is not None:
                try:
                    await self.stop(self.settings.robot_id)
                    if self._authority:
                        await self.release_control(self.settings.robot_id, self._authority)
                except Exception:
                    pass
        finally:
            self._closing = True
            try:
                if self.executor is not None:
                    await self.executor.shutdown_async(timeout=self.settings.robot_terminal_timeout)
            finally:
                await self.context.shutdown_async(timeout=self.settings.robot_terminal_timeout)
                self.context = self.node = self.executor = None
                self._images.clear()
                self._camera_subscriptions.clear()
                self._camera_users.clear()
                self._observations.clear()
