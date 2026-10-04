"""Product object builders: runtime/canonical objects -> snake_case wire dicts.

Docs §2.8 (snapshot conversion), §2.9 (Product Message), §2.13 (RunStatus),
§3.11 (ApprovalInfo), §3.15 (RunInfo / Usage / EffectSummary).
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Mapping

from roboagent.message import (
    AgentMessage,
    ArtifactReferenceContent,
    AssistantMessage,
    AudioContent,
    FileContent,
    ImageContent,
    JsonContent,
    TextContent,
    ToolResultMessage,
    ToolResultStatus,
    UserMessage,
    thaw_json,
)
from roboagent.tool import EffectCertainty, ToolEffectStatus

from ..errors import error_detail, map_runtime_error_code, public_message_for
from ..store.application import decode_effects

# Product RunStatus (docs §2.13).
RUN_STATUS_CREATED = "created"
RUN_STATUS_RUNNING = "running"
RUN_STATUS_COMPLETED = "completed"
RUN_STATUS_FAILED = "failed"
RUN_STATUS_CANCELLED = "cancelled"
RUN_STATUS_INTERRUPTED = "interrupted"

TERMINAL_RUN_STATUSES = frozenset(
    {
        RUN_STATUS_COMPLETED,
        RUN_STATUS_FAILED,
        RUN_STATUS_CANCELLED,
        RUN_STATUS_INTERRUPTED,
    }
)

TOOL_EXECUTION_STATUSES = frozenset(
    {"pending", "running", "completed", "failed", "cancelled"}
)


def rfc3339(timestamp: float | None) -> str | None:
    """Runtime float epoch seconds -> RFC 3339 UTC with millisecond precision."""
    if timestamp is None:
        return None
    moment = datetime.fromtimestamp(float(timestamp), tz=timezone.utc)
    return moment.isoformat(timespec="milliseconds").replace("+00:00", "Z")


# -- content ---------------------------------------------------------------
def content_block(content: object) -> dict[str, Any]:
    if isinstance(content, TextContent):
        return {"type": "text", "text": content.text}
    if isinstance(content, JsonContent):
        return {"type": "json", "value": thaw_json(content.value)}
    if isinstance(content, ArtifactReferenceContent):
        artifact_id = content.digest
        media_type = content.media_type
        if not media_type:
            # Unknown media type: use the lossless generic branch (docs §2.8).
            return {
                "type": "artifact_reference",
                "artifact_id": artifact_id,
                "media_type": None,
                "size": content.size,
                "digest": content.digest,
                "preview": content.preview,
            }
        if media_type.startswith("image/"):
            return {
                "type": "image",
                "artifact_id": artifact_id,
                "media_type": media_type,
                "detail": None,
            }
        if media_type.startswith("audio/"):
            return {
                "type": "audio",
                "artifact_id": artifact_id,
                "media_type": media_type,
                "transcript": None,
            }
        return {
            "type": "file",
            "artifact_id": artifact_id,
            "media_type": media_type,
            "filename": None,
        }
    if isinstance(content, ImageContent):
        return _unsupported_media("image")
    if isinstance(content, AudioContent):
        return _unsupported_media("audio")
    if isinstance(content, FileContent):
        return _unsupported_media("file")
    raise TypeError(f"Unknown canonical content type: {type(content).__name__}")


def _unsupported_media(kind: str) -> dict[str, Any]:
    # Inline runtime media must be materialized into the ArtifactService by the
    # injected ArtifactDestination before projection.  Emitting a Product branch
    # without a real artifact id would be lossy, so fail loudly instead.
    raise ValueError(
        f"Product snapshot cannot project inline {kind} content; "
        "it must be materialized as an artifact first."
    )


# -- messages --------------------------------------------------------------
def product_message(message: AgentMessage) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "message_id": message.message_id,
        "role": message.role,
        "timestamp": rfc3339(message.timestamp),
        "content": [content_block(item) for item in message.content],
    }
    if isinstance(message, AssistantMessage):
        payload["tool_calls"] = [
            {
                "tool_call_id": call.id,
                "name": call.name,
                "arguments": thaw_json(call.arguments),
            }
            for call in message.tool_calls
        ]
    elif isinstance(message, ToolResultMessage):
        payload["tool_call_id"] = message.tool_call_id
        payload["tool_name"] = message.tool_name
        payload["status"] = message.status.value
        payload["error"] = _tool_error(message)
    elif isinstance(message, UserMessage):
        pass
    return payload


def _tool_error(message: ToolResultMessage) -> dict[str, Any] | None:
    if message.status is not ToolResultStatus.ERROR or message.error is None:
        return None
    raw_code = getattr(message.error, "code", None)
    code = map_runtime_error_code(raw_code)
    retryable = getattr(message.error, "retryable", None)
    return error_detail(code, public_message_for(code), bool(retryable))


# -- usage / effects -------------------------------------------------------
def usage_dict(usage: object | None, usage_known: bool | None) -> dict[str, Any]:
    input_tokens = getattr(usage, "input_tokens", None)
    output_tokens = getattr(usage, "output_tokens", None)
    total_tokens = getattr(usage, "total_tokens", None)
    if usage is None:
        return {
            "input_tokens": None,
            "output_tokens": None,
            "total_tokens": None,
            "usage_known": False,
        }
    known = bool(usage_known) and all(
        value is not None for value in (input_tokens, output_tokens, total_tokens)
    )
    return {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "total_tokens": total_tokens,
        "usage_known": known,
    }


def _enum_value(value: object) -> str | None:
    if value is None:
        return None
    return getattr(value, "value", str(value))


def effect_summary_text(effect_status: str, certainty: str) -> str:
    if effect_status == ToolEffectStatus.SUCCEEDED.value:
        return "Tool completed and its effect was confirmed."
    if effect_status == ToolEffectStatus.FAILED.value:
        return (
            "Tool failed before committing an effect."
            if certainty == EffectCertainty.CERTAIN_NO_EFFECT.value
            else "Tool failed with an uncertain effect."
        )
    if effect_status == ToolEffectStatus.TIMED_OUT.value:
        return "Tool timed out; its effect is uncertain."
    if effect_status == ToolEffectStatus.CANCELLED.value:
        return "Tool was cancelled before its effect was confirmed."
    return "Tool outcome could not be confirmed."


def effect_summary(effect: object) -> dict[str, Any]:
    status = _enum_value(getattr(effect, "status", None)) or ToolEffectStatus.UNKNOWN.value
    certainty = _enum_value(getattr(effect, "certainty", None)) or EffectCertainty.UNKNOWN.value
    return {
        "tool_call_id": getattr(effect, "call_id", ""),
        "tool_name": getattr(effect, "tool_name", ""),
        "effect_status": status,
        "certainty": certainty,
        "summary": effect_summary_text(status, certainty),
    }


def stored_effect_summary(effect: Mapping[str, Any]) -> dict[str, Any]:
    status = str(effect.get("effect_status") or ToolEffectStatus.UNKNOWN.value)
    certainty = str(effect.get("certainty") or EffectCertainty.UNKNOWN.value)
    return {
        "tool_call_id": effect.get("tool_call_id", ""),
        "tool_name": effect.get("tool_name", ""),
        "effect_status": status,
        "certainty": certainty,
        "summary": effect.get("summary")
        or effect_summary_text(status, certainty),
    }


# -- runs ------------------------------------------------------------------
def run_info(record: Mapping[str, Any]) -> dict[str, Any]:
    status = str(record.get("status"))
    error = None
    if record.get("error_code"):
        error = error_detail(
            str(record["error_code"]),
            str(record.get("error_message") or public_message_for(str(record["error_code"]))),
            None if record.get("error_retryable") is None else bool(record["error_retryable"]),
        )
    effects = [stored_effect_summary(item) for item in decode_effects(record.get("effects_json"))]
    return {
        "run_id": record.get("run_id"),
        "session_id": record.get("session_id"),
        "root_run_id": record.get("root_run_id"),
        "parent_run_id": record.get("parent_run_id"),
        "source_run_id": record.get("source_run_id"),
        "agent_tool_name": record.get("agent_tool_name"),
        "status": status,
        "created_at": rfc3339(record.get("created_at")),
        "started_at": rfc3339(record.get("started_at")),
        "ended_at": rfc3339(record.get("ended_at")),
        "error": error,
        "usage": {
            "input_tokens": record.get("input_tokens"),
            "output_tokens": record.get("output_tokens"),
            "total_tokens": record.get("total_tokens"),
            "usage_known": bool(record.get("usage_known")),
        },
        "effects": effects,
        "retry_safe": bool(record.get("retry_safe")),
    }


def session_summary(record: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "session_id": record.get("session_id"),
        "title": record.get("title"),
        "status": session_status(record),
        "active_root_run_id": record.get("active_root_run_id"),
        "created_at": rfc3339(record.get("created_at")),
        "updated_at": rfc3339(record.get("updated_at")),
    }


def session_status(record: Mapping[str, Any]) -> str:
    active = record.get("active_root_run_id")
    return "running" if active else "idle"


def active_run_summary(record: Mapping[str, Any] | None) -> dict[str, Any] | None:
    if record is None:
        return None
    return {
        "run_id": record.get("run_id"),
        "status": record.get("status"),
        "started_at": rfc3339(record.get("started_at")),
    }


def approval_info(record: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "approval_id": record.get("approval_id"),
        "session_id": record.get("session_id"),
        "root_run_id": record.get("root_run_id"),
        "source_run_id": record.get("source_run_id"),
        "tool_call_id": record.get("tool_call_id"),
        "tool_name": record.get("tool_name"),
        "summary": record.get("summary"),
        "arguments": _arguments(record),
        "arguments_digest": record.get("arguments_digest"),
        "effect_capability": record.get("effect_capability"),
        "status": record.get("status"),
        "decision": record.get("decision"),
        "expires_at": rfc3339(record.get("expires_at")),
        "resolved_at": rfc3339(record.get("resolved_at")),
    }


def _arguments(record: Mapping[str, Any]) -> object:
    value = record.get("arguments")
    if value is not None:
        return value
    raw = record.get("arguments_json")
    if raw is None:
        return {}
    if isinstance(raw, (dict, list)):
        return raw
    import json

    try:
        return json.loads(str(raw))
    except ValueError:  # pragma: no cover - defensive
        return {}


__all__ = [
    "RUN_STATUS_CANCELLED",
    "RUN_STATUS_COMPLETED",
    "RUN_STATUS_CREATED",
    "RUN_STATUS_FAILED",
    "RUN_STATUS_INTERRUPTED",
    "RUN_STATUS_RUNNING",
    "TERMINAL_RUN_STATUSES",
    "active_run_summary",
    "approval_info",
    "content_block",
    "effect_summary",
    "effect_summary_text",
    "product_message",
    "rfc3339",
    "run_info",
    "session_status",
    "session_summary",
    "usage_dict",
]
