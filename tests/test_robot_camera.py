"""Camera acquisition lifetimes and recovery through transient source gaps."""
import asyncio
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from roserver.config import Settings
from roserver.robot.backend import RobotNotReadyError
from roserver.robot.dclpy_backend import DclpyRobotBackend
from roserver.robot.images import image_pil


def test_image_byte_accessor_avoids_python_integer_list():
    pytest.importorskip("PIL")

    class Image:
        width = height = 1
        step = 3
        encoding = "rgb8"
        data_bytes = b"\xff\x00\x00"

        @property
        def data(self):
            raise AssertionError("The slow integer list must not be materialized")

    assert image_pil(Image()).getpixel((0, 0)) == (255, 0, 0)


def test_camera_users_share_subscription_and_cancel_without_disrupting_other_viewers():
    pytest.importorskip("dclpy.qos")

    async def run():
        backend = DclpyRobotBackend(Settings())
        backend.context = object()
        backend._types["Image"] = object()
        subscription = Mock()
        callbacks = []

        def subscribe(_type, _topic, callback, _qos):
            callbacks.append(callback)
            return subscription

        backend.node = SimpleNamespace(create_subscription=subscribe)
        first = asyncio.create_task(backend.open_camera("robot_head"))
        await asyncio.sleep(0)
        message = object()
        callbacks[0](message)
        await first
        await backend.open_camera("robot_head")
        assert len(callbacks) == 1 and backend._camera_users == {"head": 2}
        backend._images.clear()
        third = asyncio.create_task(backend.open_camera("robot_head"))
        await asyncio.sleep(0)
        third.cancel()
        with pytest.raises(asyncio.CancelledError):
            await third
        assert backend._camera_users == {"head": 2}
        backend.close_camera("robot_head")
        subscription.close.assert_not_called()
        backend.close_camera("robot_head")
        subscription.close.assert_called_once()
        assert not backend._camera_users and not backend._camera_subscriptions

    asyncio.run(run())


def test_track_recovers_after_source_timeout_without_ending_video():
    pytest.importorskip("aiortc")
    pytest.importorskip("PIL")
    from aiortc.mediastreams import MediaStreamError
    from roserver.media.dclpy_engine import create_camera_track

    async def run():
        message = SimpleNamespace(width=1, height=1, step=3, encoding="rgb8", data=b"\xff\x00\x00")
        backend = SimpleNamespace(camera_frame=Mock(return_value=message))
        track = create_camera_track(backend, "robot_head")
        first = await track.recv()
        pending = asyncio.create_task(track.recv())
        await asyncio.sleep(.05)
        assert not pending.done(), "An old image must not be repeated to inflate FPS"
        backend.camera_frame.side_effect = RobotNotReadyError("Temporarily stale")
        await asyncio.sleep(.08)
        assert not pending.done() and track.readyState == "live"
        backend.camera_frame.side_effect = None
        backend.camera_frame.return_value = SimpleNamespace(
            width=1, height=1, step=3, encoding="rgb8", data=b"\x00\xff\x00")
        second = await asyncio.wait_for(pending, 1)
        assert second.pts > first.pts
        assert second.to_image().getpixel((0, 0)) == (0, 255, 0)
        track.stop()
        with pytest.raises(MediaStreamError):
            await track.recv()

    asyncio.run(run())


def test_pending_camera_opens_count_toward_capacity_and_release_on_cancellation():
    pytest.importorskip("aiortc")
    from roserver.media.dclpy_engine import DclpyMediaEngine
    from roserver.media.engine import MediaEngineError

    async def run():
        gate = asyncio.Event()
        users = 0

        async def open_camera(_source):
            nonlocal users
            users += 1
            try:
                await gate.wait()
            except BaseException:
                users -= 1
                raise

        engine = DclpyMediaEngine(SimpleNamespace(open_camera=open_camera))
        tasks = [asyncio.create_task(engine.create_session(str(index), kind="camera", audio=False,
                 video=True, video_source="robot_head")) for index in range(8)]
        await asyncio.sleep(0)
        assert users == 8
        with pytest.raises(MediaEngineError) as error:
            await engine.create_session("overflow", kind="camera", audio=False, video=True, video_source="robot_head")
        assert error.value.code == "rate_limited"
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        assert users == 0 and engine._opening == 0 and not engine._sessions

    asyncio.run(run())
