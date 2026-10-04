"""Phase 3A robot backend: discovery, authority and teleoperation.

The project is not at the robot integration stage, so every test injects
:class:`SimulatedRobotBackend` through ``create_app(robot_backend=...)``.  No
test touches the network.
"""

from __future__ import annotations

import time
from contextlib import contextmanager

from conftest import FakeModel, Turn

from roserver.agent.schema import rfc3339
from roserver.robot.backend import RobotUnavailableError, SimulatedRobotBackend

ROBOT_ID = "robot_1"

ROBOT_FIELDS = {
    "robot_id",
    "name",
    "model",
    "protocol_version",
    "connection",
    "mode",
    "capabilities",
    "cameras",
    "last_seen_at",
}
CAMERA_FIELDS = {"camera_id", "name", "video_source", "available"}
STATE_FIELDS = {
    "robot_id",
    "timestamp",
    "connection",
    "mode",
    "battery",
    "pose",
    "velocity",
    "joints",
    "faults",
}
AUTHORITY_FIELDS = {"authority_id", "robot_id", "mode", "expires_at"}


class ManualClock:
    """Injectable clock so pose integration is exact, not wall-clock timing."""

    def __init__(self, now: float = 1_700_000_000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


@contextmanager
def robot_app(client_factory, settings_factory, backend=None, **overrides):
    base = {
        "robot_authority_ttl": 5.0,
        "robot_watchdog_timeout": 5.0,
        "robot_watchdog_tick": 0.01,
        "robot_watch_interval": 0.05,
    }
    base.update(overrides)
    settings = settings_factory(**base)
    with client_factory(
        settings, model=FakeModel([Turn(text="ok")]), robot_backend=backend
    ) as client:
        yield client


def command(authority_id: str, sequence: int, **overrides):
    payload = {
        "type": "velocity",
        "authority_id": authority_id,
        "sequence": sequence,
        "client_timestamp": rfc3339(time.time()),
        "linear_x": 0.2,
        "linear_y": 0.0,
        "angular_z": 0.1,
        "deadman": True,
    }
    payload.update(overrides)
    return payload


def acquire(client, robot_id: str = ROBOT_ID, **body):
    response = client.post(f"/api/v1/robots/{robot_id}/control/acquire", json=body)
    assert response.status_code == 200, response.text
    return response.json()


# =====================================================================
# discovery / state (docs §4.5)
# =====================================================================
def test_robot_list_and_detail_shape(client_factory, settings_factory):
    with robot_app(client_factory, settings_factory) as client:
        listed = client.get("/api/v1/robots")
        assert listed.status_code == 200
        body = listed.json()
        assert body["next_cursor"] is None
        assert len(body["items"]) == 1
        robot = body["items"][0]
        assert set(robot) == ROBOT_FIELDS
        assert robot["robot_id"] == ROBOT_ID
        assert robot["connection"] == "online"
        assert robot["protocol_version"] == "1.0"

        detail = client.get(f"/api/v1/robots/{ROBOT_ID}").json()
        assert set(detail) == ROBOT_FIELDS
        assert detail["robot_id"] == ROBOT_ID


def test_robot_state_shape_and_null_not_zero(client_factory, settings_factory):
    backend = SimulatedRobotBackend()
    with robot_app(client_factory, settings_factory, backend=backend) as client:
        state = client.get(f"/api/v1/robots/{ROBOT_ID}/state").json()
        assert set(state) == STATE_FIELDS
        assert state["robot_id"] == ROBOT_ID
        assert state["timestamp"] is not None
        assert state["battery"] == {"level": 82.0, "charging": False}
        assert state["pose"]["frame_id"] == "map"
        # effort is genuinely unknown -> null, never 0
        assert state["joints"][0]["effort"] is None

        backend.set_battery(ROBOT_ID, None)
        unknown = client.get(f"/api/v1/robots/{ROBOT_ID}/state").json()
        assert unknown["battery"]["level"] is None
        assert unknown["battery"]["level"] != 0


def test_capabilities_do_not_advertise_navigation(client_factory, settings_factory):
    with robot_app(client_factory, settings_factory) as client:
        robot = client.get(f"/api/v1/robots/{ROBOT_ID}").json()
        assert robot["capabilities"] == ["state", "teleoperation"]
        assert "navigation" not in robot["capabilities"]
        assert "manipulation" not in robot["capabilities"]


# =====================================================================
# camera sources (docs §4.5 Robot object / §5.9 cameraId ↔ video_source)
# =====================================================================
def test_robot_cameras_shape_on_both_endpoints(client_factory, settings_factory):
    with robot_app(client_factory, settings_factory) as client:
        listed = client.get("/api/v1/robots").json()["items"][0]
        detail = client.get(f"/api/v1/robots/{ROBOT_ID}").json()
        for robot in (listed, detail):
            assert isinstance(robot["cameras"], list)
            assert len(robot["cameras"]) == 2
            for camera in robot["cameras"]:
                # Exact frozen contract from docs §4.5.
                assert set(camera) == CAMERA_FIELDS
                assert isinstance(camera["camera_id"], str) and camera["camera_id"]
                assert isinstance(camera["name"], str) and camera["name"]
                assert isinstance(camera["video_source"], str)
                assert isinstance(camera["available"], bool)

        # ``available: false`` means "exists but not currently streamable" and
        # must be preserved so the UI can disable it instead of hiding it.
        availability = {c["camera_id"]: c["available"] for c in detail["cameras"]}
        assert availability["head"] is True
        assert availability["wrist"] is False
        assert {c["video_source"] for c in detail["cameras"]} == {
            "robot_head",
            "robot_wrist",
        }


def test_robot_without_cameras_exposes_empty_list(
    client_factory, settings_factory
):
    backend = SimulatedRobotBackend()
    backend.add_robot("robot_bare")
    with robot_app(client_factory, settings_factory, backend=backend) as client:
        detail = client.get("/api/v1/robots/robot_bare").json()
        assert detail["cameras"] == []
        listed = client.get("/api/v1/robots").json()["items"]
        bare = next(item for item in listed if item["robot_id"] == "robot_bare")
        assert bare["cameras"] == []


def test_camera_video_source_accepted_by_media_session(
    client_factory, settings_factory
):
    """The §5.5 media create path consumes exactly the §5.9 video_source."""
    with robot_app(client_factory, settings_factory) as client:
        cameras = client.get(f"/api/v1/robots/{ROBOT_ID}").json()["cameras"]
        source = cameras[0]["video_source"]
        created = client.post(
            "/api/v1/media/sessions",
            json={
                "kind": "camera",
                "video_source": source,
                "audio": False,
                "video": True,
            },
        )
        assert created.status_code == 201, created.text
        session = created.json()
        assert session["kind"] == "camera"
        assert session["video_source"] == source
        fetched = client.get(
            f"/api/v1/media/sessions/{session['media_session_id']}"
        ).json()
        assert fetched["video_source"] == source


def test_unknown_robot_not_found(client_factory, settings_factory):
    with robot_app(client_factory, settings_factory) as client:
        for path in ("/api/v1/robots/nope", "/api/v1/robots/nope/state"):
            response = client.get(path)
            assert response.status_code == 404
            assert response.json()["error"]["code"] == "robot_not_found"
        acquire_response = client.post("/api/v1/robots/nope/control/acquire", json={})
        assert acquire_response.status_code == 404
        assert acquire_response.json()["error"]["code"] == "robot_not_found"


def test_offline_robot_not_ready(client_factory, settings_factory):
    backend = SimulatedRobotBackend()
    with robot_app(client_factory, settings_factory, backend=backend) as client:
        backend.set_connection(ROBOT_ID, "offline")
        state = client.get(f"/api/v1/robots/{ROBOT_ID}/state")
        assert state.status_code == 409
        assert state.json()["error"]["code"] == "robot_not_ready"
        acquire_response = client.post(
            f"/api/v1/robots/{ROBOT_ID}/control/acquire", json={}
        )
        assert acquire_response.status_code == 409
        assert acquire_response.json()["error"]["code"] == "robot_not_ready"
        # discovery still surfaces the offline robot
        listed = client.get("/api/v1/robots").json()
        assert listed["items"][0]["connection"] == "offline"


def test_backend_failure_mapping(client_factory, settings_factory):
    backend = SimulatedRobotBackend()
    with robot_app(client_factory, settings_factory, backend=backend) as client:
        backend.fail_next("timeout")
        timeout = client.get(f"/api/v1/robots/{ROBOT_ID}")
        assert timeout.status_code == 504
        assert timeout.json()["error"]["code"] == "robot_timeout"

        backend.fail_next("unavailable")
        unavailable = client.get(f"/api/v1/robots/{ROBOT_ID}")
        assert unavailable.status_code == 503
        assert unavailable.json()["error"]["code"] == "robot_unavailable"

        backend.fail_next("not_ready")
        not_ready = client.get(f"/api/v1/robots/{ROBOT_ID}")
        assert not_ready.status_code == 409
        assert not_ready.json()["error"]["code"] == "robot_not_ready"


# =====================================================================
# control authority (docs §4.6)
# =====================================================================
def test_acquire_returns_authority_and_expiry(client_factory, settings_factory):
    with robot_app(client_factory, settings_factory) as client:
        authority = acquire(client)
        assert set(authority) == AUTHORITY_FIELDS
        assert authority["robot_id"] == ROBOT_ID
        assert authority["mode"] == "teleoperation"
        assert authority["authority_id"].startswith("auth_")
        assert authority["expires_at"] is not None

        current = client.get(f"/api/v1/robots/{ROBOT_ID}/control").json()
        assert current["authority"]["authority_id"] == authority["authority_id"]
        assert current["authority"]["mode"] == "teleoperation"


def test_second_acquire_conflicts(client_factory, settings_factory):
    with robot_app(client_factory, settings_factory) as client:
        acquire(client)
        second = client.post(f"/api/v1/robots/{ROBOT_ID}/control/acquire", json={})
        assert second.status_code == 409
        assert second.json()["error"]["code"] == "control_authority_conflict"


def test_release_authority(client_factory, settings_factory):
    with robot_app(client_factory, settings_factory) as client:
        authority = acquire(client)
        released = client.post(
            f"/api/v1/robots/{ROBOT_ID}/control/release",
            json={"authority_id": authority["authority_id"]},
        )
        assert released.status_code == 200
        assert released.json() == {"robot_id": ROBOT_ID, "released": True}
        assert (
            client.get(f"/api/v1/robots/{ROBOT_ID}/control").json()["authority"] is None
        )
        again = client.post(f"/api/v1/robots/{ROBOT_ID}/control/release", json={})
        assert again.json()["released"] is False


def test_release_with_foreign_authority_rejected(client_factory, settings_factory):
    with robot_app(client_factory, settings_factory) as client:
        acquire(client)
        response = client.post(
            f"/api/v1/robots/{ROBOT_ID}/control/release",
            json={"authority_id": "auth_foreign"},
        )
        assert response.status_code == 403
        assert response.json()["error"]["code"] == "control_authority_required"


# =====================================================================
# teleoperation validation order (docs §4.7)
# =====================================================================
def test_teleop_requires_authority_to_open(client_factory, settings_factory):
    with robot_app(client_factory, settings_factory) as client:
        with client.websocket_connect(
            f"/api/v1/robots/{ROBOT_ID}/teleoperation"
        ) as ws:
            message = ws.receive_json()
            assert message["type"] == "control_lost"
            assert message["reason"] == "control_authority_required"


def test_command_without_authority_rejected(client_factory, settings_factory):
    with robot_app(client_factory, settings_factory) as client:
        authority = acquire(client)
        client.post(
            f"/api/v1/robots/{ROBOT_ID}/control/release",
            json={"authority_id": authority["authority_id"]},
        )
        with client.websocket_connect(
            f"/api/v1/robots/{ROBOT_ID}/teleoperation"
        ) as ws:
            assert ws.receive_json()["reason"] == "control_authority_required"
            # release removed authority, so the session cannot be re-established
        client.post(f"/api/v1/robots/{ROBOT_ID}/control/acquire", json={})
        with client.websocket_connect(
            f"/api/v1/robots/{ROBOT_ID}/teleoperation"
        ) as ws:
            ws.send_json(command(authority["authority_id"], 1))
            rejected = ws.receive_json()
            assert rejected["type"] == "command_rejected"
            assert rejected["reason"] == "control_authority_required"
            assert rejected["accepted"] is False


def test_command_with_foreign_authority_rejected(client_factory, settings_factory):
    with robot_app(client_factory, settings_factory) as client:
        acquire(client)
        with client.websocket_connect(
            f"/api/v1/robots/{ROBOT_ID}/teleoperation"
        ) as ws:
            ws.send_json(command("auth_foreign", 1))
            rejected = ws.receive_json()
            assert rejected["type"] == "command_rejected"
            assert rejected["reason"] == "control_authority_required"


def test_non_monotonic_sequence_rejected(client_factory, settings_factory):
    with robot_app(client_factory, settings_factory) as client:
        authority = acquire(client)
        with client.websocket_connect(
            f"/api/v1/robots/{ROBOT_ID}/teleoperation"
        ) as ws:
            ws.send_json(command(authority["authority_id"], 5))
            accepted = ws.receive_json()
            assert accepted["type"] == "teleop.feedback"
            assert accepted["accepted"] is True
            assert accepted["last_sequence"] == 5

            ws.send_json(command(authority["authority_id"], 3))
            rejected = ws.receive_json()
            assert rejected["type"] == "command_rejected"
            assert rejected["reason"] == "sequence_not_monotonic"
            # the rejected command did not advance the accepted cursor
            assert rejected["last_sequence"] == 5


def test_duplicate_then_foreign_authority_reports_authority_first(
    client_factory, settings_factory
):
    """§4.7 order: authority is checked before sequence monotonicity."""
    with robot_app(client_factory, settings_factory) as client:
        authority = acquire(client)
        with client.websocket_connect(
            f"/api/v1/robots/{ROBOT_ID}/teleoperation"
        ) as ws:
            ws.send_json(command(authority["authority_id"], 1))
            assert ws.receive_json()["accepted"] is True

            ws.send_json(command(authority["authority_id"], 1))
            duplicate = ws.receive_json()
            assert duplicate["type"] == "command_rejected"
            assert duplicate["reason"] == "sequence_not_monotonic"
            assert duplicate["last_sequence"] == 1

            # seq=2 is monotonic, but the authority is bogus -> authority wins
            ws.send_json(command("auth_bogus", 2))
            rejected = ws.receive_json()
            assert rejected["type"] == "command_rejected"
            assert rejected["reason"] == "control_authority_required"

            # the rejected commands did not advance the accepted cursor
            ws.send_json(command(authority["authority_id"], 2))
            accepted = ws.receive_json()
            assert accepted["accepted"] is True
            assert accepted["last_sequence"] == 2


def test_stale_timestamp_rejected(client_factory, settings_factory):
    import time

    with robot_app(client_factory, settings_factory) as client:
        authority = acquire(client)
        with client.websocket_connect(
            f"/api/v1/robots/{ROBOT_ID}/teleoperation"
        ) as ws:
            ws.send_json(
                command(
                    authority["authority_id"],
                    1,
                    client_timestamp=rfc3339(time.time() - 120.0),
                )
            )
            rejected = ws.receive_json()
            assert rejected["type"] == "command_rejected"
            assert rejected["reason"] == "stale_timestamp"


def test_velocity_over_limit_rejected(client_factory, settings_factory):
    with robot_app(client_factory, settings_factory) as client:
        authority = acquire(client)
        with client.websocket_connect(
            f"/api/v1/robots/{ROBOT_ID}/teleoperation"
        ) as ws:
            ws.send_json(command(authority["authority_id"], 1, linear_x=5.0))
            rejected = ws.receive_json()
            assert rejected["type"] == "command_rejected"
            assert rejected["reason"] == "velocity_limit"


# =====================================================================
# accepted commands move the simulated robot
# =====================================================================
def test_accepted_command_moves_simulated_state(client_factory, settings_factory):
    clock = ManualClock()
    backend = SimulatedRobotBackend(clock=clock)
    with robot_app(client_factory, settings_factory, backend=backend) as client:
        authority = acquire(client)
        with client.websocket_connect(
            f"/api/v1/robots/{ROBOT_ID}/teleoperation"
        ) as ws:
            ws.send_json(
                command(authority["authority_id"], 1, linear_x=0.2, angular_z=0.0)
            )
            feedback = ws.receive_json()
            assert feedback["accepted"] is True

            clock.advance(1.0)
            state = client.get(f"/api/v1/robots/{ROBOT_ID}/state").json()
            assert state["pose"]["x"] == 0.2
            assert state["velocity"]["linear_x"] == 0.2
            assert state["timestamp"] == rfc3339(clock.now)
            assert state["mode"] == "teleoperation"


# =====================================================================
# deadman / watchdog (docs §4.8)
# =====================================================================
def test_deadman_false_forces_zero_and_control_lost(
    client_factory, settings_factory
):
    clock = ManualClock()
    backend = SimulatedRobotBackend(clock=clock)
    with robot_app(client_factory, settings_factory, backend=backend) as client:
        authority = acquire(client)
        with client.websocket_connect(
            f"/api/v1/robots/{ROBOT_ID}/teleoperation"
        ) as ws:
            ws.send_json(
                command(
                    authority["authority_id"],
                    1,
                    linear_x=0.3,
                    angular_z=0.0,
                    deadman=False,
                )
            )
            feedback = ws.receive_json()
            assert feedback["type"] == "teleop.feedback"
            assert feedback["accepted"] is True
            assert feedback["watchdog_remaining_ms"] == 0

            lost = ws.receive_json()
            assert lost["type"] == "control_lost"
            assert lost["data"]["reason"] == "deadman"

            state = client.get(f"/api/v1/robots/{ROBOT_ID}/state").json()
            assert state["velocity"] == {
                "linear_x": 0.0,
                "linear_y": 0.0,
                "angular_z": 0.0,
            }


def test_watchdog_expiry_forces_zero_and_control_lost(
    client_factory, settings_factory
):
    backend = SimulatedRobotBackend()
    with robot_app(
        client_factory,
        settings_factory,
        backend=backend,
        robot_watchdog_timeout=0.15,
        robot_watchdog_tick=0.01,
    ) as client:
        authority = acquire(client)
        with client.websocket_connect(
            f"/api/v1/robots/{ROBOT_ID}/teleoperation"
        ) as ws:
            ws.send_json(
                command(authority["authority_id"], 1, linear_x=0.4, angular_z=0.0)
            )
            feedback = ws.receive_json()
            assert feedback["accepted"] is True

            # no further command -> server-side watchdog fires independently
            lost = ws.receive_json()
            assert lost["type"] == "control_lost"
            assert lost["data"]["reason"] == "watchdog"

            state = client.get(f"/api/v1/robots/{ROBOT_ID}/state").json()
            assert state["velocity"]["linear_x"] == 0.0


def test_authority_expiry(client_factory, settings_factory):
    with robot_app(
        client_factory,
        settings_factory,
        robot_authority_ttl=0.15,
        robot_watchdog_tick=0.01,
    ) as client:
        acquire(client)
        with client.websocket_connect(
            f"/api/v1/robots/{ROBOT_ID}/teleoperation"
        ) as ws:
            expired = ws.receive_json()
            assert expired["type"] == "authority_expired"
            assert expired["data"]["robot_id"] == ROBOT_ID
        assert (
            client.get(f"/api/v1/robots/{ROBOT_ID}/control").json()["authority"] is None
        )


# =====================================================================
# events WS (docs §4.5)
# =====================================================================
def test_events_ws_emits_state_mode_and_authority_events(
    client_factory, settings_factory
):
    # A long watch interval keeps the stream deterministic: the only events are
    # the connect snapshot plus the directly published mode/authority changes.
    with robot_app(
        client_factory, settings_factory, robot_watch_interval=30.0
    ) as client:
        with client.websocket_connect(
            f"/api/v1/robots/{ROBOT_ID}/events"
        ) as ws:
            snapshot = ws.receive_json()
            assert snapshot["type"] == "robot.state"
            assert snapshot["data"]["state"]["robot_id"] == ROBOT_ID

            authority = acquire(client)
            mode_changed = ws.receive_json()
            assert mode_changed["type"] == "robot.mode_changed"
            assert mode_changed["data"]["previous"] == "idle"
            assert mode_changed["data"]["current"] == "teleoperation"
            authority_changed = ws.receive_json()
            assert authority_changed["type"] == "control.authority_changed"
            assert (
                authority_changed["data"]["authority_id"]
                == authority["authority_id"]
            )

            client.post(
                f"/api/v1/robots/{ROBOT_ID}/control/release",
                json={"authority_id": authority["authority_id"]},
            )
            released_mode = ws.receive_json()
            assert released_mode["type"] == "robot.mode_changed"
            assert released_mode["data"]["current"] == "idle"
            released_authority = ws.receive_json()
            assert released_authority["type"] == "control.authority_changed"
            assert released_authority["data"]["authority_id"] is None


def test_events_ws_emits_connection_and_fault_events(
    client_factory, settings_factory
):
    backend = SimulatedRobotBackend()
    with robot_app(
        client_factory,
        settings_factory,
        backend=backend,
        robot_watch_interval=0.02,
    ) as client:
        with client.websocket_connect(
            f"/api/v1/robots/{ROBOT_ID}/events"
        ) as ws:
            assert ws.receive_json()["type"] == "robot.state"

            backend.set_connection(ROBOT_ID, "offline")
            connection = ws.receive_json()
            assert connection["type"] == "robot.connection_changed"
            assert connection["data"]["previous"] == "online"
            assert connection["data"]["current"] == "offline"
            assert ws.receive_json()["type"] == "robot.state"

            backend.set_connection(ROBOT_ID, "online")
            back_online = ws.receive_json()
            assert back_online["type"] == "robot.connection_changed"
            assert back_online["data"]["current"] == "online"
            assert ws.receive_json()["type"] == "robot.state"

            backend.inject_fault(ROBOT_ID, "estop", "critical", "E-stop pressed")
            fault = ws.receive_json()
            assert fault["type"] == "robot.fault"
            assert fault["data"]["fault"] == {
                "code": "estop",
                "severity": "critical",
                "message": "E-stop pressed",
            }
            assert ws.receive_json()["type"] == "robot.state"


# -- isolation: a broken robot layer must not affect core features ----------
class BrokenBackend:
    """Robot backend that cannot serve anything (future ROS2 backend未就绪)."""

    async def list_robots(self, *, timeout_ms: int = 0):
        raise RobotUnavailableError("robot backend is not reachable")

    async def get_robot_info(self, robot_id: str, *, timeout_ms: int = 0):
        raise RobotUnavailableError("robot backend is not reachable")

    async def get_robot_state(self, robot_id: str, *, timeout_ms: int = 0):
        raise RobotUnavailableError("robot backend is not reachable")

    async def acquire_control(self, robot_id: str, **kwargs):
        raise RobotUnavailableError("robot backend is not reachable")

    async def release_control(self, robot_id: str, **kwargs):
        raise RobotUnavailableError("robot backend is not reachable")

    async def send_velocity(self, robot_id: str, **kwargs):
        raise RobotUnavailableError("robot backend is not reachable")

    async def stop(self, robot_id: str, **kwargs):
        raise RobotUnavailableError("robot backend is not reachable")

    def watch_robot_state(self, robot_id: str, **kwargs):
        raise RobotUnavailableError("robot backend is not reachable")


def test_broken_robot_backend_does_not_break_core_features(
    client_factory, settings_factory, api
):
    """机器人层不可用时，agent/session/run/health 必须照常工作。"""
    with robot_app(
        client_factory, settings_factory, backend=BrokenBackend()
    ) as client:
        # core: health
        assert client.get("/api/v1/health").status_code == 200

        # core: session + run + projection
        session = api.create_session(client)
        run = api.start_run(client, session["session_id"], "still works")
        assert run.status_code == 201, run.text
        info = api.wait_terminal(client, run.json()["run_id"])
        assert info["status"] == "completed", info
        snapshot = client.get(f"/api/v1/sessions/{session['session_id']}").json()
        assert snapshot["messages"], snapshot

        # robot endpoints degrade to a registered Product error, not a crash
        robots = client.get("/api/v1/robots")
        assert robots.status_code == 503
        assert robots.json()["error"]["code"] == "robot_unavailable"
