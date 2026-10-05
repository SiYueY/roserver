"""Environment-driven roserver configuration.

V1 deployment contract (see docs/roserver.md §1.7):
    ONE roserver process
    ONE asyncio event loop
    ONE uvicorn worker

The process cannot mechanically detect a ``--workers N`` misconfiguration from
inside the worker, so this module documents the contract and exposes
:meth:`Settings.worker_warning` for entrypoints that want to warn.  It never
performs runtime "hacks" to enforce it.
"""

from __future__ import annotations

import os
import math
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_OWNER_ID = "local"


def _env(name: str, default: str | None = None) -> str | None:
    value = os.environ.get(name)
    return default if value is None or value == "" else value


def _env_int(name: str, default: int) -> int:
    raw = _env(name)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError as exc:  # pragma: no cover - defensive
        raise ValueError(f"{name} must be an integer.") from exc


def _env_float(name: str, default: float) -> float:
    raw = _env(name)
    if raw is None:
        return default
    try:
        return float(raw)
    except ValueError as exc:  # pragma: no cover - defensive
        raise ValueError(f"{name} must be a number.") from exc


def _env_bool(name: str, default: bool) -> bool:
    raw = _env(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _env_list(name: str) -> tuple[str, ...]:
    raw = _env(name)
    if raw is None:
        return ()
    return tuple(item.strip() for item in raw.split(",") if item.strip())


@dataclass(frozen=True, slots=True)
class Settings:
    """Immutable runtime settings for one roserver process."""

    host: str = "127.0.0.1"
    port: int = 8765
    owner_id: str = DEFAULT_OWNER_ID

    data_dir: Path = Path("./data")
    db_path: Path | None = None
    sessions_dir: Path | None = None
    artifacts_dir: Path | None = None

    cors_origins: tuple[str, ...] = ()
    ws_origin_validation: bool = True
    ws_allowed_origins: tuple[str, ...] = ()

    max_agent_input_bytes: int = 256 * 1024
    max_content_blocks: int = 32
    max_artifact_bytes: int = 8 * 1024 * 1024
    max_pending_inputs: int = 32
    max_pending_input_bytes: int = 256 * 1024

    idempotency_retention: float = 24 * 60 * 60.0
    approval_ttl: float = 300.0

    max_replay_events: int = 1000
    max_replay_bytes: int = 4 * 1024 * 1024
    replay_retention: float = 300.0

    cancel_settle_timeout: float = 5.0

    # Phase 3A robot backend / teleoperation limits (docs §4.6-§4.9).
    robot_authority_ttl: float = 30.0
    robot_watchdog_timeout: float = 0.5
    robot_watchdog_tick: float = 0.02
    robot_watch_interval: float = 0.05
    robot_stale_command_seconds: float = 5.0
    robot_rate_limit: int = 100
    robot_max_linear_velocity: float = 1.0
    robot_max_angular_velocity: float = 1.0
    robot_backend: str = "simulated"
    robot_id: str = "robot_1"
    robot_namespace: str = "/"
    robot_domain_id: int = 0
    robot_startup_timeout: float = 20.0
    robot_state_timeout: float = 1.0
    robot_operation_timeout: float = 180.0
    robot_terminal_timeout: float = 15.0
    robot_camera_enabled: bool = True

    # Phase 5A media / WebRTC signaling limits (docs §5.4-§5.6).
    media_session_ttl: float = 600.0
    media_max_sdp_bytes: int = 256 * 1024
    media_expiry_tick: float = 0.5
    media_tombstone_ttl: float = 3600.0

    # Phase 5B speech / PCM bridge limits (docs §5.7).
    # ``speech_max_audio_frame_bytes`` bounds one client -> server audio frame;
    # ``speech_max_audio_frames`` and ``speech_event_queue_bound`` bound the
    # engine-side capture and event queues so a stalled client cannot grow
    # memory without limit.
    speech_max_audio_frame_bytes: int = 64 * 1024
    speech_max_audio_frames: int = 32
    speech_event_queue_bound: int = 256

    model: str = ""
    model_config_path: Path | None = None
    model_name: str = ""
    system_prompt: str = ""

    extra: dict[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.robot_backend not in {"simulated", "dclpy"}:
            raise ValueError("robot_backend must be simulated or dclpy.")
        if not self.robot_id or not self.robot_namespace.startswith("/"):
            raise ValueError("robot_id must be non-empty and robot_namespace absolute.")
        if type(self.robot_domain_id) is not int or not 0 <= self.robot_domain_id <= 232:
            raise ValueError("robot_domain_id must be in 0..232.")
        if not isinstance(self.host, str) or not self.host:
            raise ValueError("host must be non-empty.")
        if not 1 <= self.port <= 65535:
            raise ValueError("port must be in 1..65535.")
        for name in (
            "max_agent_input_bytes",
            "max_content_blocks",
            "max_artifact_bytes",
            "max_pending_inputs",
            "max_pending_input_bytes",
            "max_replay_events",
            "max_replay_bytes",
            "robot_rate_limit",
            "media_max_sdp_bytes",
            "speech_max_audio_frame_bytes",
            "speech_max_audio_frames",
            "speech_event_queue_bound",
        ):
            value = getattr(self, name)
            if not isinstance(value, int) or isinstance(value, bool) or value < 1:
                raise ValueError(f"{name} must be a positive integer.")
        for name in (
            "idempotency_retention",
            "approval_ttl",
            "replay_retention",
            "cancel_settle_timeout",
            "robot_authority_ttl",
            "robot_watchdog_timeout",
            "robot_watchdog_tick",
            "robot_watch_interval",
            "robot_stale_command_seconds",
            "robot_max_linear_velocity",
            "robot_max_angular_velocity",
            "robot_startup_timeout",
            "robot_state_timeout",
            "robot_operation_timeout",
            "robot_terminal_timeout",
            "media_session_ttl",
            "media_expiry_tick",
            "media_tombstone_ttl",
        ):
            value = getattr(self, name)
            if not isinstance(value, (int, float)) or isinstance(value, bool) or not math.isfinite(value) or value <= 0:
                raise ValueError(f"{name} must be positive.")

    # -- derived paths -------------------------------------------------
    @property
    def resolved_data_dir(self) -> Path:
        return Path(self.data_dir).expanduser().resolve()

    @property
    def resolved_db_path(self) -> Path:
        if self.db_path is not None:
            return Path(self.db_path).expanduser().resolve()
        return self.resolved_data_dir / "application.db"

    @property
    def resolved_sessions_dir(self) -> Path:
        if self.sessions_dir is not None:
            return Path(self.sessions_dir).expanduser().resolve()
        return self.resolved_data_dir / "sessions"

    @property
    def resolved_artifacts_dir(self) -> Path:
        """Digest-addressed artifact blob root (docs §5.1)."""
        if self.artifacts_dir is not None:
            return Path(self.artifacts_dir).expanduser().resolve()
        return self.resolved_data_dir / "artifacts"

    @property
    def resolved_ws_origins(self) -> tuple[str, ...]:
        if self.ws_allowed_origins:
            return self.ws_allowed_origins
        return self.cors_origins

    def worker_warning(self, workers: int) -> str | None:
        if workers != 1:
            return (
                "roserver requires exactly one uvicorn worker: roboagent uses a "
                "same-event-loop contract and the in-process RunProjection / "
                "PublicEventBuffer are not shared across processes."
            )
        return None

    @classmethod
    def from_env(cls, environ: dict[str, str] | None = None) -> "Settings":
        env = os.environ if environ is None else environ

        def get(name: str, default: str | None = None) -> str | None:
            value = env.get(name)
            return default if value is None or value == "" else value

        def get_int(name: str, default: int) -> int:
            raw = get(name)
            return default if raw is None else int(raw)

        def get_float(name: str, default: float) -> float:
            raw = get(name)
            return default if raw is None else float(raw)

        def get_bool(name: str, default: bool) -> bool:
            raw = get(name)
            if raw is None:
                return default
            return raw.strip().lower() in {"1", "true", "yes", "on"}

        def get_list(name: str) -> tuple[str, ...]:
            raw = get(name)
            if raw is None:
                return ()
            return tuple(item.strip() for item in raw.split(",") if item.strip())

        data_dir = Path(get("ROSERVER_DATA_DIR", "./data") or "./data")
        db_path = get("ROSERVER_DB_PATH")
        sessions_dir = get("ROSERVER_SESSIONS_DIR")
        model_config = get("ROSERVER_MODEL_CONFIG")
        return cls(
            host=get("ROSERVER_HOST", "127.0.0.1") or "127.0.0.1",
            port=get_int("ROSERVER_PORT", 8765),
            owner_id=get("ROSERVER_OWNER_ID", DEFAULT_OWNER_ID) or DEFAULT_OWNER_ID,
            data_dir=data_dir,
            db_path=Path(db_path) if db_path else None,
            sessions_dir=Path(sessions_dir) if sessions_dir else None,
            cors_origins=get_list("ROSERVER_CORS_ORIGINS"),
            ws_origin_validation=get_bool("ROSERVER_WS_ORIGIN_VALIDATION", True),
            ws_allowed_origins=get_list("ROSERVER_WS_ALLOWED_ORIGINS"),
            max_agent_input_bytes=get_int("ROSERVER_MAX_AGENT_INPUT_BYTES", 256 * 1024),
            max_content_blocks=get_int("ROSERVER_MAX_CONTENT_BLOCKS", 32),
            max_artifact_bytes=get_int("ROSERVER_MAX_ARTIFACT_BYTES", 8 * 1024 * 1024),
            max_pending_inputs=get_int("ROSERVER_MAX_PENDING_INPUTS", 32),
            max_pending_input_bytes=get_int("ROSERVER_MAX_PENDING_INPUT_BYTES", 256 * 1024),
            idempotency_retention=get_float("ROSERVER_IDEMPOTENCY_RETENTION", 24 * 60 * 60.0),
            approval_ttl=get_float("ROSERVER_APPROVAL_TTL", 300.0),
            max_replay_events=get_int("ROSERVER_MAX_REPLAY_EVENTS", 1000),
            max_replay_bytes=get_int("ROSERVER_MAX_REPLAY_BYTES", 4 * 1024 * 1024),
            replay_retention=get_float("ROSERVER_REPLAY_RETENTION", 300.0),
            cancel_settle_timeout=get_float("ROSERVER_CANCEL_SETTLE_TIMEOUT", 5.0),
            robot_authority_ttl=get_float("ROSERVER_ROBOT_AUTHORITY_TTL", 30.0),
            robot_watchdog_timeout=get_float("ROSERVER_ROBOT_WATCHDOG_TIMEOUT", 0.5),
            robot_watchdog_tick=get_float("ROSERVER_ROBOT_WATCHDOG_TICK", 0.02),
            robot_watch_interval=get_float("ROSERVER_ROBOT_WATCH_INTERVAL", 0.05),
            robot_stale_command_seconds=get_float(
                "ROSERVER_ROBOT_STALE_COMMAND_SECONDS", 5.0
            ),
            robot_rate_limit=get_int("ROSERVER_ROBOT_RATE_LIMIT", 100),
            robot_backend=get("ROSERVER_ROBOT_BACKEND", "simulated") or "simulated",
            robot_id=get("ROSERVER_ROBOT_ID", "robot_1") or "robot_1",
            robot_namespace=get("ROSERVER_ROBOT_NAMESPACE", "/") or "/",
            robot_domain_id=get_int("ROSERVER_ROBOT_DOMAIN_ID", get_int("ROS_DOMAIN_ID", 0)),
            robot_startup_timeout=get_float("ROSERVER_ROBOT_STARTUP_TIMEOUT", 20.0),
            robot_state_timeout=get_float("ROSERVER_ROBOT_STATE_TIMEOUT", 1.0),
            robot_operation_timeout=get_float("ROSERVER_ROBOT_OPERATION_TIMEOUT", 180.0),
            robot_terminal_timeout=get_float("ROSERVER_ROBOT_TERMINAL_TIMEOUT", 15.0),
            robot_camera_enabled=get_bool("ROSERVER_ROBOT_CAMERA_ENABLED", True),
            robot_max_linear_velocity=get_float(
                "ROSERVER_ROBOT_MAX_LINEAR_VELOCITY", 1.0
            ),
            robot_max_angular_velocity=get_float(
                "ROSERVER_ROBOT_MAX_ANGULAR_VELOCITY", 1.0
            ),
            media_session_ttl=get_float("ROSERVER_MEDIA_SESSION_TTL", 600.0),
            media_max_sdp_bytes=get_int(
                "ROSERVER_MEDIA_MAX_SDP_BYTES", 256 * 1024
            ),
            media_expiry_tick=get_float("ROSERVER_MEDIA_EXPIRY_TICK", 0.5),
            media_tombstone_ttl=get_float(
                "ROSERVER_MEDIA_TOMBSTONE_TTL", 3600.0
            ),
            speech_max_audio_frame_bytes=get_int(
                "ROSERVER_SPEECH_MAX_AUDIO_FRAME_BYTES", 64 * 1024
            ),
            speech_max_audio_frames=get_int(
                "ROSERVER_SPEECH_MAX_AUDIO_FRAMES", 32
            ),
            speech_event_queue_bound=get_int(
                "ROSERVER_SPEECH_EVENT_QUEUE_BOUND", 256
            ),
            model=get("ROSERVER_MODEL", "") or "",
            model_config_path=Path(model_config) if model_config else None,
            model_name=get("ROSERVER_MODEL_NAME", "") or "",
            system_prompt=get("ROSERVER_SYSTEM_PROMPT", "") or "",
        )


__all__ = ["DEFAULT_OWNER_ID", "Settings"]
