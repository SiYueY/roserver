"""FastAPI application factory and lifespan."""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from typing import AsyncIterator, Callable, Sequence
from uuid import uuid4

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.cors import CORSMiddleware
from starlette.responses import JSONResponse

from roboagent.agent import LocalSessionRepository
from roboagent.context import ContextManager
from roboagent.model import Model
from roboagent.tool import (
    Tool,
    ToolExecutionPolicy,
    WorkspaceArtifactDestination,
    WorkspaceArtifactReader,
    WorkspaceToolResultMaterializer,
)

from . import __version__
from .agent.api import router as agent_router
from .agent.approval import ProductApprovalProvider
from .agent.factory import AgentFactory, ModelFactory
from .agent.policy import RequireApprovalForSideEffects
from .agent.service import AgentService
from .artifact.adapters import ServiceMediaResolver
from .artifact.api import router as artifact_router
from .artifact.service import ArtifactService
from .artifact.workspace import ArtifactWorkspace
from .config import Settings
from .errors import ProductError, error_body
from .media.api import router as media_router
from .media.engine import MediaEngine, SimulatedMediaEngine
from .media.service import MediaService
from .robot.api import router as robot_router
from .robot.backend import RobotBackend, SimulatedRobotBackend
from .robot.service import RobotService
from .robot.tools import robot_tools
from .speech.api import router as speech_router
from .speech.engine import SimulatedSpeechEngine, SpeechEngine
from .speech.service import SpeechService
from .store.application import ApplicationStore
from .system.api import router as system_router


logger = logging.getLogger("roserver")


def create_app(
    settings: Settings | None = None,
    *,
    model: Model | None = None,
    model_factory: ModelFactory | None = None,
    tools: Sequence[Tool] = (),
    tool_policy: ToolExecutionPolicy | None = None,
    context_manager: ContextManager | None = None,
    robot_backend: RobotBackend | None = None,
    media_engine: MediaEngine | None = None,
    speech_engine: SpeechEngine | None = None,
) -> FastAPI:
    resolved = settings or Settings.from_env()
    store = ApplicationStore(resolved.resolved_db_path)
    repository = LocalSessionRepository(resolved.resolved_sessions_dir)
    approval_provider = ProductApprovalProvider(
        store, owner_id=resolved.owner_id, ttl=resolved.approval_ttl
    )
    if robot_backend is not None:
        backend = robot_backend
    elif resolved.robot_backend == "dclpy":
        from .robot.dclpy_backend import DclpyRobotBackend
        backend = DclpyRobotBackend(resolved)
    else:
        backend = SimulatedRobotBackend()
    robot = RobotService(settings=resolved, backend=backend, store=store)
    # Phase 3B: the Agent Tool path uses the same RobotService as the manual UI.
    factory = AgentFactory(
        settings=resolved,
        approval_provider=approval_provider,
        model=model,
        model_factory=model_factory,
        tools=(*tools, *robot_tools(robot, resolved)),
        tool_policy=tool_policy or RequireApprovalForSideEffects(),
    )
    artifacts = ArtifactService(store, resolved)
    workspace = ArtifactWorkspace(artifacts)
    agent = factory.build_agent()
    # Robot camera frames are materialized as workspace artifacts. Bind both
    # readers after the durable workspace exists so provider models can send
    # them as image inputs on the following turn.
    model_instance = agent.model
    if hasattr(model_instance, "artifact_reader"):
        model_instance.artifact_reader = WorkspaceArtifactReader(workspace)
    if hasattr(model_instance, "media_resolver"):
        model_instance.media_resolver = ServiceMediaResolver(artifacts)
    service = AgentService(
        settings=resolved,
        agent=agent,
        repository=repository,
        store=store,
        approval_provider=approval_provider,
        artifacts=artifacts,
        artifact_reader=WorkspaceArtifactReader(workspace),
        artifact_destination=WorkspaceArtifactDestination(workspace),
        media_resolver=ServiceMediaResolver(artifacts),
        workspace=workspace,
        result_materializer=WorkspaceToolResultMaterializer(workspace=workspace),
    )
    if media_engine is not None:
        engine = media_engine
    elif callable(getattr(backend, "camera_frame", None)):
        from .media.dclpy_engine import DclpyMediaEngine
        engine = DclpyMediaEngine(backend)
    else:
        engine = SimulatedMediaEngine()
    media = MediaService(settings=resolved, engine=engine)
    set_disconnect = getattr(engine, "set_disconnect_handler", None)
    if set_disconnect is not None:
        set_disconnect(media.on_client_disconnect)
    # Phase 5B: the speech bridge is likewise injectable and simulation-only.
    speech_engine_resolved: SpeechEngine = speech_engine or SimulatedSpeechEngine(
        max_audio_frames=resolved.speech_max_audio_frames,
        event_queue_bound=resolved.speech_event_queue_bound,
    )
    speech = SpeechService(
        settings=resolved, engine=speech_engine_resolved, media=media
    )

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        await service.startup()
        # The robot layer must never block core features
        # (agent / session / run / artifact).  A robot-side failure is logged and
        # isolated instead of aborting application startup.
        robot_ready = False
        try:
            await robot.startup()
            robot_ready = True
        except Exception:  # pragma: no cover - defensive isolation
            logger.exception(
                "robot backend failed to start; continuing without it"
            )
        # The media layer (Phase 5A) is likewise isolated: signaling failures
        # must not take down agent / session / run / artifact.
        media_ready = False
        try:
            await media.startup()
            media_ready = True
        except Exception:  # pragma: no cover - defensive isolation
            logger.exception(
                "media layer failed to start; continuing without it"
            )
        # The speech bridge (Phase 5B) is isolated the same way: a speech
        # failure must never block health / session / run / artifact.
        speech_ready = False
        try:
            await speech.startup()
            speech_ready = True
        except Exception:  # pragma: no cover - defensive isolation
            logger.exception(
                "speech bridge failed to start; continuing without it"
            )
        try:
            yield
        finally:
            # Settle Agent tools while their robot/media resources still exist.
            await service.shutdown(close_store=False)
            if speech_ready:
                try:
                    await speech.close()
                except Exception:  # pragma: no cover - defensive isolation
                    logger.exception("speech bridge failed to shut down cleanly")
            if media_ready:
                try:
                    await media.close()
                except Exception:  # pragma: no cover - defensive isolation
                    logger.exception("media layer failed to shut down cleanly")
            if robot_ready or resolved.robot_backend == "dclpy":
                try:
                    await robot.shutdown()
                except Exception:  # pragma: no cover - defensive isolation
                    logger.exception("robot backend failed to shut down cleanly")
            await store.close()

    app = FastAPI(title="roserver", version=__version__, lifespan=lifespan)
    app.state.settings = resolved
    app.state.service = service
    app.state.factory = factory
    app.state.artifacts = artifacts
    app.state.robot = robot
    app.state.robot_backend = backend
    app.state.media = media
    app.state.media_engine = engine
    app.state.speech = speech
    app.state.speech_engine = speech_engine_resolved

    if resolved.cors_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=list(resolved.cors_origins),
            allow_credentials=False,
            allow_methods=["GET", "POST", "PATCH", "DELETE", "OPTIONS"],
            allow_headers=["Content-Type", "Idempotency-Key", "X-Request-Id"],
            expose_headers=["Idempotency-Replayed", "X-Request-Id"],
        )

    @app.middleware("http")
    async def request_id_middleware(request: Request, call_next: Callable):
        request.state.request_id = request.headers.get("x-request-id") or uuid4().hex
        response = await call_next(request)
        response.headers.setdefault("X-Request-Id", request.state.request_id)
        return response

    _register_error_handlers(app)
    app.include_router(system_router)
    app.include_router(agent_router)
    app.include_router(artifact_router)
    app.include_router(robot_router)
    app.include_router(media_router)
    app.include_router(speech_router)
    return app


def _register_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(ProductError)
    async def product_error_handler(request: Request, exc: ProductError) -> JSONResponse:
        rid = getattr(request.state, "request_id", "")
        return JSONResponse(status_code=exc.status, content=exc.to_body(rid))

    @app.exception_handler(RequestValidationError)
    async def validation_error_handler(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        rid = getattr(request.state, "request_id", "")
        return JSONResponse(
            status_code=400,
            content=error_body("invalid_input", "Request validation failed.", rid),
        )

    @app.exception_handler(StarletteHTTPException)
    async def http_error_handler(
        request: Request, exc: StarletteHTTPException
    ) -> JSONResponse:
        rid = getattr(request.state, "request_id", "")
        code = {
            404: "invalid_input",
            405: "invalid_input",
            413: "payload_too_large",
            415: "unsupported_media_type",
            422: "invalid_input",
        }.get(exc.status_code, "internal_error")
        message = "Request could not be completed." if exc.status_code >= 500 else "Not found."
        return JSONResponse(
            status_code=exc.status_code,
            content=error_body(code, message, rid),
        )

    @app.exception_handler(Exception)
    async def unhandled_error_handler(request: Request, exc: Exception) -> JSONResponse:
        rid = getattr(request.state, "request_id", "")
        return JSONResponse(
            status_code=500,
            content=error_body("internal_error", "Unexpected internal error.", rid),
        )


def app_factory() -> FastAPI:
    """Entrypoint for ``uvicorn roserver.app:app_factory --factory --workers 1``."""
    return create_app()


__all__ = ["create_app", "app_factory"]
