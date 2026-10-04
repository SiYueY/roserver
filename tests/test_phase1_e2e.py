"""Phase 0/1 end-to-end: sessions, runs, streaming, projection."""

from __future__ import annotations


from conftest import FakeModel, Turn



def test_health(client_factory, settings_factory):
    with client_factory(settings_factory(), model=FakeModel([Turn(text="ok")])) as client:
        response = client.get("/api/v1/health")
        assert response.status_code == 200
        assert response.json()["status"] == "ok"
        assert response.headers["X-Request-Id"]


def test_session_crud_and_run(client_factory, settings_factory, api):
    with client_factory(
        settings_factory(), model=FakeModel([Turn(text="Hello there")])
    ) as client:
        created = api.create_session(client, "Chat 1")
        session_id = created["session_id"]
        assert created["title"] == "Chat 1"
        assert created["status"] == "idle"

        listed = client.get("/api/v1/sessions").json()
        assert [item["session_id"] for item in listed["items"]] == [session_id]

        fetched = client.get(f"/api/v1/sessions/{session_id}").json()
        assert fetched["session_id"] == session_id
        assert fetched["messages"] == []

        patched = client.patch(
            f"/api/v1/sessions/{session_id}", json={"title": "Renamed"}
        ).json()
        assert patched["title"] == "Renamed"

        started = api.start_run(client, session_id, "Hi")
        assert started.status_code == 201, started.text
        run = started.json()
        assert run["status"] == "created"
        assert run["root_run_id"] == run["run_id"]
        assert run["user_message_id"]

        terminal = api.wait_terminal(client, run["run_id"])
        assert terminal["status"] == "completed"
        assert terminal["usage"]["usage_known"] is False
        assert terminal["parent_run_id"] is None
        assert terminal["agent_tool_name"] is None
        assert terminal["retry_safe"] is True

        snapshot = client.get(f"/api/v1/sessions/{session_id}").json()
        assert snapshot["status"] == "idle"
        roles = [message["role"] for message in snapshot["messages"]]
        assert roles == ["user", "assistant"]
        assistant = snapshot["messages"][1]
        assert assistant["content"] == [{"type": "text", "text": "Hello there"}]
        assert assistant["message_id"]


def test_assistant_stream_stable_message_id(client_factory, settings_factory, api):
    with client_factory(
        settings_factory(), model=FakeModel([Turn(text="streamed reply", chunks=3)])
    ) as client:
        session = api.create_session(client)
        run = api.start_run(client, session["session_id"], "go").json()
        terminal = api.wait_terminal(client, run["run_id"])
        assert terminal["status"] == "completed"

        events = api.collect_events(client, run["run_id"], after_sequence=0)
        types = [event["type"] for event in events]
        assert types[0] == "run.started"
        assert "assistant.started" in types
        assert types[-1] == "run.completed"
        sequences = [event["sequence"] for event in events]
        assert sequences == sorted(sequences)
        assert sequences[0] == 1

        started = [e for e in events if e["type"] == "assistant.started"]
        deltas = [e for e in events if e["type"] == "assistant.delta"]
        completed = [e for e in events if e["type"] == "assistant.completed"]
        assert len(started) == 1 and len(completed) == 1
        assert deltas
        message_id = started[0]["data"]["message_id"]
        assert all(e["data"]["message_id"] == message_id for e in deltas)
        assert all(e["data"]["block_index"] == 0 for e in deltas)
        assert "".join(e["data"]["delta"] for e in deltas) == "streamed reply"

        snapshot = client.get(f"/api/v1/sessions/{session['session_id']}").json()
        assert snapshot["messages"][-1]["message_id"] == message_id
        assert types.index("assistant.completed") < types.index("run.completed")


def test_run_projection(client_factory, settings_factory, api):
    with client_factory(
        settings_factory(), model=FakeModel([Turn(text="projected")])
    ) as client:
        session = api.create_session(client)
        run = api.start_run(client, session["session_id"], "go").json()
        api.wait_terminal(client, run["run_id"])
        projection = client.get(f"/api/v1/runs/{run['run_id']}/projection").json()
        assert projection["root_run_id"] == run["run_id"]
        assert projection["status"] == "completed"
        assert projection["last_sequence"] >= 1
        assert len(projection["assistant_messages"]) == 1
        entry = projection["assistant_messages"][0]
        assert entry["state"] == "completed"
        assert entry["blocks"] == [{"type": "text", "text": "projected"}]


def test_session_busy_on_second_run(client_factory, settings_factory, api):
    with client_factory(
        settings_factory(), model=FakeModel([Turn(text="slow", delay=0.3)])
    ) as client:
        session = api.create_session(client)
        first = api.start_run(client, session["session_id"], "one")
        assert first.status_code == 201
        second = api.start_run(client, session["session_id"], "two")
        assert second.status_code == 409
        assert second.json()["error"]["code"] == "session_busy"
        api.wait_terminal(client, first.json()["run_id"])


def test_session_delete_saga(client_factory, settings_factory, api):
    with client_factory(
        settings_factory(), model=FakeModel([Turn(text="ok")])
    ) as client:
        session = api.create_session(client)
        session_id = session["session_id"]
        assert client.delete(f"/api/v1/sessions/{session_id}").status_code == 204
        assert client.get(f"/api/v1/sessions/{session_id}").status_code == 404
        # DELETE is naturally idempotent.
        assert client.delete(f"/api/v1/sessions/{session_id}").status_code == 204


def test_session_delete_busy(client_factory, settings_factory, api):
    with client_factory(
        settings_factory(), model=FakeModel([Turn(text="slow", delay=0.3)])
    ) as client:
        session = api.create_session(client)
        run = api.start_run(client, session["session_id"], "go").json()
        response = client.delete(f"/api/v1/sessions/{session['session_id']}")
        assert response.status_code == 409
        assert response.json()["error"]["code"] == "session_busy"
        api.wait_terminal(client, run["run_id"])
