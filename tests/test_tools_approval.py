"""Tool/effect projection, assistant.aborted synthesis and approval plumbing."""

from __future__ import annotations

import asyncio
import time

from conftest import (
    FakeModel,
    RequireApprovalPolicy,
    Turn,
    fake_tool_call,
    make_fake_tool,
)
from roboagent.tool import AllowAllToolPolicy
from roserver.app import create_app


def test_tool_execution_and_effect_projection(client_factory, settings_factory, api):
    model = FakeModel(
        [
            Turn(text="calling", tool_calls=(fake_tool_call(),)),
            Turn(text="done"),
        ]
    )
    with client_factory(
        settings_factory(),
        model=model,
        tools=[make_fake_tool()],
        tool_policy=AllowAllToolPolicy(),
    ) as client:
        session = api.create_session(client)
        run = api.start_run(client, session["session_id"], "use tool").json()
        terminal = api.wait_terminal(client, run["run_id"])
        assert terminal["status"] == "completed"
        assert len(terminal["effects"]) == 1
        effect = terminal["effects"][0]
        assert effect["tool_call_id"] == "call_1"
        assert effect["effect_status"] == "succeeded"
        assert effect["certainty"] == "certain"
        assert effect["summary"]

        projection = client.get(f"/api/v1/runs/{run['run_id']}/projection").json()
        assert len(projection["tools"]) == 1
        tool = projection["tools"][0]
        assert tool["execution_status"] == "completed"
        assert tool["effect_status"] == "succeeded"
        assert tool["certainty"] == "certain"
        assert tool["tool_name"] == "fake_tool"
        assert tool["message_id"]

        events = api.collect_events(client, run["run_id"], after_sequence=0)
        types = [event["type"] for event in events]
        assert types[-1] == "run.completed"
        assert "tool.started" in types
        assert "tool.execution_finished" in types
        assert "tool.effect_committed" in types
        assert types.index("tool.started") < types.index("tool.execution_finished")
        assert types.index("tool.execution_finished") < types.index("tool.effect_committed")

        finished = next(e for e in events if e["type"] == "tool.execution_finished")
        assert finished["data"]["execution_status"] == "completed"
        committed = next(e for e in events if e["type"] == "tool.effect_committed")
        assert committed["data"]["effect_status"] == "succeeded"
        assert committed["data"]["certainty"] == "certain"

        snapshot = client.get(f"/api/v1/sessions/{session['session_id']}").json()
        roles = [message["role"] for message in snapshot["messages"]]
        assert roles == ["user", "assistant", "tool", "assistant"]
        tool_message = snapshot["messages"][2]
        assert tool_message["tool_call_id"] == "call_1"
        assert tool_message["status"] == "success"
        assert tool_message["error"] is None


def test_default_policy_requires_approval_for_side_effects(settings_factory):
    app = create_app(
        settings_factory(), model=FakeModel([Turn(text="unused")]), tools=[make_fake_tool()]
    )
    agent = app.state.service.agent
    tool = agent.tool_registry.get("fake_tool")
    decision = asyncio.run(agent.tool_policy.evaluate(fake_tool_call(), tool, None))
    assert decision.action.value == "require_approval"


def test_assistant_aborted_on_run_failure(client_factory, settings_factory, api):
    model = FakeModel([Turn(text="partial answer", fail="boom", chunks=2)])
    with client_factory(settings_factory(), model=model) as client:
        session = api.create_session(client)
        run = api.start_run(client, session["session_id"], "go").json()
        terminal = api.wait_terminal(client, run["run_id"])
        assert terminal["status"] == "failed"
        assert terminal["error"]["code"] == "model_failed"

        projection = client.get(f"/api/v1/runs/{run['run_id']}/projection").json()
        entry = projection["assistant_messages"][0]
        assert entry["state"] == "aborted"

        events = api.collect_events(
            client, run["run_id"], after_sequence=0, stop_type="run.failed"
        )
        types = [event["type"] for event in events]
        assert "assistant.started" in types
        assert "assistant.aborted" in types
        aborted = next(e for e in events if e["type"] == "assistant.aborted")
        assert aborted["data"]["reason"] == "run_failed"
        failed = next(e for e in events if e["type"] == "run.failed")
        assert failed["data"]["error"]["code"] == "model_failed"
        assert types.index("assistant.aborted") < types.index("run.failed")


def test_cancel_run_and_aborted(client_factory, settings_factory, api):
    model = FakeModel([Turn(text="slow answer", delay=0.4)])
    with client_factory(settings_factory(), model=model) as client:
        session = api.create_session(client)
        run = api.start_run(client, session["session_id"], "go").json()
        cancelled = client.post(f"/api/v1/runs/{run['run_id']}/cancel")
        assert cancelled.status_code == 200, cancelled.text
        assert cancelled.json()["status"] == "cancelled"
        # Let the runtime Run settle before reading the projection or subscribing,
        # so cancel teardown cannot race the rest of the test.
        assert api.wait_terminal(client, run["run_id"])["status"] == "cancelled"

        projection = client.get(f"/api/v1/runs/{run['run_id']}/projection").json()
        assert projection["status"] == "cancelled"
        if projection["assistant_messages"]:
            assert projection["assistant_messages"][0]["state"] == "aborted"

        events = api.collect_events(
            client, run["run_id"], after_sequence=0, stop_type="run.cancelled"
        )
        types = [event["type"] for event in events]
        assert types[-1] == "run.cancelled"
        if "assistant.aborted" in types:
            aborted = next(e for e in events if e["type"] == "assistant.aborted")
            assert aborted["data"]["reason"] == "run_cancelled"


def test_approval_resolve_happy_path(client_factory, settings_factory, api):
    model = FakeModel(
        [Turn(tool_calls=(fake_tool_call(),)), Turn(text="approved and done")]
    )
    with client_factory(
        settings_factory(),
        model=model,
        tools=[make_fake_tool()],
        tool_policy=RequireApprovalPolicy(),
    ) as client:
        session = api.create_session(client)
        run = api.start_run(client, session["session_id"], "do it").json()
        approval = api.wait_approval(client, run["run_id"])
        info = client.get(f"/api/v1/approvals/{approval['approval_id']}").json()
        assert info["status"] == "pending"
        assert info["tool_call_id"] == "call_1"
        assert info["arguments_digest"]

        response = client.post(
            f"/api/v1/approvals/{approval['approval_id']}/resolve",
            json={
                "decision": "approve",
                "arguments_digest": info["arguments_digest"],
                "reason": "operator confirmed",
            },
        )
        assert response.status_code == 200, response.text
        resolved = response.json()
        assert resolved["status"] == "approved"
        assert resolved["decision"] == "approve"
        assert resolved["resolved_at"]

        terminal = api.wait_terminal(client, run["run_id"])
        assert terminal["status"] == "completed"

        stored = client.get(f"/api/v1/approvals/{approval['approval_id']}").json()
        assert stored["status"] == "approved"

        events = api.collect_events(client, run["run_id"], after_sequence=0)
        types = [event["type"] for event in events]
        assert "approval.requested" in types
        assert "approval.resolved" in types


def test_approval_digest_mismatch_and_already_resolved(
    client_factory, settings_factory, api
):
    model = FakeModel([Turn(tool_calls=(fake_tool_call(),)), Turn(text="done")])
    with client_factory(
        settings_factory(),
        model=model,
        tools=[make_fake_tool()],
        tool_policy=RequireApprovalPolicy(),
    ) as client:
        session = api.create_session(client)
        run = api.start_run(client, session["session_id"], "do it").json()
        approval = api.wait_approval(client, run["run_id"])
        info = client.get(f"/api/v1/approvals/{approval['approval_id']}").json()
        url = f"/api/v1/approvals/{approval['approval_id']}/resolve"

        mismatch = client.post(
            url,
            json={"decision": "approve", "arguments_digest": "sha256:" + "0" * 64},
        )
        assert mismatch.status_code == 409
        assert mismatch.json()["error"]["code"] == "approval_digest_mismatch"

        assert (
            client.post(
                url,
                json={"decision": "approve", "arguments_digest": info["arguments_digest"]},
            ).status_code
            == 200
        )
        conflict = client.post(
            url,
            json={"decision": "deny", "arguments_digest": info["arguments_digest"]},
        )
        assert conflict.status_code == 409
        assert conflict.json()["error"]["code"] == "approval_already_resolved"
        replay = client.post(
            url,
            json={"decision": "approve", "arguments_digest": info["arguments_digest"]},
        )
        assert replay.status_code == 200
        assert replay.json()["status"] == "approved"
        api.wait_terminal(client, run["run_id"])


def test_approval_resolve_is_idempotent(client_factory, settings_factory, api):
    model = FakeModel([Turn(tool_calls=(fake_tool_call(),)), Turn(text="done")])
    with client_factory(
        settings_factory(),
        model=model,
        tools=[make_fake_tool()],
        tool_policy=RequireApprovalPolicy(),
    ) as client:
        session = api.create_session(client)
        run = api.start_run(client, session["session_id"], "do it").json()
        approval = api.wait_approval(client, run["run_id"])
        info = client.get(f"/api/v1/approvals/{approval['approval_id']}").json()
        body = {"decision": "approve", "arguments_digest": info["arguments_digest"]}
        url = f"/api/v1/approvals/{approval['approval_id']}/resolve"
        first = client.post(url, json=body, headers={"Idempotency-Key": "approve-1"})
        assert first.status_code == 200
        replay = client.post(url, json=body, headers={"Idempotency-Key": "approve-1"})
        assert replay.status_code == 200
        assert replay.headers["Idempotency-Replayed"] == "true"
        assert replay.json() == first.json()
        api.wait_terminal(client, run["run_id"])


def test_approval_expired(client_factory, settings_factory, api):
    model = FakeModel([Turn(tool_calls=(fake_tool_call(),)), Turn(text="done")])
    with client_factory(
        settings_factory(approval_ttl=0.2),
        model=model,
        tools=[make_fake_tool()],
        tool_policy=RequireApprovalPolicy(),
    ) as client:
        session = api.create_session(client)
        run = api.start_run(client, session["session_id"], "do it").json()
        approval = api.wait_approval(client, run["run_id"])
        info = client.get(f"/api/v1/approvals/{approval['approval_id']}").json()
        time.sleep(0.5)
        response = client.post(
            f"/api/v1/approvals/{approval['approval_id']}/resolve",
            json={"decision": "approve", "arguments_digest": info["arguments_digest"]},
        )
        assert response.status_code == 410
        assert response.json()["error"]["code"] == "approval_expired"
        api.wait_terminal(client, run["run_id"])
