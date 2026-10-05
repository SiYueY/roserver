"""RobotService: discovery, control authority and teleoperation (docs §4.3-§4.9).

All of this runs on the single owning event loop.  The robot backend is async, the
control authority is persisted in ``robot_authorities`` through the
ApplicationStore, and the command watchdog is server-side and independent of
the browser (docs §4.8).
"""

from __future__ import annotations

import asyncio
import math
import time
import uuid
from collections import deque
from datetime import datetime, timezone
from typing import Any

from ..agent.schema import rfc3339
from ..config import Settings
from ..errors import ProductError
from ..store.application import ApplicationStore
from .backend import (
    DEFAULT_TIMEOUT_MS,
    RobotBackendError,
    RobotBackend,
    to_product_error,
)
from .operations import RobotOperations

TELEOP_MODE = "teleoperation"
MODE_IDLE = "idle"
_MODE_TELEOP = "teleoperation"

# The five documented §4.5 events plus the §4.9 teleoperation events.
ROBOT_EVENT_TYPES = frozenset(
    {
        "robot.state",
        "robot.connection_changed",
        "robot.mode_changed",
        "robot.fault",
        "control.authority_changed",
        "robot.operation",
    }
)
TELEOP_EVENT_TYPES = frozenset(
    {"control_lost", "authority_expired", "command_rejected", "robot_fault"}
)

_COMPARE_KEYS = (
    "connection",
    "mode",
    "battery",
    "pose",
    "velocity",
    "joints",
    "faults",
)


def _number(value: object) -> float | None:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value) if math.isfinite(value) else None
    return None


def _parse_rfc3339(value: object) -> float | None:
    if not isinstance(value, str) or not value:
        return None
    text = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        moment = datetime.fromisoformat(text)
    except ValueError:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.timestamp()


def robot_object(info: dict[str, Any]) -> dict[str, Any]:
    """Project robot-backend robot info onto the §4.5 Robot object (snake_case).

    ``cameras`` is always present (``[]`` when the robot exposes none) so
    rodesk's ``CameraSourceMenu`` / ``SourceCameraAdapter`` can use it as the
    single source of truth for ``cameraId ↔ video_source`` (docs §5.9).
    """
    return {
        "robot_id": info.get("robot_id"),
        "name": info.get("name"),
        "model": info.get("model"),
        "protocol_version": info.get("protocol_version"),
        "connection": info.get("connection"),
        "mode": info.get("mode"),
        "capabilities": list(info.get("capabilities") or []),
        "cameras": [
            {
                "camera_id": camera.get("camera_id"),
                "name": camera.get("name"),
                "video_source": camera.get("video_source"),
                # Contract: boolean.  A missing value means "not streamable".
                "available": bool(camera.get("available", False)),
            }
            for camera in info.get("cameras") or []
        ],
        "last_seen_at": rfc3339(info.get("last_seen_at")),
    }


def robot_state_object(raw: dict[str, Any]) -> dict[str, Any]:
    """Project robot-backend state onto the §4.5 RobotState (unknown values as null)."""
    battery = raw.get("battery")
    pose = raw.get("pose")
    velocity = raw.get("velocity")
    joints = []
    for joint in raw.get("joints") or []:
        joints.append(
            {
                "name": joint.get("name"),
                "position": joint.get("position"),
                "velocity": joint.get("velocity"),
                "effort": joint.get("effort"),
            }
        )
    return {
        "robot_id": raw.get("robot_id"),
        "timestamp": rfc3339(raw.get("timestamp")),
        "connection": raw.get("connection"),
        "mode": raw.get("mode"),
        "battery": None
        if battery is None
        else {
            "level": battery.get("level"),
            "charging": bool(battery.get("charging", False)),
        },
        "pose": None
        if pose is None
        else {
            "frame_id": pose.get("frame_id"),
            "x": pose.get("x"),
            "y": pose.get("y"),
            "z": pose.get("z"),
        },
        "velocity": None
        if velocity is None
        else {
            "linear_x": velocity.get("linear_x"),
            "linear_y": velocity.get("linear_y"),
            "angular_z": velocity.get("angular_z"),
        },
        "joints": joints,
        "faults": list(raw.get("faults") or []),
    }


class RobotSubscription:
    """One live consumer of a robot's event stream."""

    __slots__ = ("robot_id", "queue", "closed")

    def __init__(self, robot_id: str) -> None:
        self.robot_id = robot_id
        self.queue: asyncio.Queue[tuple[str, Any]] = asyncio.Queue()
        self.closed = False

    def close(self) -> None:
        if not self.closed:
            self.closed = True
            self.queue.put_nowait(("closed", None))


class RobotService:
    """Product-facing robot capability layer over a :class:`RobotBackend`."""

    def __init__(
        self,
        *,
        settings: Settings,
        backend: RobotBackend,
        store: ApplicationStore,
    ) -> None:
        self.settings = settings
        self.backend = backend
        self.store = store
        self.owner_id = settings.owner_id

        self._subscribers: dict[str, set[RobotSubscription]] = {}
        self._watchers: dict[str, asyncio.Task[None]] = {}
        self._watchdogs: dict[str, asyncio.Task[None]] = {}
        self._observed: dict[str, dict[str, Any]] = {}
        self._last_commands: dict[str, float] = {}
        self._last_sequences: dict[str, int] = {}
        self._recent_commands: dict[str, deque[float]] = {}
        self.operations = RobotOperations(self)

    # =================================================================
    # lifecycle
    # =================================================================
    async def startup(self) -> None:
        # One-time connectivity probe.  A real ROS2/DDS backend may need to join a
        # domain here; failures propagate so the application lifespan can isolate
        # them and keep serving core features.
        startup = getattr(self.backend, "startup", None)
        if startup is not None:
            await startup()
        await self._guard(self.backend.list_robots(timeout_ms=DEFAULT_TIMEOUT_MS))
        await self.operations.startup()
        # Persisted browser leases are never resumed across a process restart.
        discard = getattr(self.backend, "discard_persisted_authority", None)
        if discard is not None:
            rows = await asyncio.to_thread(self.store._query,
                "SELECT robot_id,authority_id FROM robot_authorities WHERE owner_id=?", (self.owner_id,))
            for row in rows:
                if row["authority_id"]:
                    await self._guard(discard(row["robot_id"], row["authority_id"]))
        await asyncio.to_thread(self.store._execute,
            "DELETE FROM robot_authorities WHERE owner_id=?", (self.owner_id,))

    async def shutdown(self) -> None:
        await self.operations.close()
        tasks = [*self._watchers.values(), *self._watchdogs.values()]
        self._watchers.clear()
        self._watchdogs.clear()
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        for subscriptions in self._subscribers.values():
            for subscription in subscriptions:
                subscription.close()
        self._subscribers.clear()
        close = getattr(self.backend, "close", None)
        if close is not None:
            await close()

    def _cancel_watchdog(self, robot_id: str) -> None:
        task = self._watchdogs.pop(robot_id, None)
        if (
            task is not None
            and not task.done()
            and task is not asyncio.current_task()
        ):
            task.cancel()

    def _ensure_watchdog(self, robot_id: str) -> None:
        task = self._watchdogs.get(robot_id)
        if task is None or task.done():
            self._watchdogs[robot_id] = asyncio.create_task(
                self._watchdog_loop(robot_id)
            )

    # =================================================================
    # discovery / state (docs §4.3, §4.5)
    # =================================================================
    async def _guard(self, awaitable: Any) -> Any:
        try:
            return await awaitable
        except RobotBackendError as exc:
            raise to_product_error(exc) from exc

    async def list_robots(self) -> dict[str, Any]:
        infos = await self._guard(self.backend.list_robots(timeout_ms=DEFAULT_TIMEOUT_MS))
        return {
            "items": [robot_object(info) for info in infos],
            "next_cursor": None,
        }

    async def get_robot(self, robot_id: str) -> dict[str, Any]:
        info = await self._guard(
            self.backend.get_robot_info(robot_id, timeout_ms=DEFAULT_TIMEOUT_MS)
        )
        return robot_object(info)

    async def snapshot_state(self, robot_id: str) -> dict[str, Any]:
        """RobotState projection without the online gate (for event snapshots)."""
        raw = await self._guard(
            self.backend.get_robot_state(robot_id, timeout_ms=DEFAULT_TIMEOUT_MS)
        )
        return robot_state_object(raw)

    async def get_robot_state(self, robot_id: str) -> dict[str, Any]:
        raw = await self._guard(
            self.backend.get_robot_state(robot_id, timeout_ms=DEFAULT_TIMEOUT_MS)
        )
        if raw.get("connection") != "online":
            raise ProductError("robot_not_ready", "Robot is not online.")
        return robot_state_object(raw)

    async def _require_online(self, robot_id: str) -> dict[str, Any]:
        info = await self._guard(
            self.backend.get_robot_info(robot_id, timeout_ms=DEFAULT_TIMEOUT_MS)
        )
        if info.get("connection") != "online":
            raise ProductError("robot_not_ready", "Robot is not online.")
        return info

    # =================================================================
    # control authority (docs §4.6)
    # =================================================================
    async def acquire_authority(
        self,
        robot_id: str,
        *,
        mode: str = TELEOP_MODE,
        holder_id: str | None = None,
        ttl_s: float | None = None,
    ) -> dict[str, Any]:
        if mode != TELEOP_MODE:
            raise ProductError(
                "invalid_input", "Only teleoperation control authority is supported."
            )
        if ttl_s is not None and (not math.isfinite(ttl_s) or ttl_s <= 0 or ttl_s > 3600):
            raise ProductError("invalid_input", "ttl_s must be positive.")
        await self._require_online(robot_id)
        now = time.time()
        ttl = ttl_s if ttl_s is not None else self.settings.robot_authority_ttl
        authority_id = f"auth_{uuid.uuid4().hex}"
        expires_at = now + ttl
        conflict = await self.store.acquire_robot_authority(
            robot_id=robot_id,
            owner_id=self.owner_id,
            authority_id=authority_id,
            holder_id=holder_id or self.owner_id,
            mode=mode,
            now=now,
            expires_at=expires_at,
        )
        if conflict is not None:
            raise ProductError(
                "control_authority_conflict",
                "Control authority is held by another holder.",
            )
        try:
            await self.backend.acquire_control(
                robot_id, authority_id, mode, ttl_s=ttl
            )
        except RobotBackendError as exc:
            await self.store.release_robot_authority(robot_id, self.owner_id, authority_id)
            raise to_product_error(exc) from exc
        self._set_mode(robot_id, _MODE_TELEOP)
        self._publish(
            robot_id,
            self._event(
                robot_id,
                "control.authority_changed",
                {
                    "robot_id": robot_id,
                    "authority_id": authority_id,
                    "mode": mode,
                    "holder": holder_id or self.owner_id,
                },
            ),
        )
        self._ensure_watchdog(robot_id)
        return {
            "authority_id": authority_id,
            "robot_id": robot_id,
            "mode": mode,
            "expires_at": rfc3339(expires_at),
        }

    async def release_authority(
        self, robot_id: str, *, authority_id: str | None = None
    ) -> dict[str, Any]:
        record = await self.store.get_robot_authority(robot_id, self.owner_id)
        if record is None:
            return {"robot_id": robot_id, "released": False}
        if authority_id is not None and record.get("authority_id") != authority_id:
            raise ProductError(
                "control_authority_required",
                "Control authority is held by another holder.",
            )
        current = record.get("authority_id")
        if current is not None:
            try:
                await self.backend.release_control(robot_id, current)
            except RobotBackendError:  # pragma: no cover - release must not fail closed
                pass
        await self.store.release_robot_authority(robot_id, self.owner_id)
        self._cancel_watchdog(robot_id)
        self._last_commands.pop(robot_id, None)
        self._set_mode(robot_id, MODE_IDLE)
        self._publish(
            robot_id,
            self._event(
                robot_id,
                "control.authority_changed",
                {
                    "robot_id": robot_id,
                    "authority_id": None,
                    "mode": None,
                    "holder": None,
                },
            ),
        )
        return {"robot_id": robot_id, "released": True}

    async def get_authority(self, robot_id: str) -> dict[str, Any]:
        record = await self.store.get_robot_authority(robot_id, self.owner_id)
        now = time.time()
        if (
            record is not None
            and record.get("expires_at") is not None
            and float(record["expires_at"]) <= now
        ):
            await self._expire_authority(robot_id, record)
            record = None
        if record is None:
            return {"robot_id": robot_id, "authority": None}
        return {
            "robot_id": robot_id,
            "authority": {
                "authority_id": record.get("authority_id"),
                "robot_id": robot_id,
                "mode": record.get("mode") or TELEOP_MODE,
                "expires_at": rfc3339(record.get("expires_at")),
            },
        }

    async def renew_authority(self, robot_id: str, authority_id: str) -> dict[str, Any]:
        record = await self.store.get_robot_authority(robot_id, self.owner_id)
        now = time.time()
        if record is None or record.get("authority_id") != authority_id or record["expires_at"] <= now:
            raise ProductError("control_authority_required", "Control authority is absent or expired.")
        renew = getattr(self.backend, "renew_control", None)
        if renew is None:
            raise ProductError("robot_not_ready", "Backend cannot renew control authority.")
        await self._guard(renew(robot_id, authority_id, ttl_s=self.settings.robot_authority_ttl))
        expires = now + self.settings.robot_authority_ttl
        await asyncio.to_thread(self.store._execute,
            "UPDATE robot_authorities SET expires_at=? WHERE robot_id=? AND owner_id=? AND authority_id=?",
            (expires, robot_id, self.owner_id, authority_id))
        return {"authority_id": authority_id, "robot_id": robot_id, "mode": record["mode"], "expires_at": rfc3339(expires)}

    async def has_authority(self, robot_id: str) -> bool:
        record = await self.store.get_robot_authority(robot_id, self.owner_id)
        if record is None:
            return False
        expires_at = record.get("expires_at")
        return expires_at is None or float(expires_at) > time.time()

    # =================================================================
    # teleoperation (docs §4.7-§4.9)
    # =================================================================
    async def handle_velocity_command(
        self, robot_id: str, command: dict[str, Any]
    ) -> dict[str, Any]:
        now = time.time()
        record = await self.store.get_robot_authority(robot_id, self.owner_id)
        if record is None or record.get("authority_id") != command.get("authority_id"):
            return self._reject(robot_id, "control_authority_required")
        expires_at = record.get("expires_at")
        if expires_at is not None and float(expires_at) <= now:
            await self._expire_authority(robot_id, record)
            return self._reject(robot_id, "authority_expired")

        if command.get("type") != "velocity":
            return self._reject(robot_id, "unsupported_command")

        # sequence monotonicity
        sequence = command.get("sequence")
        if not isinstance(sequence, int) or isinstance(sequence, bool) or sequence < 0:
            return self._reject(robot_id, "invalid_input")
        last_sequence = self._last_sequences.get(robot_id)
        if last_sequence is not None and sequence <= last_sequence:
            return self._reject(robot_id, "sequence_not_monotonic")

        # stale client timestamp
        client_timestamp = _parse_rfc3339(command.get("client_timestamp"))
        if client_timestamp is None:
            return self._reject(robot_id, "invalid_input")
        stale = self.settings.robot_stale_command_seconds
        age = now - client_timestamp
        if age > stale or age < -stale:
            return self._reject(robot_id, "stale_timestamp")

        # velocity limit
        linear_x = _number(command.get("linear_x"))
        linear_y = _number(command.get("linear_y"))
        angular_z = _number(command.get("angular_z"))
        if linear_x is None or linear_y is None or angular_z is None:
            return self._reject(robot_id, "invalid_input")
        if (
            math.hypot(linear_x, linear_y) > min(self.settings.robot_max_linear_velocity, .3 if not getattr(self.backend, "is_simulated", True) else self.settings.robot_max_linear_velocity)
            or abs(angular_z) > min(self.settings.robot_max_angular_velocity, .5 if not getattr(self.backend, "is_simulated", True) else self.settings.robot_max_angular_velocity)
        ):
            return self._reject(robot_id, "velocity_limit")

        # rate limit
        window = self._recent_commands.setdefault(robot_id, deque())
        while window and now - window[0] > 1.0:
            window.popleft()
        if len(window) >= self.settings.robot_rate_limit:
            return self._reject(robot_id, "rate_limited")

        # deadman / watchdog
        if command.get("deadman") is not True:
            self._last_sequences[robot_id] = sequence
            await self._force_zero(robot_id, reason="deadman")
            return self._feedback(robot_id, accepted=True, now=now)

        try:
            await self.backend.send_velocity(
                robot_id,
                str(record["authority_id"]),
                {
                    "linear_x": linear_x,
                    "linear_y": linear_y,
                    "angular_z": angular_z,
                },
            )
        except RobotBackendError as exc:
            return self._reject(robot_id, exc.code)

        self._last_sequences[robot_id] = sequence
        self._last_commands[robot_id] = now
        window.append(now)
        return self._feedback(robot_id, accepted=True, now=now)

    async def on_teleop_disconnect(self, robot_id: str) -> None:
        """Connection closed -> zero command (docs §4.8)."""
        try:
            await self._force_zero(robot_id, reason="disconnect")
        except Exception:  # pragma: no cover - best effort on teardown
            return

    def _feedback(
        self, robot_id: str, *, accepted: bool, now: float
    ) -> dict[str, Any]:
        last_sequence = self._last_sequences.get(robot_id, 0)
        last_command = self._last_commands.get(robot_id)
        if last_command is None:
            remaining = 0
        else:
            elapsed = max(0.0, now - last_command)
            remaining = max(
                0,
                int(
                    (self.settings.robot_watchdog_timeout - elapsed) * 1000
                ),
            )
        return {
            "type": "teleop.feedback",
            "last_sequence": last_sequence,
            "accepted": accepted,
            "watchdog_remaining_ms": remaining,
        }

    def _reject(self, robot_id: str, reason: str) -> dict[str, Any]:
        """One rejection == one message, so the reason is never mis-attributed.

        Carries the §4.9 feedback fields (``last_sequence`` / ``accepted`` /
        ``watchdog_remaining_ms``) alongside the documented ``command_rejected``
        reason.  A rejected command never advances the accepted-sequence cursor.
        """
        feedback = self._feedback(robot_id, accepted=False, now=time.time())
        return {
            "type": "command_rejected",
            "robot_id": robot_id,
            "reason": reason,
            "last_sequence": feedback["last_sequence"],
            "accepted": False,
            "watchdog_remaining_ms": feedback["watchdog_remaining_ms"],
        }

    async def _force_zero(self, robot_id: str, *, reason: str) -> None:
        self._last_commands.pop(robot_id, None)
        record = await self.store.get_robot_authority(robot_id, self.owner_id)
        authority_id = record.get("authority_id") if record else None
        try:
            await self.backend.stop(robot_id, authority_id)
        except RobotBackendError:  # pragma: no cover - safety path must not raise
            pass
        self._publish(
            robot_id,
            self._event(
                robot_id,
                "control_lost",
                {"robot_id": robot_id, "reason": reason},
            ),
        )

    async def _expire_authority(self, robot_id: str, record: dict[str, Any]) -> None:
        self._last_commands.pop(robot_id, None)
        current = record.get("authority_id")
        if current is not None:
            try:
                await self.backend.release_control(robot_id, current)
            except RobotBackendError:  # pragma: no cover - expiry must not fail closed
                pass
        await self.store.release_robot_authority(robot_id, self.owner_id)
        self._cancel_watchdog(robot_id)
        self._publish(
            robot_id,
            self._event(
                robot_id,
                "authority_expired",
                {"robot_id": robot_id, "authority_id": current},
            ),
        )
        self._publish(
            robot_id,
            self._event(
                robot_id,
                "control.authority_changed",
                {
                    "robot_id": robot_id,
                    "authority_id": None,
                    "mode": None,
                    "holder": None,
                },
            ),
        )
        self._set_mode(robot_id, MODE_IDLE)

    async def _watchdog_loop(self, robot_id: str) -> None:
        try:
            while True:
                await asyncio.sleep(self.settings.robot_watchdog_tick)
                record = await self.store.get_robot_authority(robot_id, self.owner_id)
                if record is None:
                    return
                now = time.time()
                expires_at = record.get("expires_at")
                if expires_at is not None and float(expires_at) <= now:
                    await self._expire_authority(robot_id, record)
                    return
                last_command = self._last_commands.get(robot_id)
                if (
                    last_command is not None
                    and now - last_command > self.settings.robot_watchdog_timeout
                ):
                    await self._force_zero(robot_id, reason="watchdog")
        except asyncio.CancelledError:
            raise

    # =================================================================
    # event fan-out (docs §4.5)
    # =================================================================
    def _event(self, robot_id: str, event_type: str, data: dict[str, Any]) -> dict[str, Any]:
        return {
            "type": event_type,
            "timestamp": rfc3339(time.time()),
            "robot_id": robot_id,
            "data": data,
        }

    def _publish(self, robot_id: str, event: dict[str, Any]) -> None:
        for subscription in tuple(self._subscribers.get(robot_id, ())):
            if not subscription.closed:
                subscription.queue.put_nowait(("event", event))

    def subscribe(self, robot_id: str) -> RobotSubscription:
        subscription = RobotSubscription(robot_id)
        self._subscribers.setdefault(robot_id, set()).add(subscription)
        self._ensure_watcher(robot_id)
        return subscription

    def unsubscribe(self, subscription: RobotSubscription) -> None:
        subscriptions = self._subscribers.get(subscription.robot_id)
        if subscriptions is not None:
            subscriptions.discard(subscription)
            if not subscriptions:
                self._subscribers.pop(subscription.robot_id, None)
                task = self._watchers.pop(subscription.robot_id, None)
                if task is not None and not task.done():
                    task.cancel()
        subscription.close()

    def _ensure_watcher(self, robot_id: str) -> None:
        task = self._watchers.get(robot_id)
        if task is None or task.done():
            self._watchers[robot_id] = asyncio.create_task(self._watch_loop(robot_id))

    async def _watch_loop(self, robot_id: str) -> None:
        try:
            async for raw in self.backend.watch_robot_state(
                robot_id, interval_s=self.settings.robot_watch_interval
            ):
                self._emit_state(robot_id, raw)
        except asyncio.CancelledError:
            raise
        except RobotBackendError:
            self._mark_offline(robot_id)
        except Exception:  # pragma: no cover - defensive stream guard
            return

    def _mark_offline(self, robot_id: str) -> None:
        previous = self._observed.get(robot_id)
        if previous is None or previous.get("connection") == "offline":
            return
        previous_connection = previous.get("connection")
        previous["connection"] = "offline"
        self._publish(
            robot_id,
            self._event(
                robot_id,
                "robot.connection_changed",
                {
                    "robot_id": robot_id,
                    "previous": previous_connection,
                    "current": "offline",
                },
            ),
        )

    def _emit_state(self, robot_id: str, raw: dict[str, Any]) -> None:
        previous = self._observed.get(robot_id)
        if previous is None:
            # The events WS sends its own connect-time robot.state snapshot, so
            # the first observation only seeds the diff baseline.
            self._observed[robot_id] = raw
            return
        if raw.get("connection") != previous.get("connection"):
            self._publish(
                robot_id,
                self._event(
                    robot_id,
                    "robot.connection_changed",
                    {
                        "robot_id": robot_id,
                        "previous": previous.get("connection"),
                        "current": raw.get("connection"),
                    },
                ),
            )
        if raw.get("mode") != previous.get("mode"):
            self._publish(
                robot_id,
                self._event(
                    robot_id,
                    "robot.mode_changed",
                    {
                        "robot_id": robot_id,
                        "previous": previous.get("mode"),
                        "current": raw.get("mode"),
                    },
                ),
            )
        known = {fault.get("code") for fault in previous.get("faults") or []}
        for fault in raw.get("faults") or []:
            if fault.get("code") not in known:
                self._publish(
                    robot_id,
                    self._event(
                        robot_id,
                        "robot.fault",
                        {"robot_id": robot_id, "fault": dict(fault)},
                    ),
                )
        if not any(previous.get(key) != raw.get(key) for key in _COMPARE_KEYS):
            return
        self._observed[robot_id] = raw
        self._publish(
            robot_id,
            self._event(
                robot_id,
                "robot.state",
                {"robot_id": robot_id, "state": robot_state_object(raw)},
            ),
        )

    def _set_mode(self, robot_id: str, mode: str) -> None:
        observed = self._observed.get(robot_id)
        previous = observed.get("mode") if observed is not None else None
        if previous == mode:
            return
        if observed is not None:
            observed["mode"] = mode
        self._publish(
            robot_id,
            self._event(
                robot_id,
                "robot.mode_changed",
                {"robot_id": robot_id, "previous": previous, "current": mode},
            ),
        )


__all__ = [
    "ROBOT_EVENT_TYPES",
    "TELEOP_EVENT_TYPES",
    "RobotService",
    "RobotSubscription",
    "robot_object",
    "robot_state_object",
]
