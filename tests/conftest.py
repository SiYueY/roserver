"""Shared test fixtures: a deterministic fake model, fake tool and live server."""

from __future__ import annotations

import asyncio
import json
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, Iterator, Sequence

import httpx
import pytest
import uvicorn
from websockets.sync.client import connect as websocket_connect

from roboagent.message import (
    AssistantMessage,
    FrozenJsonObject,
    TextContent,
    ToolCall,
)
from roboagent.model import (
    FinishReason,
    ModelCapabilities,
    ModelResponse,
    ResponseCompleted,
    ResponseStarted,
    TextDelta,
    ToolCallCompleted,
    ToolCallStarted,
    Usage,
    UsageUpdated,
)
from roboagent.tool import (
    Tool,
    ToolDecision,
    ToolDefinition,
    ToolEffectKind,
    ToolExecutionMode,
    ToolPolicyDecision,
    ToolTextContent,
)

from roserver.app import create_app
from roserver.config import Settings


@dataclass
class Turn:
    text: str = ""
    tool_calls: tuple[ToolCall, ...] = ()
    usage: Usage | None = None
    delay: float = 0.0
    chunks: int = 3
    fail: str | None = None


def _split(text: str, chunks: int) -> list[str]:
    if not text:
        return []
    if chunks <= 1:
        return [text]
    size = max(1, len(text) // chunks)
    parts = [text[index : index + size] for index in range(0, len(text), size)]
    return parts or [text]


class FakeModel:
    """Deterministic offline model that streams one scripted Turn per call."""

    def __init__(self, turns: Sequence[Turn]) -> None:
        self.turns = list(turns) or [Turn(text="ok")]
        self.index = 0
        self.started = 0

    @property
    def capabilities(self) -> ModelCapabilities:
        return ModelCapabilities(tool_calling=True, parallel_tool_calls=False)

    def stream(self, context: object, settings: object = None):
        turn = self.turns[min(self.index, len(self.turns) - 1)]
        self.index += 1
        self.started += 1
        return self._stream_turn(turn)

    async def _stream_turn(self, turn: Turn):
        yield ResponseStarted("resp")
        sequence = 1
        if turn.delay:
            await asyncio.sleep(turn.delay)
        for part in _split(turn.text, turn.chunks):
            if turn.delay:
                await asyncio.sleep(turn.delay)
            yield TextDelta(sequence, part)
            sequence += 1
        if turn.fail is not None:
            raise RuntimeError(turn.fail)
        for index, call in enumerate(turn.tool_calls):
            yield ToolCallStarted(sequence, index, call.id, call.name)
            sequence += 1
            yield ToolCallCompleted(sequence, index, call)
            sequence += 1
        if turn.usage is not None:
            yield UsageUpdated(sequence, turn.usage)
            sequence += 1
        finish = FinishReason.TOOL_CALL if turn.tool_calls else FinishReason.STOP
        message = AssistantMessage(
            (TextContent(turn.text),) if turn.text else (), turn.tool_calls
        )
        yield ResponseCompleted(sequence, ModelResponse(message, finish, turn.usage))


class RequireApprovalPolicy:
    async def evaluate(self, call: ToolCall, tool: Tool | None, context: object):
        if tool is not None and tool.effect_kind is ToolEffectKind.SIDE_EFFECTING:
            return ToolPolicyDecision(ToolDecision.REQUIRE_APPROVAL, "side effecting")
        return ToolPolicyDecision(ToolDecision.ALLOW)


def make_fake_tool(name: str = "fake_tool") -> Tool:
    definition = ToolDefinition(
        name,
        "A fake side-effecting tool used by roserver tests.",
        FrozenJsonObject(
            {
                "type": "object",
                "properties": {"value": {"type": "string"}},
                "additionalProperties": True,
            }
        ),
    )

    async def handler(arguments: object, context: object) -> ToolTextContent:
        return ToolTextContent("tool-ok")

    return Tool(
        definition,
        handler,
        execution_mode=ToolExecutionMode.SERIAL,
        effect_kind=ToolEffectKind.SIDE_EFFECTING,
    )


def fake_tool_call(call_id: str = "call_1", name: str = "fake_tool") -> ToolCall:
    return ToolCall(call_id, name, FrozenJsonObject({"value": "x"}))


@pytest.fixture
def settings_factory(tmp_path):
    def build(**overrides) -> Settings:
        base: dict[str, object] = {
            "data_dir": tmp_path,
            "approval_ttl": 30.0,
            "replay_retention": 300.0,
        }
        base.update(overrides)
        return Settings(**base)  # type: ignore[arg-type]

    return build


class LiveWebSocket:
    """Small websocket surface compatible with the parts of TestClient we used.

    Starlette's ``WebSocketTestSession`` tears down the anyio portal while the
    application handler may still be running, which injects a ``CancelledError``
    into an unrelated test.  Real websocket clients do not share that teardown
    pathology, so the tests talk to a real socket instead.
    """

    def __init__(self, connection: Any) -> None:
        self._connection = connection

    def send_json(self, data: object) -> None:
        self._connection.send(json.dumps(data))

    def receive_json(self, timeout: float | None = None) -> Any:
        return json.loads(self._connection.recv(timeout=timeout))

    def close(self, code: int = 1000) -> None:
        self._connection.close(code)


class LiveWebSocketConnection:
    """Context manager returned by :meth:`LiveClient.websocket_connect`."""

    def __init__(
        self,
        uri: str,
        headers: dict[str, str] | None,
        subprotocols: Sequence[str] | None,
    ) -> None:
        self._uri = uri
        self._headers = headers
        self._subprotocols = subprotocols
        self._context: Any = None

    def __enter__(self) -> LiveWebSocket:
        origin = None
        extra_headers = None
        if self._headers:
            headers = dict(self._headers)
            origin = headers.pop("Origin", None)
            extra_headers = headers or None
        self._context = websocket_connect(
            self._uri,
            origin=origin,
            subprotocols=self._subprotocols,
            additional_headers=extra_headers,
            proxy=None,
            open_timeout=10.0,
            close_timeout=5.0,
        )
        return LiveWebSocket(self._context.__enter__())

    def __exit__(self, *exc_info: object) -> bool:
        if self._context is not None:
            return bool(self._context.__exit__(*exc_info))
        return False


class LiveClient:
    """Synchronous HTTP + websocket client pointed at a real uvicorn server."""

    def __init__(self, base_url: str) -> None:
        self.base_url = base_url
        self._http = httpx.Client(
            base_url=base_url,
            timeout=30.0,
            follow_redirects=True,
            trust_env=False,
        )

    def get(self, path: str, **kwargs: Any) -> httpx.Response:
        return self._http.get(path, **kwargs)

    def post(self, path: str, **kwargs: Any) -> httpx.Response:
        return self._http.post(path, **kwargs)

    def patch(self, path: str, **kwargs: Any) -> httpx.Response:
        return self._http.patch(path, **kwargs)

    def delete(self, path: str, **kwargs: Any) -> httpx.Response:
        return self._http.delete(path, **kwargs)

    def websocket_connect(
        self,
        path: str,
        headers: dict[str, str] | None = None,
        subprotocols: Sequence[str] | None = None,
        **kwargs: Any,
    ) -> LiveWebSocketConnection:
        del kwargs  # only the options used by the suite are supported
        ws_base = self.base_url.replace("http://", "ws://", 1)
        return LiveWebSocketConnection(ws_base + path, headers, subprotocols)

    def close(self) -> None:
        self._http.close()


class LiveServer:
    """A real uvicorn server bound to an ephemeral loopback port.

    ``port=0`` lets the OS pick a free port, so concurrent workstreams and
    repeated runs can never collide on a fixed port.  The server runs in a
    daemon thread; teardown joins that thread so an uvicorn process or thread
    cannot leak between tests.
    """

    STARTUP_TIMEOUT = 15.0
    SHUTDOWN_TIMEOUT = 10.0

    def __init__(self, app: object) -> None:
        self._app = app
        self._server: uvicorn.Server | None = None
        self._thread: threading.Thread | None = None
        self.port: int | None = None
        self.base_url: str | None = None

    def __enter__(self) -> LiveClient:
        self.start()
        assert self.base_url is not None
        self._client = LiveClient(self.base_url)
        return self._client

    def __exit__(self, *exc_info: object) -> bool:
        self._client.close()
        self.stop()
        return False

    def stop(self, *, graceful: bool = True) -> None:
        """Stop the server; ``graceful=False`` simulates a hard process crash.

        A graceful stop runs the ASGI lifespan shutdown, which (among other
        things) cancels active Runs.  A crash must *not* run it, otherwise no
        nonterminal run or pending approval is left behind for the next
        process's startup reconciliation.  ``force_exit`` makes uvicorn skip
        ``lifespan.shutdown()`` exactly like a killed process.
        """
        if self._server is not None:
            if not graceful:
                self._server.force_exit = True
            self._server.should_exit = True
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=self.SHUTDOWN_TIMEOUT)
        if thread is not None and thread.is_alive():
            if self._server is not None:
                self._server.force_exit = True
            thread.join(timeout=5.0)
        if thread is not None and thread.is_alive():
            raise RuntimeError(
                "uvicorn test server thread did not stop; refusing to leak it"
            )

    def start(self) -> None:
        config = uvicorn.Config(
            self._app,
            host="127.0.0.1",
            port=0,
            log_level="warning",
            access_log=False,
            lifespan="on",
        )
        self._server = uvicorn.Server(config)
        self._thread = threading.Thread(
            target=self._server.run, name="roserver-test-uvicorn", daemon=True
        )
        self._thread.start()
        deadline = time.time() + self.STARTUP_TIMEOUT
        while time.time() < deadline:
            if self._server.started:
                break
            if not self._thread.is_alive():
                raise RuntimeError(
                    "uvicorn test server thread exited before becoming ready"
                )
            time.sleep(0.005)
        else:
            self.stop()
            raise RuntimeError(
                f"uvicorn test server did not start within {self.STARTUP_TIMEOUT:.0f}s"
            )
        assert self._server.servers, "uvicorn did not create a listener"
        self.port = self._server.servers[0].sockets[0].getsockname()[1]
        self.base_url = f"http://127.0.0.1:{self.port}"
        self._wait_for_health(deadline)

    def _wait_for_health(self, deadline: float) -> None:
        last_error = "no response"
        while time.time() < deadline:
            try:
                response = httpx.get(
                    f"{self.base_url}/api/v1/health",
                    timeout=1.0,
                    trust_env=False,
                )
            except Exception as exc:  # noqa: BLE001 - reported below
                last_error = repr(exc)
            else:
                if response.status_code == 200:
                    return
                last_error = f"HTTP {response.status_code}"
            time.sleep(0.01)
        self.stop()
        raise RuntimeError(
            f"live server at {self.base_url} never answered /api/v1/health: "
            f"{last_error}"
        )


@pytest.fixture
def live_server():
    """Factory starting a real uvicorn server built from ``create_app``.

    ``crash_on_exit=True`` stops the server without running the ASGI lifespan
    shutdown, which lets a test reproduce process-crash recovery explicitly.
    """

    @contextmanager
    def build(
        settings: Settings,
        *,
        model: object,
        tools: Sequence[Tool] = (),
        tool_policy: object = None,
        robot_backend: object = None,
        media_engine: object = None,
        crash_on_exit: bool = False,
    ) -> Iterator[LiveClient]:
        app = create_app(
            settings,
            model=model,
            tools=tools,
            tool_policy=tool_policy,
            robot_backend=robot_backend,  # type: ignore[arg-type]
            media_engine=media_engine,  # type: ignore[arg-type]
        )
        server = LiveServer(app)
        client = server.__enter__()
        try:
            yield client
        finally:
            client.close()
            server.stop(graceful=not crash_on_exit)

    return build


@pytest.fixture
def client_factory(live_server):
    """Backwards-compatible alias for the live-server factory."""
    return live_server


class Api:
    """Small synchronous helpers built on the real Product HTTP surface."""

    def create_session(self, client: LiveClient, title: str | None = None) -> dict:
        response = client.post("/api/v1/sessions", json={"title": title} if title else {})
        assert response.status_code == 201, response.text
        return response.json()

    def start_run(
        self,
        client: LiveClient,
        session_id: str,
        text: str = "hello",
        *,
        idempotency_key: str | None = None,
        content: list | None = None,
    ):
        headers = {"Content-Type": "application/json"}
        if idempotency_key:
            headers["Idempotency-Key"] = idempotency_key
        body = {
            "input": {
                "client_message_id": "client_1",
                "content": content if content is not None else [{"type": "text", "text": text}],
            }
        }
        return client.post(
            f"/api/v1/sessions/{session_id}/runs", json=body, headers=headers
        )

    def wait_terminal(self, client: LiveClient, run_id: str, timeout: float = 8.0) -> dict:
        deadline = time.time() + timeout
        last: dict = {}
        while time.time() < deadline:
            response = client.get(f"/api/v1/runs/{run_id}")
            assert response.status_code == 200, response.text
            last = response.json()
            if last["status"] in {"completed", "failed", "cancelled", "interrupted"}:
                return last
            time.sleep(0.01)
        raise AssertionError(f"run {run_id} did not terminate: {last}")

    def wait_approval(
        self, client: LiveClient, run_id: str, timeout: float = 5.0
    ) -> dict:
        deadline = time.time() + timeout
        last: dict = {}
        while time.time() < deadline:
            response = client.get(f"/api/v1/runs/{run_id}/projection")
            if response.status_code == 200:
                last = response.json()
                approvals = last.get("approvals") or []
                if approvals:
                    return approvals[0]
            time.sleep(0.01)
        raise AssertionError(f"no approval requested for {run_id}: {last}")

    def collect_events(
        self,
        client: LiveClient,
        run_id: str,
        *,
        after_sequence: int | None = None,
        stop_type: str = "run.completed",
        timeout: float = 5.0,
        limit: int = 200,
    ) -> list[dict]:
        suffix = "" if after_sequence is None else f"?after_sequence={after_sequence}"
        events: list[dict] = []
        deadline = time.time() + timeout
        with client.websocket_connect(f"/api/v1/runs/{run_id}/events{suffix}") as ws:
            while time.time() < deadline and len(events) < limit:
                try:
                    event = ws.receive_json(timeout=max(0.01, deadline - time.time()))
                except BaseException:
                    # Socket closed (or the deadline elapsed) before the stop
                    # type arrived: return what we saw.
                    break
                events.append(event)
                if event.get("type") == stop_type:
                    break
        return events


@pytest.fixture
def api() -> Api:
    return Api()
