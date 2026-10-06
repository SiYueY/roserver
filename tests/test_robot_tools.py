"""Phase 3B: the Agent Tool path must go through RobotService.

Also covers: rodesk Robot State and rodesk manual commands use the same
RobotService capability layer (see tests/test_robot.py for the HTTP surface).
"""

from __future__ import annotations

import asyncio

from conftest import FakeModel, Turn
from roboagent import Agent
from roboagent.context import PromptInput
from roboagent.message import FrozenJsonObject, ToolCall, UserMessage
from roboagent.tool import ToolRegistry
from roboagent.runtime import RunStatus

from roserver.config import Settings
from roserver.robot.backend import SimulatedRobotBackend
from roserver.robot.service import RobotService
from roserver.robot.tools import robot_tools
from roserver.store.application import ApplicationStore

ROBOT_ID = "robot_1"


def _build(tmp_path):
    settings = Settings(data_dir=tmp_path, model="echo")
    store = ApplicationStore(settings.resolved_db_path)
    backend = SimulatedRobotBackend()
    service = RobotService(settings=settings, backend=backend, store=store)
    return settings, store, service


def test_agent_tool_reads_robot_state_through_service(tmp_path):
    async def check() -> None:
        settings, store, service = _build(tmp_path)
        await store.open()
        tools = robot_tools(service, settings)
        call = ToolCall("call_state", "get_robot_state", FrozenJsonObject({}))
        agent = Agent(
            FakeModel([Turn(text="", tool_calls=(call,)), Turn(text="reported")]),
            tool_registry=ToolRegistry(tools),
            prompt=PromptInput("Operate the robot."),
        )
        session = agent.new_session(session_id="s")
        result = await session.start(
            UserMessage("what is the robot doing?")
        ).result()

        assert result.status is RunStatus.COMPLETED, result.error
        tool_messages = [m for m in session.messages if m.role == "tool"]
        assert tool_messages, session.messages
        payload = tool_messages[0].content[0].value
        assert payload["robot_id"] == ROBOT_ID
        assert payload["connection"] == "online"
        await store.close()

    asyncio.run(check())


def test_agent_tool_resolves_display_name_to_only_robot(tmp_path):
    async def check() -> None:
        settings, store, service = _build(tmp_path)
        await store.open()
        tools = robot_tools(service, settings)
        call = ToolCall(
            "call_display_name", "get_robot_state", FrozenJsonObject({"robot_id": "MFR3Duo"})
        )
        agent = Agent(
            FakeModel([Turn(text="", tool_calls=(call,)), Turn(text="reported")]),
            tool_registry=ToolRegistry(tools),
            prompt=PromptInput("Operate the robot."),
        )
        session = agent.new_session(session_id="display-name")
        result = await session.start(UserMessage("read the robot state")).result()

        assert result.status is RunStatus.COMPLETED, result.error
        message = next(message for message in session.messages if message.role == "tool")
        assert message.content[0].value["robot_id"] == ROBOT_ID
        await store.close()

    asyncio.run(check())


def test_agent_tool_simulation_navigate_goes_through_robot_service(tmp_path):
    async def check() -> None:
        settings, store, service = _build(tmp_path)
        await store.open()
        before = await service.get_robot_state(ROBOT_ID)
        tools = robot_tools(service, settings)
        call = ToolCall(
            "call_nav",
            "simulate_navigate",
            FrozenJsonObject({"target": "meeting_room"}),
        )
        agent = Agent(
            FakeModel([Turn(text="", tool_calls=(call,)), Turn(text="done")]),
            tool_registry=ToolRegistry(tools),
            prompt=PromptInput("Operate the robot."),
        )
        session = agent.new_session(session_id="s2")
        result = await session.start(UserMessage("go to the meeting room")).result()
        assert result.status is RunStatus.COMPLETED, result.error

        tool_messages = [m for m in session.messages if m.role == "tool"]
        assert tool_messages, session.messages
        report = tool_messages[0].content[0].value
        # Explicitly a simulation, not a real navigation capability.
        assert report["simulated"] is True
        assert report["target"] == "meeting_room"
        assert "mfr3duo_nav" in report["note"]

        # The command genuinely reached RobotService: the simulated robot moved,
        # and the authority was released afterwards.
        after = await service.get_robot_state(ROBOT_ID)
        assert after["pose"]["x"] != before["pose"]["x"]
        assert await service.has_authority(ROBOT_ID) is False
        await store.close()

    asyncio.run(check())


def test_simulation_navigate_requires_a_target(tmp_path):
    async def check() -> None:
        settings, store, service = _build(tmp_path)
        await store.open()
        tools = robot_tools(service, settings)
        call = ToolCall("call_bad", "simulate_navigate", FrozenJsonObject({}))
        agent = Agent(
            FakeModel([Turn(text="", tool_calls=(call,)), Turn(text="failed")]),
            tool_registry=ToolRegistry(tools),
            prompt=PromptInput("Operate."),
        )
        session = agent.new_session(session_id="s3")
        result = await session.start(UserMessage("navigate nowhere")).result()
        # The tool fails, but the Run itself still terminates cleanly.
        assert result.status in (RunStatus.COMPLETED, RunStatus.FAILED)
        assert await service.has_authority(ROBOT_ID) is False
        await store.close()

    asyncio.run(check())
