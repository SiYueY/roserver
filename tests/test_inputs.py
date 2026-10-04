"""Steer / follow-up receipts and pending-input backpressure (docs §3.4, §3.6)."""

from __future__ import annotations

from conftest import FakeModel, Turn


def _body(text: str) -> dict:
    return {"input": {"client_message_id": "c1", "content": [{"type": "text", "text": text}]}}


def test_steer_receipt(client_factory, settings_factory, api):
    model = FakeModel([Turn(text="first", delay=0.2), Turn(text="second")])
    with client_factory(settings_factory(), model=model) as client:
        session = api.create_session(client)
        run = api.start_run(client, session["session_id"], "go").json()
        response = client.post(f"/api/v1/runs/{run['run_id']}/steer", json=_body("steer text"))
        assert response.status_code == 200, response.text
        receipt = response.json()
        assert receipt["session_id"] == session["session_id"]
        assert receipt["run_id"] == run["run_id"]
        assert receipt["kind"] == "steer"
        assert receipt["sequence"] >= 1
        assert receipt["input_id"]
        assert receipt["accepted_at"]
        api.wait_terminal(client, run["run_id"])
        snapshot = client.get(f"/api/v1/sessions/{session['session_id']}").json()
        texts = [
            block["text"]
            for message in snapshot["messages"]
            for block in message["content"]
            if block.get("type") == "text"
        ]
        assert "steer text" in texts


def test_follow_up_starts_run_when_idle(client_factory, settings_factory, api):
    model = FakeModel([Turn(text="followed up")])
    with client_factory(settings_factory(), model=model) as client:
        session = api.create_session(client)
        response = client.post(
            f"/api/v1/sessions/{session['session_id']}/follow-ups", json=_body("later")
        )
        assert response.status_code == 200, response.text
        receipt = response.json()
        assert receipt["kind"] == "follow_up"
        assert receipt["run_id"]
        terminal = api.wait_terminal(client, receipt["run_id"])
        assert terminal["status"] == "completed"
        snapshot = client.get(f"/api/v1/sessions/{session['session_id']}").json()
        texts = [
            block["text"]
            for message in snapshot["messages"]
            for block in message["content"]
            if block.get("type") == "text"
        ]
        assert "later" in texts


def test_pending_queue_full(client_factory, settings_factory, api):
    model = FakeModel([Turn(text="slow", delay=0.4)])
    with client_factory(settings_factory(max_pending_inputs=1), model=model) as client:
        session = api.create_session(client)
        run = api.start_run(client, session["session_id"], "go").json()
        first = client.post(f"/api/v1/runs/{run['run_id']}/steer", json=_body("one"))
        assert first.status_code == 200
        second = client.post(f"/api/v1/runs/{run['run_id']}/steer", json=_body("two"))
        assert second.status_code == 429
        assert second.json()["error"]["code"] == "pending_queue_full"
        api.wait_terminal(client, run["run_id"])


def test_steer_inactive_run(client_factory, settings_factory, api):
    with client_factory(settings_factory(), model=FakeModel([Turn(text="done")])) as client:
        session = api.create_session(client)
        run = api.start_run(client, session["session_id"], "go").json()
        api.wait_terminal(client, run["run_id"])
        response = client.post(f"/api/v1/runs/{run['run_id']}/steer", json=_body("late"))
        assert response.status_code == 409
        assert response.json()["error"]["code"] == "session_busy"
