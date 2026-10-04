"""Speech bridge: PCM <-> roboagent SpeechSession (docs §5.7, Phase 5B)."""

from .api import router
from .engine import SpeechEngine, SpeechEngineError, SimulatedSpeechEngine, to_product_error
from .service import SpeechBridge, SpeechService

__all__ = [
    "SpeechBridge",
    "SpeechEngine",
    "SpeechEngineError",
    "SpeechService",
    "SimulatedSpeechEngine",
    "router",
    "to_product_error",
]
