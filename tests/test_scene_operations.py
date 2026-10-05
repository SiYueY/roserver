"""Simulation fixture inputs share task ownership, never masquerade as arm skills."""
import asyncio
import math
import time
from types import SimpleNamespace
import pytest

from roserver.errors import ProductError
from roserver.robot.operations import validate_operation
from roserver.robot.tools import robot_tools
from roserver.config import Settings
from roserver.robot.dclpy_backend import DclpyRobotBackend
from roboagent.message import FrozenJsonObject


@pytest.mark.parametrize('value', [True, None, 'open', math.nan, math.inf])
def test_scene_joint_requires_finite_target(value):
    with pytest.raises(ProductError):
        validate_operation({'kind': 'scene_joint', 'object_id': 'drawer', 'position': value}, 30)


def test_scene_joint_sequence_preserves_names_and_targets():
    result = validate_operation({'kind': 'sequence', 'steps': [
        {'kind': 'scene_joint', 'object_id': 'drawer', 'position': -.2},
        {'kind': 'scene_joint', 'object_id': 'drawer', 'position': 0.},
    ]}, 30)
    assert result['steps'][0]['position'] == -.2
    assert result['steps'][1]['object_id'] == 'drawer'
    assert result['steps'][0]['timeout_s'] == 30


def test_agent_deadline_allows_robot_timeout_and_terminal_confirmation(tmp_path):
    settings = Settings(data_dir=tmp_path)
    service = SimpleNamespace(settings=settings, backend=SimpleNamespace(is_simulated=False))
    tool = next(t for t in robot_tools(service, settings) if t.definition.name == 'execute_robot_task')
    assert tool.requested_timeout(FrozenJsonObject({'kind': 'pick'})) == 200.
    assert tool.requested_timeout(FrozenJsonObject({'kind': 'sequence', 'timeout_s': 120})) == 140.


def test_scene_operation_waits_for_terminal_status_tick(tmp_path):
    backend = DclpyRobotBackend(Settings(data_dir=tmp_path, robot_startup_timeout=.2))
    backend._status = SimpleNamespace(ready=False, state=2)
    backend._status_seen = time.monotonic()
    backend._task_client = SimpleNamespace(server_is_ready=lambda: True)
    backend._lease_client = SimpleNamespace(service_is_ready=lambda: True)

    async def publish_ready():
        await asyncio.sleep(.03)
        backend._on_status(SimpleNamespace(ready=True, state=1))

    async def verify():
        publisher = asyncio.create_task(publish_ready())
        assert await backend._wait_for_task_ready()
        await publisher

    asyncio.run(verify())


@pytest.mark.parametrize('position,velocity,names', [
    ([math.nan], [0.], ['drawer']), ([0.], [math.inf], ['drawer']),
    ([], [0.], ['drawer']), ([0., 0.], [0., 0.], ['drawer', 'drawer']),
])
def test_bad_fixture_telemetry_does_not_refresh_observations(tmp_path, position, velocity, names):
    backend = DclpyRobotBackend(Settings(data_dir=tmp_path))
    backend._on_scene_joints(SimpleNamespace(name=['drawer'], position=[-.2], velocity=[0.]))
    previous = backend._scene_joints
    backend._on_scene_joints(SimpleNamespace(name=names, position=position, velocity=velocity))
    assert backend._scene_joints is previous
