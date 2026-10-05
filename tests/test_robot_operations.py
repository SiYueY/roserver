"""Physical-command admission, durability and cancellation regressions."""
import asyncio
import json
from types import SimpleNamespace

import pytest

from roserver.config import Settings
from roserver.errors import ProductError
from roserver.robot.backend import SimulatedRobotBackend
from roserver.robot.dclpy_backend import DclpyRobotBackend
from roserver.robot.images import image_pil
from roserver.robot.operations import validate_operation
from roserver.robot.service import RobotService
from roserver.robot.tools import robot_tools
from roserver.store.application import ApplicationStore


class OperationBackend(SimulatedRobotBackend):
    is_simulated = False

    def __init__(self):
        super().__init__()
        self.entered = asyncio.Event()
        self.finish = asyncio.Event()
        self.calls = 0
        self.reconciled = []
        self._uncertain = False

    async def execute_operation(self, robot_id, arguments, feedback):
        self.calls += 1
        self.entered.set()
        feedback({"phase": "executing"})
        await self.finish.wait()
        return {"success": True, "termination_confirmed": True}

    async def reconcile_operation(self, identifier, arguments):
        self.reconciled.append(identifier)


def test_operations_idempotency_conflict_and_concurrent_admission(tmp_path):
    async def run():
        settings = Settings(data_dir=tmp_path)
        store = ApplicationStore(settings.resolved_db_path)
        await store.open()
        backend = OperationBackend()
        service = RobotService(settings=settings, store=store, backend=backend)
        await service.startup()
        operations = service.operations
        payload = {"kind": "navigate", "pose": {"x": 1, "y": 2}}
        first, same = await asyncio.gather(operations.start("robot_1", payload, "once"),
                                         operations.start("robot_1", payload, "once"))
        assert first["operation_id"] == same["operation_id"]
        await backend.entered.wait()
        with pytest.raises(ProductError) as conflict:
            await operations.start("robot_1", dict(payload, timeout_s=10), "once")
        assert conflict.value.code == "idempotency_conflict"
        with pytest.raises(ProductError) as busy:
            await operations.start("robot_1", payload)
        assert busy.value.code == "control_authority_conflict"
        backend.finish.set()
        result = await operations.wait("robot_1", first["operation_id"])
        assert result["status"] == "succeeded" and isinstance(result["finished_at"], str)
        assert backend.calls == 1
        await service.shutdown()
        # A fresh service must reuse the persisted result without dispatch.
        restarted = RobotService(settings=settings, store=store, backend=OperationBackend())
        await restarted.startup()
        reused = await restarted.operations.start("robot_1", payload, "once")
        assert reused["status"] == "succeeded" and reused["operation_id"] == first["operation_id"]
        assert restarted.backend.calls == 0
        await restarted.shutdown()
        await store.close()
    asyncio.run(run())


def test_crash_reconciliation_never_replays_physical_command(tmp_path):
    async def run():
        settings = Settings(data_dir=tmp_path)
        store = ApplicationStore(settings.resolved_db_path)
        await store.open()
        identifier = "op_" + "a" * 32
        record = {"operation_id": identifier, "robot_id": "robot_1", "kind": "navigate", "status": "running"}
        store._execute("INSERT INTO robot_operations VALUES (?,?,?,?,?,?,?,?)",
                       (identifier, "local", "robot_1", "crashed", "digest", json.dumps({"kind": "navigate"}),
                        json.dumps(record), 0))
        backend = OperationBackend()
        service = RobotService(settings=settings, store=store, backend=backend)
        await service.startup()
        assert backend.reconciled == [identifier] and backend.calls == 0
        result = service.operations.get("robot_1", identifier)
        assert result["status"] == "failed" and result["error"]["code"] == "server_restarted"
        await service.shutdown()
        await store.close()
    asyncio.run(run())


def test_cancellation_before_worker_dispatch_never_calls_backend(tmp_path):
    async def run():
        settings = Settings(data_dir=tmp_path)
        store = ApplicationStore(settings.resolved_db_path)
        await store.open()
        backend = OperationBackend()
        service = RobotService(settings=settings, store=store, backend=backend)
        await service.startup()
        first = await service.operations.start("robot_1", {"kind": "navigate", "pose": {"x": 1, "y": 2}})
        result = await service.operations.cancel("robot_1", first["operation_id"])
        assert result["status"] == "cancelled" and backend.calls == 0
        assert not service.operations._tasks
        await service.shutdown()
        await store.close()
    asyncio.run(run())


@pytest.mark.parametrize("uncertain", [False, True])
def test_cancel_waits_for_dispatch_cleanup_and_exposes_uncertain_termination(tmp_path, uncertain):
    async def run():
        settings = Settings(data_dir=tmp_path)
        store = ApplicationStore(settings.resolved_db_path)
        await store.open()
        backend = OperationBackend()
        backend._uncertain = uncertain
        service = RobotService(settings=settings, store=store, backend=backend)
        await service.startup()
        first = await service.operations.start("robot_1", {"kind": "navigate", "pose": {"x": 1, "y": 2}})
        await backend.entered.wait()
        result = await service.operations.cancel("robot_1", first["operation_id"])
        assert result["status"] == ("failed" if uncertain else "cancelled")
        assert not service.operations._tasks
        await service.shutdown()
        await store.close()
    asyncio.run(run())


@pytest.mark.parametrize("value", [float("nan"), float("inf"), True, None])
def test_operation_rejects_invalid_pose_before_dispatch(value):
    with pytest.raises(ProductError):
        validate_operation({"kind": "navigate", "pose": {"x": value, "y": 1}}, 180)


@pytest.mark.parametrize("value", [{"kind": {}}, {"kind": "recover", "manipulator": []},
                                  {"kind": "sequence", "steps": [{"kind": []}]}])
def test_invalid_operation_discriminators_return_product_error(value):
    with pytest.raises(ProductError) as error:
        validate_operation(value, 180)
    assert error.value.code == "invalid_input"


def test_image_decode_preserves_bgr_channels_and_padded_rows():
    pytest.importorskip("PIL")
    message = SimpleNamespace(width=1, height=2, step=4, encoding="bgr8",
                              data=bytes([0, 0, 255, 99, 0, 255, 0, 99]))
    image = image_pil(message)
    assert image.getpixel((0, 0)) == (255, 0, 0)
    assert image.getpixel((0, 1)) == (0, 255, 0)
    message.data = b"\0"
    with pytest.raises(ValueError, match="Truncated"):
        image_pil(message)


@pytest.mark.parametrize("status,confirmed,uncertain", [(5, True, False), (0, True, True), (6, False, True)])
def test_dcl_cancel_requires_actual_terminal_state(status, confirmed, uncertain):
    class Future:
        def __init__(self, value): self.value = value
        async def _wait_async(self): return self.value

    async def run():
        backend = DclpyRobotBackend(Settings())
        backend._unconfirmed_gripper = ("op_" + "a" * 32, {"kind": "gripper_move"})
        result = SimpleNamespace(status=status, result=SimpleNamespace(termination_confirmed=confirmed))
        backend._active_handle = SimpleNamespace(accepted=True, get_result_async=lambda: Future(result),
                                               cancel_goal_async=lambda: Future(None))
        await backend._cancel_action()
        assert backend._uncertain == uncertain
        assert (backend._unconfirmed_gripper is None) == (status in {4, 5, 6})
    asyncio.run(run())


def test_successful_recovery_is_persisted_and_not_reopened_after_restart(tmp_path):
    async def run():
        settings = Settings(data_dir=tmp_path)
        store = ApplicationStore(settings.resolved_db_path)
        await store.open()
        backend = OperationBackend()
        service = RobotService(settings=settings, store=store, backend=backend)
        await service.startup()
        identifier = "op_" + "b" * 32
        prior = {"operation_id": identifier, "robot_id": "robot_1", "kind": "gripper_move", "status": "failed",
                 "error": {"code": "robot_recovery_required"}}
        service.operations._records[identifier] = prior
        store._execute("INSERT INTO robot_operations VALUES (?,?,?,?,?,?,?,?)",
                       (identifier, "local", "robot_1", None, "digest", json.dumps({"kind": "gripper_move"}),
                        json.dumps(prior), 0))
        async def recover(robot_id): return {"ready": True}
        backend.recover = recover
        recovery = await service.operations.start("robot_1", {"kind": "recover"})
        await service.operations.wait("robot_1", recovery["operation_id"])
        assert prior["recovery_resolved"]
        await service.shutdown()
        restarted = RobotService(settings=settings, store=store, backend=OperationBackend())
        await restarted.startup()
        assert not restarted.backend.reconciled
        await restarted.shutdown()
        await store.close()
    asyncio.run(run())


def test_interrupted_remote_cancel_preserves_the_recovery_gate():
    class Future:
        async def _wait_async(self): raise asyncio.CancelledError()

    async def run():
        backend = DclpyRobotBackend(Settings())
        backend._active_handle = SimpleNamespace(accepted=True, get_result_async=Future,
                                               cancel_goal_async=Future)
        with pytest.raises(asyncio.CancelledError):
            await backend._cancel_action()
        assert backend._uncertain
    asyncio.run(run())


def test_real_robot_registry_never_exposes_simulation_navigation(tmp_path):
    settings = Settings(data_dir=tmp_path)
    backend = OperationBackend()
    service = RobotService(settings=settings, store=ApplicationStore(settings.resolved_db_path), backend=backend)
    names = {tool.definition.name for tool in robot_tools(service, settings)}
    assert names == {"get_robot_state", "get_robot_observations", "execute_robot_task", "get_camera_image", "stop_robot"}


def test_camera_tool_materializes_an_actual_jpeg_artifact(client_factory, settings_factory, api):
    pytest.importorskip("PIL")
    from conftest import Turn
    from test_artifacts import FileCapableModel
    from roboagent.message import FrozenJsonObject, ToolCall
    backend = OperationBackend()
    backend.camera_frame = lambda source: SimpleNamespace(width=2, height=2, step=6, encoding="rgb8", data=bytes(12))
    model = FileCapableModel([Turn(tool_calls=(ToolCall("image", "get_camera_image", FrozenJsonObject({})),)),
                             Turn(text="Frame received.")])
    with client_factory(settings_factory(), model=model, robot_backend=backend) as client:
        session = api.create_session(client)
        response = api.start_run(client, session["session_id"])
        run = api.wait_terminal(client, response.json()["run_id"])
        assert run["status"] == "completed", run
        snapshot = client.get(f"/api/v1/sessions/{session['session_id']}").json()
        images = [block for message in snapshot["messages"] if message["role"] == "tool"
                  for block in message["content"] if block["type"] == "image"]
        assert len(images) == 1
        content = client.get(f"/api/v1/artifacts/{images[0]['artifact_id']}/content")
        assert content.status_code == 200 and content.content[:2] == b"\xff\xd8"
