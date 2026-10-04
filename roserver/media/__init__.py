"""media session infrastructure (docs §5.4-§5.6): engine, simulator, service."""

from .engine import (
    MEDIA_KINDS,
    MediaEngine,
    MediaEngineError,
    MediaNegotiationError,
    SimulatedMediaEngine,
    to_product_error,
)
from .service import MediaService, media_session_object

__all__ = [
    "MEDIA_KINDS",
    "MediaEngine",
    "MediaEngineError",
    "MediaNegotiationError",
    "MediaService",
    "SimulatedMediaEngine",
    "media_session_object",
    "to_product_error",
]
