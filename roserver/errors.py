"""Product error envelope and the closed §3.12 error-code registry."""

from __future__ import annotations

from typing import Any

# code -> HTTP status.  HTTP-less codes (RUN/STREAM/APPROVAL scope) map to 500
# when they accidentally surface over HTTP, but they are normally embedded in
# RunInfo.error / run.failed / approval terminal_reason instead.
PRODUCT_ERROR_STATUS: dict[str, int] = {
    "invalid_input": 400,
    "control_authority_required": 403,
    "session_not_found": 404,
    "run_not_found": 404,
    "approval_not_found": 404,
    "artifact_not_found": 404,
    "robot_not_found": 404,
    "media_session_not_found": 404,
    "projection_not_available": 404,
    "session_busy": 409,
    "session_deleting": 409,
    "idempotency_conflict": 409,
    "idempotency_in_progress": 409,
    "approval_already_resolved": 409,
    "approval_digest_mismatch": 409,
    "control_authority_conflict": 409,
    "robot_not_ready": 409,
    "robot_recovery_required": 409,
    "robot_execution_failed": 502,
    "robot_operation_not_found": 404,
    "artifact_in_use": 409,
    "media_session_closed": 409,
    "persistence_conflict": 409,
    "approval_expired": 410,
    "payload_too_large": 413,
    "unsupported_media_type": 415,
    "unsupported_content_type": 422,
    "pending_queue_full": 429,
    "rate_limited": 429,
    "internal_error": 500,
    "model_failed": 502,
    "media_negotiation_failed": 502,
    "provider_auth_failed": 503,
    "provider_unavailable": 503,
    "robot_unavailable": 503,
    "robot_timeout": 504,
    # RUN / STREAM / APPROVAL scoped codes (no canonical HTTP status).
    "run_timeout": 500,
    "runtime_error": 500,
    "run_cancelled": 500,
    "stream_resync_required": 500,
    "server_restarted": 500,
}

# Runtime / system error code -> Product error code (docs §3.12 / §7.9).
RUNTIME_ERROR_MAP: dict[str, str] = {
    "timeout": "run_timeout",
    "model_error": "model_failed",
    "session_persistence_error": "persistence_conflict",
    "session_conflict": "persistence_conflict",
    "provider_authentication_error": "provider_auth_failed",
    "provider_auth_error": "provider_auth_failed",
    "provider_connection_error": "provider_unavailable",
    "provider_http_error": "provider_unavailable",
    "provider_rate_limit": "provider_unavailable",
    "provider_timeout": "provider_unavailable",
    "run_cancelled": "run_cancelled",
}

_PUBLIC_MESSAGES: dict[str, str] = {
    "run_timeout": "Run exceeded its timeout.",
    "model_failed": "Model invocation failed.",
    "provider_auth_failed": "Model provider authentication failed.",
    "provider_unavailable": "Model provider is unavailable.",
    "persistence_conflict": "Session persistence conflict.",
    "run_cancelled": "Run was cancelled.",
    "runtime_error": "Run failed due to an internal runtime error.",
    "internal_error": "Unexpected internal error.",
}


def product_code_exists(code: str) -> bool:
    return code in PRODUCT_ERROR_STATUS


def map_runtime_error_code(code: str | None) -> str:
    """Map a roboagent Runtime error code onto the closed Product registry."""
    if not code:
        return "runtime_error"
    mapped = RUNTIME_ERROR_MAP.get(code)
    if mapped is not None:
        return mapped
    if product_code_exists(code):
        return code
    return "runtime_error"


def public_message_for(code: str) -> str:
    return _PUBLIC_MESSAGES.get(code, "Run failed.")


class ProductError(Exception):
    """A Product-protocol error carrying a registered code and HTTP status."""

    def __init__(
        self,
        code: str,
        message: str | None = None,
        *,
        status: int | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        if not product_code_exists(code):
            raise ValueError(f"Unregistered Product error code: {code!r}")
        self.code = code
        self.message = message or public_message_for(code)
        self.status = status if status is not None else PRODUCT_ERROR_STATUS[code]
        self.details = details or {}
        super().__init__(self.message)

    def to_body(self, request_id: str) -> dict[str, Any]:
        return {
            "error": {
                "code": self.code,
                "message": self.message,
                "request_id": request_id,
                "details": self.details,
            }
        }


def error_body(code: str, message: str, request_id: str, details: dict[str, Any] | None = None) -> dict[str, Any]:
    return {
        "error": {
            "code": code,
            "message": message,
            "request_id": request_id,
            "details": details or {},
        }
    }


def error_detail(code: str, message: str, retryable: bool | None = None) -> dict[str, Any]:
    payload: dict[str, Any] = {"code": code, "message": message}
    if retryable is not None:
        payload["retryable"] = retryable
    return payload


__all__ = [
    "PRODUCT_ERROR_STATUS",
    "ProductError",
    "error_body",
    "error_detail",
    "map_runtime_error_code",
    "product_code_exists",
    "public_message_for",
]
