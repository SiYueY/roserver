"""Opt-in full-stack acceptance against an isolated headless MuJoCo robot.

Run with scripts/run-dclpy.sh environment (see README), never collected by
pytest. Starts its own ROS domain; no commands reach an existing robot.
"""
from __future__ import annotations

import asyncio
import argparse
from contextlib import asynccontextmanager
import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import tempfile
import time

import httpx
import uvicorn
from websockets.asyncio.client import connect
from aiortc import RTCConfiguration, RTCPeerConnection, RTCSessionDescription
from roboagent.message import FrozenJsonObject, ToolCall
from roboagent.model import ModelCapabilities
from roboagent.runtime import Modality

from conftest import FakeModel, Turn
from roserver.app import create_app
from roserver.config import Settings
from roserver.errors import ProductError

ROOT = Path(__file__).resolve().parents[2]
SEQUENCE = {"kind": "sequence", "timeout_s": 180, "steps": [
    {"kind": "navigate", "pose": {"x": -.35, "y": .7, "theta": 0}},
    {"kind": "pick", "object_id": "box"},
    {"kind": "place", "object_id": "box", "pose": {"x": .72, "y": .75, "z": .985}},
]}


class CameraModel(FakeModel):
    @property
    def capabilities(self):
        return ModelCapabilities(input_modalities=frozenset({Modality.TEXT, Modality.IMAGE, Modality.FILE}),
                                 tool_calling=True)


@asynccontextmanager
async def product_client(app):
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    port = listener.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, log_level="error", lifespan="off"))
    task = asyncio.create_task(server.serve(sockets=[listener]))
    try:
        deadline = time.monotonic() + 5
        while not server.started:
            assert not task.done() and time.monotonic() < deadline
            await asyncio.sleep(.01)
        async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}", timeout=200, trust_env=False) as client:
            yield client
    finally:
        server.should_exit = True
        await asyncio.wait_for(task, 10)
        listener.close()


async def check_video(client: httpx.AsyncClient, *, camera: str = "head", duration_s: float = 0, backend=None) -> None:
    # SDK readiness and the first camera frame have independent lifecycles.
    deadline = time.monotonic() + 10
    while True:
        image = await client.get(f"/api/v1/robots/robot_1/cameras/{camera}/image")
        if image.status_code != 503 or time.monotonic() >= deadline:
            break
        await asyncio.sleep(.1)
    assert image.status_code == 200 and image.content[:2] == b"\xff\xd8", image.text[:200]
    response = await client.post("/api/v1/media/sessions", json={
        "kind": "camera", "video": True, "video_source": f"robot_{camera}"})
    assert response.status_code == 201, response.text
    identifier = response.json()["media_session_id"]
    peer = RTCPeerConnection(RTCConfiguration(iceServers=[]))
    received = asyncio.get_running_loop().create_future()
    decoded: list[float] = []
    captured: list[tuple[float, tuple[int, int]]] = []
    physics_clock: list[tuple[float, float]] = []
    clock_subscription = None
    reader = None
    original_callback = backend._on_image if backend is not None else None
    if backend is not None:
        if duration_s:
            from sensor_msgs_dclpy.msg import TimeReference
            from dclpy.qos import qos_profile_sensor_data
            clock_subscription = backend.node.create_subscription(
                TimeReference, backend._name("sensors/simulation/time_reference"),
                lambda message: physics_clock.append((time.monotonic(),
                    message.time_ref.sec + message.time_ref.nanosec / 1e9)), qos_profile_sensor_data)
        def sample(name, message):
            original_callback(name, message)
            if name == camera:
                captured.append((time.monotonic(), (message.header.stamp.sec, message.header.stamp.nanosec)))
        backend._on_image = sample

    @peer.on("track")
    def on_track(track):
        async def read():
            try:
                while True:
                    frame = await track.recv()
                    decoded.append(time.monotonic())
                    if not received.done():
                        received.set_result((frame.width, frame.height))
            except Exception as exc:
                if not received.done():
                    received.set_exception(exc)
        nonlocal reader
        reader = asyncio.create_task(read())

    try:
        peer.addTransceiver("video", direction="recvonly")
        await peer.setLocalDescription(await peer.createOffer())
        answer = await client.post(f"/api/v1/media/sessions/{identifier}/offer", json={
            "type": "offer", "sdp": peer.localDescription.sdp})
        assert answer.status_code == 200, answer.text
        await peer.setRemoteDescription(RTCSessionDescription(type="answer", sdp=answer.json()["sdp"]))
        dimensions = await asyncio.wait_for(received, 15)
        assert min(dimensions) > 0
        print("PASS actual JPEG and WebRTC decoded camera frame", camera, dimensions, flush=True)
        if duration_s:
            await asyncio.sleep(3)
            start = time.monotonic()
            await asyncio.sleep(duration_s)
            end = time.monotonic()
            received_times = [value for value in decoded if start <= value <= end]
            source_samples = {stamp for when, stamp in captured if start <= when <= end}
            physics_samples = [(when, stamp) for when, stamp in physics_clock if start <= when <= end]
            stats = {"camera": camera, "decoded_fps": len(received_times) / (end - start),
                     "fresh_source_fps": len(source_samples) / (end - start),
                     "max_frame_gap_s": max((b-a for a,b in zip(received_times, received_times[1:], strict=False)), default=0)}
            print("CAMERA_FPS", json.dumps(stats), flush=True)
            for report in (await peer.getStats()).values():
                if report.type == 'inbound-rtp' and report.kind == 'video':
                    print('VIDEO_RTP', camera, 'lost', report.packetsLost,
                          'received', report.packetsReceived, 'jitter', report.jitter, flush=True)
            if len(physics_samples) > 1:
                print("PHYSICS_RTF", camera, (physics_samples[-1][1] - physics_samples[0][1]) /
                      (physics_samples[-1][0] - physics_samples[0][0]), flush=True)
            assert dimensions == (480, 270), dimensions
            assert stats["decoded_fps"] >= 28.5 and stats["fresh_source_fps"] >= 30, stats
            assert stats["max_frame_gap_s"] < .25, stats
    finally:
        await peer.close()
        await client.delete(f"/api/v1/media/sessions/{identifier}")
        if reader is not None:
            reader.cancel()
            await asyncio.gather(reader, return_exceptions=True)
        if backend is not None:
            if clock_subscription is not None:
                clock_subscription.close()
            backend._on_image = original_callback
            assert not backend._camera_users, backend._camera_users


async def check_kitchen_tasks(client, app, *, interactive: bool) -> None:
    from sensor_msgs_dclpy.msg import TimeReference
    traces = []
    last_trace = 0.
    physics_time = 0.
    def physics(message):
        nonlocal physics_time
        physics_time = message.time_ref.sec + message.time_ref.nanosec * 1e-9
    def trace():
        nonlocal last_trace
        now = time.monotonic()
        if now - last_trace < 1:
            return
        last_trace = now
        sample = app.state.robot_backend._joints.get('franka_spine_vertical_joint')
        traces.append({'wall': now, 'physics': physics_time, 'spine': sample[0] if sample else None})
    backend = app.state.robot_backend
    backend.node.create_subscription(TimeReference, backend._name('sensors/simulation/time_reference'), physics, 10)
    session = (await client.post('/api/v1/sessions', json={'title': 'Kitchen acceptance'})).json()
    response = await client.post(f"/api/v1/sessions/{session['session_id']}/runs", json={
        'input': {'content': [{'type': 'text', 'text': 'Navigate, pick and place the kitchen box.'}]}})
    assert response.status_code in (201, 202), response.text
    identifier = response.json()['run_id']
    deadline = time.monotonic() + 250
    while True:
        run = (await client.get(f'/api/v1/runs/{identifier}')).json()
        trace()
        if run['status'] in {'completed', 'failed', 'cancelled', 'interrupted'}:
            break
        assert time.monotonic() < deadline, run
        await asyncio.sleep(.25)
    if run['status'] != 'completed':
        print((await client.get(f'/api/v1/runs/{identifier}/projection')).text, flush=True)
        print('SPINE_TRACE', json.dumps(traces[-50:]), flush=True)
    assert run['status'] == 'completed', run
    record = next(r for r in app.state.robot.operations._records.values() if r['kind'] == 'sequence')
    if record['status'] != 'succeeded':
        print('SPINE_TRACE', json.dumps(traces[-50:]), flush=True)
        print('TASK_FAILED', record, flush=True)
        print('MEASURED_STATE', (await client.get('/api/v1/robots/robot_1/state')).text, flush=True)
        print('MEASURED_OBSERVATIONS', (await client.get('/api/v1/robots/robot_1/observations')).text, flush=True)
    assert record['status'] == 'succeeded', record
    observations = (await client.get('/api/v1/robots/robot_1/observations')).json()['observations']
    box = next(o for o in observations if o['kind'] == 'object' and o['id'] == 'box')
    target = SEQUENCE['steps'][-1]['pose']
    assert all(abs(box['pose'][axis] - target[axis]) < .025 for axis in ('x', 'y', 'z')), box
    print('PASS kitchen Agent -> dclpy -> physical navigation, grasp, lift and placement', flush=True)
    if not interactive:
        return
    async def operation(name, position, timeout=20):
        response = await client.post('/api/v1/robots/robot_1/operations', json={
            'kind': 'scene_joint', 'object_id': name, 'position': position, 'timeout_s': timeout})
        assert response.status_code == 202, response.text
        return await app.state.robot.operations.wait('robot_1', response.json()['operation_id'])
    for name, opened in [('bottom_main_group_1_slidejoint', -.2), ('top_main_group_leftdoorhinge', -.6)]:
        for position in (opened, 0.):
            record = await operation(name, position)
            assert record['status'] == 'succeeded', record
            values = (await client.get('/api/v1/robots/robot_1/observations')).json()['observations']
            measured = next(v for v in values if v['kind'] == 'scene_joint' and v['id'] == name)
            assert abs(measured['position'] - position) < .015 and abs(measured['velocity']) < .02, measured
            print('PASS measured fixture actuator', name, measured['position'], flush=True)
    invalid_response = await client.post('/api/v1/robots/robot_1/operations', json={
        'kind': 'scene_joint', 'object_id': 'bottom_main_group_1_slidejoint', 'position': .1,
        'timeout_s': 20})
    assert invalid_response.status_code == 202, invalid_response.text
    invalid_id = invalid_response.json()['operation_id']
    try:
        await app.state.robot.operations.wait('robot_1', invalid_id)
        raise AssertionError('out-of-range fixture target unexpectedly succeeded')
    except ProductError as error:
        assert error.code == 'invalid_input', error
    response = await client.post('/api/v1/robots/robot_1/operations', json={
        'kind': 'scene_joint', 'object_id': 'bottom_main_group_1_slidejoint', 'position': -.5, 'timeout_s': 20})
    assert response.status_code == 202, response.text
    await asyncio.sleep(.7)
    cancelled = await client.post(f"/api/v1/robots/robot_1/operations/{response.json()['operation_id']}/cancel")
    assert cancelled.json()['status'] == 'cancelled', cancelled.text
    assert not app.state.robot_backend._uncertain
    held_positions = []
    for _ in range(8):
        await asyncio.sleep(.15)
        values = (await client.get('/api/v1/robots/robot_1/observations')).json()['observations']
        measured = next(v for v in values if v['kind'] == 'scene_joint' and
                        v['id'] == 'bottom_main_group_1_slidejoint')
        held_positions.append(measured['position'])
        assert abs(measured['velocity']) < .02, measured
    assert max(held_positions) - min(held_positions) < .015, held_positions
    assert (await operation('bottom_main_group_1_slidejoint', 0.))['status'] == 'succeeded'
    print('PASS fixture limits, confirmed cancellation and subsequent close', flush=True)


async def acceptance(domain: int, data_dir: Path, *, teleoperation_only: bool = False,
                     camera_fps: bool = False, browser_video: bool = False, scene: str = 'tasks') -> None:
    model = CameraModel([Turn(tool_calls=(ToolCall("physical_task", "execute_robot_task", FrozenJsonObject(SEQUENCE)),)),
                       Turn(tool_calls=(ToolCall("camera_artifact", "get_camera_image", FrozenJsonObject({})),)),
                       Turn(text="Physical task finished.")])
    app = create_app(Settings(data_dir=data_dir, robot_backend="dclpy", robot_domain_id=domain,
                             cors_origins=("http://127.0.0.1:5189",)), model=model)
    async with app.router.lifespan_context(app), product_client(app) as client:
        camera_only = camera_fps or browser_video
        deadline = time.monotonic() + 100
        while True:
            response = await client.get("/api/v1/robots/robot_1")
            info = response.json()
            if (response.status_code == 200 and info["connection"] == "online" and
                (camera_only or "task_sequence" in info["capabilities"])):
                break
            assert time.monotonic() < deadline, info
            await asyncio.sleep(.2)
        state = (await client.get("/api/v1/robots/robot_1/state")).json()
        assert len(state["joints"]) >= 17 and state["battery"] is None, state
        print("PASS real discovery, odometry and merged joint state", flush=True)
        if camera_only:
            if browser_video:
                process = await asyncio.create_subprocess_exec(
                    "node", "visual/camera.runtime.mjs", str(client.base_url).rstrip("/"), cwd=ROOT / "rodesk/web")
                try:
                    assert await asyncio.wait_for(process.wait(), 180) == 0
                finally:
                    if process.returncode is None:
                        process.terminate()
                        await process.wait()
                deadline = time.monotonic() + 5
                while app.state.robot_backend._camera_users:
                    assert time.monotonic() < deadline, app.state.robot_backend._camera_users
                    await asyncio.sleep(.05)
            if camera_fps:
                for camera in ["head", "front", "rear", "left", "right", "wrist", "right_wrist"]:
                    await check_video(client, camera=camera, duration_s=6, backend=app.state.robot_backend)
            return
        observations = await client.get("/api/v1/robots/robot_1/observations")
        assert any(value["id"] == "box" for value in observations.json()["observations"]), observations.text
        print("PASS DDS graph discovery of actual object and tool observations", flush=True)
        if scene != 'tasks' and not teleoperation_only:
            await check_kitchen_tasks(client, app, interactive=scene == 'kitchen_interactive')
            return

        authority = (await client.post("/api/v1/robots/robot_1/control/acquire")).json()
        authority_id = authority["authority_id"]
        start_x = state["pose"]["x"]
        service = app.state.robot
        from datetime import datetime, timezone
        ws_url = str(client.base_url).rstrip("/").replace("http:", "ws:") + "/api/v1/robots/robot_1/teleoperation"
        async with connect(ws_url, proxy=None) as websocket:
            for sequence in range(1, 16):
                await websocket.send(json.dumps({
                    "type": "velocity", "authority_id": authority_id, "sequence": sequence,
                    "client_timestamp": datetime.now(timezone.utc).isoformat(),
                    "linear_x": .05, "linear_y": 0, "angular_z": 0, "deadman": True}))
                feedback = json.loads(await asyncio.wait_for(websocket.recv(), 2))
                assert feedback.get("accepted"), feedback
                await asyncio.sleep(.1)
            moved = (await client.get("/api/v1/robots/robot_1/state")).json()
            assert moved["pose"]["x"] - start_x > .01, (state, moved)

            async def command(sequence: int, *, deadman: bool = True) -> None:
                await websocket.send(json.dumps({
                    "type": "velocity", "authority_id": authority_id, "sequence": sequence,
                    "client_timestamp": datetime.now(timezone.utc).isoformat(),
                    "linear_x": .05 if deadman else 0, "linear_y": 0, "angular_z": 0, "deadman": deadman}))
                feedback = json.loads(await asyncio.wait_for(websocket.recv(), 2))
                assert feedback["type"] == "teleop.feedback" and feedback["accepted"], feedback

            await command(16, deadman=False)
            stopped_event = json.loads(await asyncio.wait_for(websocket.recv(), 2))
            assert stopped_event["type"] == "control_lost" and stopped_event["data"]["reason"] == "deadman", stopped_event
            await asyncio.sleep(.2)
            stopped = (await client.get("/api/v1/robots/robot_1/state")).json()
            assert abs(stopped["velocity"]["linear_x"]) < .01, stopped
            for sequence in range(17, 25):
                await command(sequence)
                await asyncio.sleep(.1)
            resumed = (await client.get("/api/v1/robots/robot_1/state")).json()
            assert resumed["pose"]["x"] - stopped["pose"]["x"] > .005, (stopped, resumed)
            renewed = await client.post("/api/v1/robots/robot_1/control/renew", json={"authority_id": authority_id})
            assert renewed.status_code == 200, renewed.text
            stopped_event = json.loads(await asyncio.wait_for(websocket.recv(), 2))
            assert stopped_event["type"] == "control_lost" and stopped_event["data"]["reason"] == "watchdog", stopped_event
            await asyncio.sleep(.2)
            stopped = (await client.get("/api/v1/robots/robot_1/state")).json()
            assert abs(stopped["velocity"]["linear_x"]) < .01, stopped
            for sequence in range(25, 33):
                await command(sequence)
                await asyncio.sleep(.1)
            resumed = (await client.get("/api/v1/robots/robot_1/state")).json()
            assert resumed["pose"]["x"] - stopped["pose"]["x"] > .005, (stopped, resumed)
            await command(33, deadman=False)
            stopped_event = json.loads(await asyncio.wait_for(websocket.recv(), 2))
            assert stopped_event["data"]["reason"] == "deadman", stopped_event
        release = await client.post("/api/v1/robots/robot_1/control/release", json={"authority_id": authority_id})
        assert release.status_code == 200, release.text
        print("PASS actual HTTP/WebSocket motion, deadman stop/resume, watchdog stop/resume, lease renewal/release", flush=True)

        if teleoperation_only:
            await check_video(client, backend=app.state.robot_backend)
            return

        gripper = {"kind": "gripper_move", "width": .04, "speed": .04, "timeout_s": 10}
        first = await client.post("/api/v1/robots/robot_1/operations", json=gripper, headers={"Idempotency-Key": "gripper-once"})
        assert first.status_code == 202, first.text
        identifier = first.json()["operation_id"]
        record = await service.operations.wait("robot_1", identifier)
        assert record["status"] == "succeeded", record
        duplicate = await client.post("/api/v1/robots/robot_1/operations", json=gripper, headers={"Idempotency-Key": "gripper-once"})
        assert duplicate.json()["operation_id"] == identifier, duplicate.text
        print("PASS real gripper Action and idempotent Product operation", flush=True)
        await check_video(client)

        session = await client.post("/api/v1/sessions", json={"title": "Real robot acceptance"})
        identifier = session.json()["session_id"]
        response = await client.post(f"/api/v1/sessions/{identifier}/runs", json={
            "input": {"content": [{"type": "text", "text": "Navigate, pick and place the box."}]}})
        assert response.status_code in {201, 202}, response.text
        run_id = response.json()["run_id"]
        deadline = time.monotonic() + 210
        while True:
            run = (await client.get(f"/api/v1/runs/{run_id}")).json()
            if run["status"] in {"completed", "failed", "cancelled", "interrupted"}:
                break
            assert time.monotonic() < deadline, run
            await asyncio.sleep(.25)
        if run["status"] != "completed":
            print((await client.get(f"/api/v1/runs/{run_id}/projection")).text, flush=True)
        assert run["status"] == "completed", run
        artifacts = await app.state.service.store.list_artifacts("local")
        assert artifacts and artifacts[-1]["media_type"] == "image/jpeg", artifacts
        print("PASS Agent camera tool materialized a real JPEG Artifact", flush=True)
        records = list(service.operations._records.values())
        task_record = next(record for record in records if record["kind"] == "sequence")
        assert task_record["status"] == "succeeded", task_record
        print("PASS Agent Run -> dclpy -> ROS Task Action -> physical navigate/pick/place", json.dumps(task_record), flush=True)

        deadline = time.monotonic() + 5
        while not app.state.robot_backend._task_available():
            assert time.monotonic() < deadline
            await asyncio.sleep(.1)

        response = await client.post("/api/v1/robots/robot_1/operations", json={
            "kind": "navigate", "pose": {"x": -1.5, "y": .7}, "timeout_s": 60})
        assert response.status_code == 202, response.text
        identifier = response.json()["operation_id"]
        await asyncio.sleep(.5)
        cancelled = await client.post(f"/api/v1/robots/robot_1/operations/{identifier}/cancel")
        assert cancelled.json()["status"] == "cancelled", cancelled.text
        assert not app.state.robot_backend._uncertain
        print("PASS task cancellation confirmed by actual ROS terminal result", flush=True)
        response = await client.post("/api/v1/robots/robot_1/operations", json={"kind": "recover"})
        assert response.status_code == 202, response.text
        recovery = await service.operations.wait("robot_1", response.json()["operation_id"])
        assert recovery["status"] == "succeeded", recovery
        print("PASS explicit recovery waits for fresh Robot SDK readiness", flush=True)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--teleoperation-only", action="store_true", help="Check real control and camera without grasp tasks")
    parser.add_argument("--camera-fps", action="store_true", help="Measure fresh source/decoded FPS for every camera")
    parser.add_argument("--browser-video", action="store_true", help="Measure actual Chrome video in a mobile viewport")
    parser.add_argument("--viewer", action="store_true", help="Keep the MuJoCo viewer enabled during validation")
    parser.add_argument("--scene", choices=("tasks", "kitchen", "kitchen_interactive"), default="tasks")
    args = parser.parse_args()
    if args.scene != "tasks":
        import yaml
        scene_config = yaml.safe_load((ROOT / "mfr3duo_ros2/mfr3duo_scenes/scenes/kitchen/scene.yaml").read_text())
        aisle, counter = scene_config["waypoints"]["aisle"], scene_config["waypoints"]["counter"]
        resting = scene_config["place_pose"]
        SEQUENCE["steps"] = [
            {"kind": "navigate", "pose": {"x": aisle[0], "y": aisle[1], "theta": aisle[2]}},
            {"kind": "navigate", "pose": {"x": counter[0], "y": counter[1], "theta": counter[2]}},
            {"kind": "pick", "object_id": "box"},
            {"kind": "place", "object_id": "box", "pose": {"x": resting[0], "y": resting[1], "z": resting[2]}},
        ]
    domain = 120 + os.getpid() % 60
    with tempfile.TemporaryDirectory(prefix="roserver-ros2-") as temporary:
        directory = Path(temporary)
        env = os.environ.copy()
        env.pop("PYTHONPATH", None)  # Child ROS CLI uses its own CPython 3.10 providers.
        env.update(ROS_DOMAIN_ID=str(domain), ROS_LOG_DIR=str(directory / "ros-log"),
                   XDG_CACHE_HOME=str(directory / "cache"), MUJOCO_GL="egl")
        logfile = directory / "robot.log"
        with logfile.open("w") as stream:
            process = subprocess.Popen([
                "bash", "-c", 'source /opt/ros/humble/setup.bash\nsource "$1/mfr3duo_ros2/install/setup.bash"\nexec ros2 launch mfr3duo_robot robot.launch.py viewer_enabled:="$2" scene:="$3"',
                "ros2-acceptance", str(ROOT), "true" if args.viewer else "false", args.scene],
                env=env, stdout=stream, stderr=subprocess.STDOUT, start_new_session=True)
            try:
                asyncio.run(acceptance(domain, directory / "data", teleoperation_only=args.teleoperation_only,
                                       camera_fps=args.camera_fps, browser_video=args.browser_video, scene=args.scene))
            except BaseException:
                print(logfile.read_text()[-18000:], flush=True)
                raise
            finally:
                os.killpg(process.pid, signal.SIGINT)
                try:
                    process.wait(timeout=20)
                except subprocess.TimeoutExpired:
                    os.killpg(process.pid, signal.SIGKILL)
                    process.wait()
                # Preserve evidence after the isolated fixture has been removed.
                Path("/tmp/roserver-ros2-runtime-robot.log").write_text(logfile.read_text())


if __name__ == "__main__":
    main()
