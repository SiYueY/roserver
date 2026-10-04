"""Product error envelope, content negotiation and idempotency (docs §3.4, §3.5, §3.12)."""

from __future__ import annotations

from conftest import FakeModel, Turn


def _error(response) -> dict:
    body = response.json()
    assert set(body) == {"error"}
    return body["error"]


def test_unsupported_media_type(client_factory, settings_factory, api):
    with client_factory(settings_factory(), model=FakeModel([Turn(text="x")])) as client:
        session = api.create_session(client)
        response = client.post(
            f"/api/v1/sessions/{session['session_id']}/runs",
            content=b'{"input": {"content": [{"type": "text", "text": "hi"}]}}',
            headers={"Content-Type": "text/plain"},
        )
        assert response.status_code == 415
        error = _error(response)
        assert error["code"] == "unsupported_media_type"
        assert error["request_id"]


def test_invalid_json_and_missing_content(client_factory, settings_factory, api):
    with client_factory(settings_factory(), model=FakeModel([Turn(text="x")])) as client:
        session = api.create_session(client)
        url = f"/api/v1/sessions/{session['session_id']}/runs"
        response = client.post(url, content=b"{not json", headers={"Content-Type": "application/json"})
        assert response.status_code == 400
        assert _error(response)["code"] == "invalid_input"
        response = client.post(url, json={}, headers={"Content-Type": "application/json"})
        assert response.status_code == 400
        assert _error(response)["code"] == "invalid_input"


def test_unsupported_content_type_and_empty(client_factory, settings_factory, api):
    with client_factory(settings_factory(), model=FakeModel([Turn(text="x")])) as client:
        session = api.create_session(client)
        url = f"/api/v1/sessions/{session['session_id']}/runs"
        # Phase 4 accepts image/audio/file/artifact_reference; an unknown artifact
        # is therefore a 404, not an unsupported content type.
        response = client.post(
            url,
            json={"input": {"content": [{"type": "image", "artifact_id": "sha256:" + "a" * 64}]}},
        )
        assert response.status_code == 404
        assert _error(response)["code"] == "artifact_not_found"
        # A genuinely unknown content type stays unsupported.
        response = client.post(
            url, json={"input": {"content": [{"type": "video"}]}}
        )
        assert response.status_code == 422
        assert _error(response)["code"] == "unsupported_content_type"
        # A malformed artifact id is invalid input.
        response = client.post(
            url, json={"input": {"content": [{"type": "image", "artifact_id": "nope"}]}}
        )
        assert response.status_code == 400
        assert _error(response)["code"] == "invalid_input"
        response = client.post(url, json={"input": {"content": []}})
        assert response.status_code == 422
        assert _error(response)["code"] == "unsupported_content_type"


def test_payload_too_large(client_factory, settings_factory, api):
    with client_factory(
        settings_factory(max_agent_input_bytes=64, max_content_blocks=64),
        model=FakeModel([Turn(text="x")]),
    ) as client:
        session = api.create_session(client)
        response = api.start_run(client, session["session_id"], "x" * 500)
        assert response.status_code == 413
        assert _error(response)["code"] == "payload_too_large"


def test_unknown_session_and_run_errors(client_factory, settings_factory):
    with client_factory(settings_factory(), model=FakeModel([Turn(text="x")])) as client:
        assert _error(client.get("/api/v1/sessions/nope"))["code"] == "session_not_found"
        assert _error(client.get("/api/v1/runs/nope"))["code"] == "run_not_found"
        assert (
            _error(client.get("/api/v1/approvals/nope"))["code"] == "approval_not_found"
        )
        # Unknown route still uses the Product error envelope.
        response = client.get("/api/v1/does-not-exist")
        assert response.status_code == 404
        assert _error(response)["code"] == "invalid_input"


def test_idempotent_run_start(client_factory, settings_factory, api):
    with client_factory(settings_factory(), model=FakeModel([Turn(text="once")])) as client:
        session = api.create_session(client)
        first = api.start_run(client, session["session_id"], "hello", idempotency_key="k1")
        assert first.status_code == 201, first.text
        body = first.json()

        replay = api.start_run(client, session["session_id"], "hello", idempotency_key="k1")
        assert replay.status_code == 201
        assert replay.json() == body
        assert replay.headers["Idempotency-Replayed"] == "true"

        conflict = api.start_run(
            client, session["session_id"], "different", idempotency_key="k1"
        )
        assert conflict.status_code == 409
        assert _error(conflict)["code"] == "idempotency_conflict"
        api.wait_terminal(client, body["run_id"])
        snapshot = client.get(f"/api/v1/sessions/{session['session_id']}").json()
        # Replay did not create a second user turn.
        assert [m["role"] for m in snapshot["messages"]].count("user") == 1


def test_idempotent_failure_replay(client_factory, settings_factory, api):
    with client_factory(settings_factory(), model=FakeModel([Turn(text="x")])) as client:
        response = api.start_run(client, "missing", "hello", idempotency_key="bad")
        assert response.status_code == 404
        replay = api.start_run(client, "missing", "hello", idempotency_key="bad")
        assert replay.status_code == 404
        assert replay.headers["Idempotency-Replayed"] == "true"
        assert _error(replay)["code"] == "session_not_found"


def test_list_sessions_cursor(client_factory, settings_factory, api):
    import time

    with client_factory(settings_factory(), model=FakeModel([Turn(text="x")])) as client:
        for index in range(3):
            api.create_session(client, f"S{index}")
            time.sleep(0.01)
        page = client.get("/api/v1/sessions?limit=2").json()
        assert len(page["items"]) == 2
        assert page["next_cursor"]
        next_page = client.get(
            f"/api/v1/sessions?limit=2&cursor={page['next_cursor']}"
        ).json()
        assert len(next_page["items"]) == 1
        assert next_page["next_cursor"] is None
        bad = client.get("/api/v1/sessions?order=name")
        assert bad.status_code == 400
