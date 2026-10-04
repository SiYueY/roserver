"""Restart reconciliation, pending cleanup and WS replay/resync."""

from __future__ import annotations

from conftest import (
    FakeModel,
    RequireApprovalPolicy,
    Turn,
    fake_tool_call,
    make_fake_tool,
)


def _steer(client, run_id: str, text: str = "queued"):
    return client.post(
        f"/api/v1/runs/{run_id}/steer",
        json={"input": {"client_message_id": "c1", "content": [{"type": "text", "text": text}]}},
        headers={"Content-Type": "application/json"},
    )


def test_restart_interrupts_active_run_and_clears_pending(
    client_factory, settings_factory, api
):
    with client_factory(
        settings_factory(),
        model=FakeModel([Turn(text="slow", delay=5.0)]),
        # A real restart is a process crash: no lifespan shutdown may cancel the
        # active Run, otherwise startup reconciliation has nothing to interrupt.
        crash_on_exit=True,
    ) as client:
        session = api.create_session(client)
        run = api.start_run(client, session["session_id"], "go").json()
        # Wait until the runtime Run is actually running.
        for _ in range(200):
            record = client.get(f"/api/v1/runs/{run['run_id']}").json()
            if record["status"] == "running":
                break
        assert _steer(client, run["run_id"]).status_code == 200

    with client_factory(settings_factory(), model=FakeModel([Turn(text="x")])) as client:
        record = client.get(f"/api/v1/runs/{run['run_id']}").json()
        assert record["status"] == "interrupted"
        assert record["ended_at"]

        snapshot = client.get(f"/api/v1/sessions/{session['session_id']}").json()
        assert snapshot["active_root_run_id"] is None
        assert snapshot["status"] == "idle"

        events = api.collect_events(
            client,
            run["run_id"],
            after_sequence=0,
            stop_type="run.interrupted",
        )
        interrupted = next(e for e in events if e["type"] == "run.interrupted")
        assert interrupted["data"]["reason"] == "server_restarted"
        assert interrupted["data"]["discarded_pending_count"] == 1

        # The queued steer is gone: a later Run must not consume it.
        again = api.start_run(client, session["session_id"], "fresh").json()
        api.wait_terminal(client, again["run_id"])
        final = client.get(f"/api/v1/sessions/{session['session_id']}").json()
        texts = [
            block["text"]
            for message in final["messages"]
            for block in message["content"]
            if block.get("type") == "text"
        ]
        assert "queued" not in texts


def test_ws_resync_when_buffer_gone(client_factory, settings_factory, api):
    with client_factory(
        settings_factory(), model=FakeModel([Turn(text="finished")])
    ) as client:
        session = api.create_session(client)
        run = api.start_run(client, session["session_id"], "go").json()
        api.wait_terminal(client, run["run_id"])

    # New process over the same ApplicationStore / session dirs: in-memory
    # RunProjection and PublicEventBuffer are gone.
    with client_factory(settings_factory(), model=FakeModel([Turn(text="x")])) as client:
        assert client.get(f"/api/v1/runs/{run['run_id']}/projection").status_code == 404
        events = api.collect_events(
            client,
            run["run_id"],
            after_sequence=0,
            stop_type="stream.resync_required",
        )
        assert events[0]["type"] == "stream.resync_required"
        assert events[0]["data"]["requested_after_sequence"] == 0
        assert events[0]["data"]["oldest_available_sequence"] is None


def test_ws_replay_after_pruning_requires_resync(
    client_factory, settings_factory, api
):
    model = FakeModel([Turn(text="abcdefghijklmnopqrstuvwxyz", chunks=20)])
    with client_factory(
        settings_factory(max_replay_events=4, max_replay_bytes=10_000_000),
        model=model,
    ) as client:
        session = api.create_session(client)
        run = api.start_run(client, session["session_id"], "go").json()
        api.wait_terminal(client, run["run_id"])
        events = api.collect_events(
            client,
            run["run_id"],
            after_sequence=0,
            stop_type="stream.resync_required",
        )
        assert events[0]["type"] == "stream.resync_required"


def test_live_only_subscription_without_after_sequence(
    client_factory, settings_factory, api
):
    with client_factory(
        settings_factory(), model=FakeModel([Turn(text="live", delay=1.0)])
    ) as client:
        session = api.create_session(client)
        run = api.start_run(client, session["session_id"], "go").json()
        events = api.collect_events(
            client, run["run_id"], after_sequence=None, timeout=15.0
        )
        types = [event["type"] for event in events]
        assert "run.completed" in types
        # No replay cursor: only events published after subscribing.
        assert types[0] != "run.started"


def test_restart_cancels_pending_approval(client_factory, settings_factory, api):
    model = FakeModel(
        [Turn(tool_calls=(fake_tool_call(),)), Turn(text="done", delay=0.5)]
    )
    with client_factory(
        settings_factory(),
        model=model,
        tools=[make_fake_tool()],
        tool_policy=RequireApprovalPolicy(),
        # Crash (not a graceful stop) so the pending approval and the blocked
        # Run survive into the next process's startup reconciliation.
        crash_on_exit=True,
    ) as client:
        session = api.create_session(client)
        run = api.start_run(client, session["session_id"], "do it").json()
        approval = api.wait_approval(client, run["run_id"])
        approval_id = approval["approval_id"]

    with client_factory(settings_factory(), model=FakeModel([Turn(text="x")])) as client:
        info = client.get(f"/api/v1/approvals/{approval_id}").json()
        assert info["status"] == "cancelled"
        assert info["decision"] is None
        record = client.get(f"/api/v1/runs/{run['run_id']}").json()
        assert record["status"] == "interrupted"
