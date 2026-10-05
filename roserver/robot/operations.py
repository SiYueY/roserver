"""Bounded product operations, with actual ROS terminal confirmation on cancel."""
from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import math
import time
import uuid
from typing import Any

from ..errors import ProductError
from ..agent.schema import rfc3339
from .backend import RobotBackendError

KINDS = {"navigate", "pick", "place", "scene_joint", "sequence", "gripper_move", "gripper_grasp", "recover"}
TERMINAL = {"succeeded", "failed", "cancelled"}


def validate_operation(value: dict[str, Any], default_timeout: float) -> dict[str, Any]:
    result = copy.deepcopy(value)
    kind = result.get("kind")
    if not isinstance(kind, str) or kind not in KINDS:
        raise ProductError("invalid_input", "Unsupported robot operation kind.")
    timeout = result.setdefault("timeout_s", default_timeout)
    if type(timeout) not in (int, float) or not math.isfinite(timeout) or not .001 <= timeout <= 3600:
        raise ProductError("invalid_input", "timeout_s must be finite and in 0.001..3600.")
    hand = result.get("manipulator", "auto" if kind in {"pick", "place"} else "left")
    if not isinstance(hand, str) or hand not in ({"left", "right"} if kind.startswith("gripper_") else {"auto", "left", "right"}):
        raise ProductError("invalid_input", "Invalid manipulator selection.")
    if kind == "sequence":
        steps = result.get("steps")
        if not isinstance(steps, list) or not 1 <= len(steps) <= 32:
            raise ProductError("invalid_input", "A sequence requires 1..32 steps.")
        if any(not isinstance(step, dict) or not isinstance(step.get("kind"), str)
               or step["kind"] not in {"navigate", "pick", "place", "scene_joint"} for step in steps):
            raise ProductError("invalid_input", "Unsupported task sequence step.")
        result["steps"] = [validate_operation(step, timeout) for step in steps]
    if kind in {"pick", "place", "scene_joint"}:
        identifier = result.get("object_id")
        if not isinstance(identifier, str) or not identifier or len(identifier) > 128:
            raise ProductError("invalid_input", "A bounded object_id is required.")
    if kind == "scene_joint":
        position = result.get("position")
        if not isinstance(position, (int, float)) or isinstance(position, bool) or not math.isfinite(position):
            raise ProductError("invalid_input", "A finite scene joint position is required.")
    if kind in {"navigate", "place"} and result.get("pose") is None:
        raise ProductError("invalid_input", "A target pose is required.")
    if result.get("pose") is not None:
        pose = result["pose"]
        if not isinstance(pose, dict):
            raise ProductError("invalid_input", "pose must be an object.")
        for name in ("x", "y", "z", "theta"):
            coordinate = pose.get(name, 0.0 if name in {"z", "theta"} else None)
            if not isinstance(coordinate, (int, float)) or isinstance(coordinate, bool) or not math.isfinite(coordinate):
                raise ProductError("invalid_input", "Pose coordinates must be finite numbers.")
        frame = pose.get("frame_id", "map" if kind == "navigate" else "simulation_world")
        if not isinstance(frame, str) or not frame or len(frame) > 128:
            raise ProductError("invalid_input", "A valid pose frame_id is required.")
    if kind.startswith("gripper_"):
        for name, lower, upper in (("width", 0, .08), ("speed", .0001, .2), ("force", .001, 100)):
            if name == "force" and kind != "gripper_grasp":
                continue
            number = result.get(name)
            if not isinstance(number, (int, float)) or isinstance(number, bool) or not math.isfinite(number) or not lower <= number <= upper:
                raise ProductError("invalid_input", f"Invalid gripper {name}.")
        for name in ("epsilon_inner", "epsilon_outer"):
            number = result.get(name, .005)
            if type(number) not in (int, float) or not math.isfinite(number) or not 0 <= number <= .08:
                raise ProductError("invalid_input", "Invalid gripper tolerance.")
    return result


class RobotOperations:
    def __init__(self, service: Any) -> None:
        self.service = service
        self._records: dict[str, dict[str, Any]] = {}
        self._tasks: dict[str, asyncio.Task] = {}
        self._closing = False
        self._admission = asyncio.Lock()

    async def startup(self) -> None:
        store, owner = self.service.store, self.service.owner_id
        rows = await asyncio.to_thread(store._query,
            "SELECT * FROM robot_operations WHERE owner_id=? ORDER BY updated_at DESC", (owner,))
        for row in reversed(rows):
            record = json.loads(row["record_json"])
            pending = record["status"] not in TERMINAL
            recovery = ((record.get("error") or {}).get("code") == "robot_recovery_required"
                        and not record.get("recovery_resolved", False))
            if pending or recovery:
                reconcile = getattr(self.service.backend, "reconcile_operation", None)
                if reconcile is not None:
                    await reconcile(record["operation_id"], json.loads(row["arguments_json"]))
            if pending:
                record.update(status="failed", finished_at=rfc3339(time.time()),
                              error={"code": "server_restarted", "message": "Server restarted during robot operation; the operation was not replayed."})
                await self._save(record)
            self._records[record["operation_id"]] = record
        for identifier in tuple(self._records)[:-256]:
            self._records.pop(identifier)
        # Keep restart reconciliation and idempotency bounded by the same
        # retention horizon used by the other Product commands.
        cutoff = time.time() - self.service.settings.idempotency_retention
        await asyncio.to_thread(store._execute,
            "DELETE FROM robot_operations WHERE owner_id=? AND updated_at<? "
            "AND json_extract(record_json,'$.status') IN ('succeeded','failed','cancelled')", (owner, cutoff))

    async def _save(self, record: dict[str, Any]) -> None:
        await asyncio.to_thread(self.service.store._execute,
            "UPDATE robot_operations SET record_json=?, updated_at=? WHERE operation_id=? AND owner_id=?",
            (json.dumps(record, allow_nan=False), time.time(), record["operation_id"], self.service.owner_id))

    async def start(self, robot_id: str, arguments: dict[str, Any], idempotency_key: str | None = None) -> dict[str, Any]:
        async with self._admission:
            return await self._start(robot_id, arguments, idempotency_key)

    async def _start(self, robot_id: str, arguments: dict[str, Any], idempotency_key: str | None) -> dict[str, Any]:
        if self._closing:
            raise ProductError("robot_unavailable", "Robot operations are shutting down.")
        payload = validate_operation(arguments, self.service.settings.robot_operation_timeout)
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False)
        if len(encoded.encode()) > self.service.settings.max_agent_input_bytes:
            raise ProductError("payload_too_large", "Robot operation input exceeds the payload limit.")
        digest = hashlib.sha256(encoded.encode()).hexdigest()
        if idempotency_key is not None:
            if not 1 <= len(idempotency_key) <= 128:
                raise ProductError("invalid_input", "Idempotency-Key must contain 1..128 characters.")
            rows = await asyncio.to_thread(self.service.store._query,
                "SELECT * FROM robot_operations WHERE owner_id=? AND robot_id=? AND idempotency_key=?",
                (self.service.owner_id, robot_id, idempotency_key))
            if rows:
                if rows[0]["arguments_digest"] != digest:
                    raise ProductError("idempotency_conflict", "Idempotency-Key was used with different operation input.")
                identifier = rows[0]["operation_id"]
                return copy.deepcopy(self._records.get(identifier) or json.loads(rows[0]["record_json"]))
        backend = self.service.backend
        if not hasattr(backend, "execute_operation"):
            raise ProductError("robot_not_ready", "This backend has no real robot operations.")
        if any(record["status"] not in TERMINAL for record in self._records.values()):
            raise ProductError("control_authority_conflict", "A robot operation is active.")
        if await self.service.has_authority(robot_id):
            raise ProductError("control_authority_conflict", "Teleoperation owns robot control.")
        await self.service.get_robot(robot_id)
        # Recheck after awaits so two concurrent Product requests cannot both reserve.
        if any(record["status"] not in TERMINAL for record in self._records.values()):
            raise ProductError("control_authority_conflict", "A robot operation is active.")
        while len(self._records) >= 256:
            oldest = next(iter(self._records))
            if self._records[oldest]["status"] not in TERMINAL:
                raise ProductError("rate_limited", "Robot operation capacity exhausted.")
            self._records.pop(oldest)
        identifier = f"op_{uuid.uuid4().hex}"
        record = {"operation_id": identifier, "robot_id": robot_id, "kind": payload["kind"],
                  "status": "pending", "created_at": rfc3339(time.time()), "finished_at": None,
                  "feedback": None, "result": None, "error": None}
        self._records[identifier] = record
        insert = asyncio.create_task(asyncio.to_thread(self.service.store._execute,
            "INSERT INTO robot_operations VALUES (?,?,?,?,?,?,?,?)",
            (identifier, self.service.owner_id, robot_id, idempotency_key, digest, encoded,
             json.dumps(record), time.time())))
        try:
            await asyncio.shield(insert)
        except asyncio.CancelledError:
            await insert
            record.update(status="cancelled", finished_at=rfc3339(time.time()),
                          error={"code": "run_cancelled", "message": "Operation admission cancelled before dispatch."})
            await self._save(record)
            raise
        except Exception:
            self._records.pop(identifier, None)
            raise
        payload["_operation_id"] = identifier
        self._tasks[identifier] = asyncio.create_task(self._run(record, payload))
        self._publish(record)
        return copy.deepcopy(record)

    def _publish(self, record: dict[str, Any]) -> None:
        robot = record["robot_id"]
        self.service._publish(robot, self.service._event(robot, "robot.operation", copy.deepcopy(record)))

    async def _run(self, record: dict[str, Any], arguments: dict[str, Any]) -> None:
        record["status"] = "running"
        self._publish(record)
        def feedback(value: dict[str, Any]) -> None:
            record["feedback"] = value
            self._publish(record)
        try:
            await self._save(record)
            if arguments["kind"] == "recover":
                result = await self.service.backend.recover(record["robot_id"])
                for prior in self._records.values():
                    if (prior.get("error") or {}).get("code") == "robot_recovery_required":
                        prior["recovery_resolved"] = True
                        await self._save(prior)
            else:
                result = await self.service.backend.execute_operation(record["robot_id"], arguments, feedback)
            record.update(status="succeeded", result=result)
        except asyncio.CancelledError:
            uncertain = getattr(self.service.backend, "_uncertain", False)
            record.update(status="failed" if uncertain else "cancelled",
                          error={"code": "robot_recovery_required" if uncertain else "run_cancelled",
                                 "message": "Robot termination is uncertain." if uncertain else "Robot operation cancelled."})
        except RobotBackendError as exc:
            uncertain = getattr(self.service.backend, "_uncertain", False)
            record.update(status="failed", error={"code": "robot_recovery_required" if uncertain else exc.code,
                          "message": exc.message + (" Robot termination requires recovery." if uncertain else "")})
        except Exception:
            record.update(status="failed", error={"code": "internal_error", "message": "Robot operation failed."})
        finally:
            record["finished_at"] = rfc3339(time.time())
            await self._save(record)
            self._tasks.pop(record["operation_id"], None)
            self._publish(record)

    def get(self, robot_id: str, identifier: str) -> dict[str, Any]:
        record = self._records.get(identifier)
        if record is None or record["robot_id"] != robot_id:
            raise ProductError("robot_operation_not_found", "Robot operation was not found.")
        return copy.deepcopy(record)

    async def cancel(self, robot_id: str, identifier: str) -> dict[str, Any]:
        self.get(robot_id, identifier)
        task = self._tasks.get(identifier)
        if task is not None:
            task.cancel()
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                if not task.cancelled():
                    raise
                record = self._records[identifier]
                record.update(status="cancelled", finished_at=rfc3339(time.time()),
                              error={"code": "run_cancelled", "message": "Operation cancelled before dispatch."})
                await self._save(record)
                self._tasks.pop(identifier, None)
        return self.get(robot_id, identifier)

    async def wait(self, robot_id: str, identifier: str, cancellation: Any = None) -> dict[str, Any]:
        task = self._tasks.get(identifier)
        signal = asyncio.create_task(cancellation.wait_cancelled()) if cancellation is not None else None
        try:
            if task is not None:
                if signal is None:
                    await asyncio.shield(task)
                else:
                    done, _ = await asyncio.wait((task, signal), return_when=asyncio.FIRST_COMPLETED)
                    if signal in done and cancellation.cancelled:
                        await self.cancel(robot_id, identifier)
                        raise asyncio.CancelledError()
            record = self.get(robot_id, identifier)
            if record["error"]:
                error = record["error"]
                raise ProductError(error["code"], error["message"])
            return record
        except asyncio.CancelledError:
            await self.cancel(robot_id, identifier)
            raise
        finally:
            if signal is not None:
                signal.cancel()
                await asyncio.gather(signal, return_exceptions=True)

    async def close(self) -> None:
        self._closing = True
        tasks = tuple(self._tasks.values())
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        for record in self._records.values():
            if record["status"] not in TERMINAL:
                record.update(status="cancelled", finished_at=rfc3339(time.time()),
                              error={"code": "run_cancelled", "message": "Server shut down before operation dispatch."})
                await self._save(record)
