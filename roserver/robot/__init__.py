"""robot backend infrastructure (docs §4): client protocol, simulator, service."""

from .backend import (
    ControlAuthorityConflictError,
    ControlAuthorityRequiredError,
    RobotBackendError,
    RobotBackend,
    RobotNotReadyError,
    RobotNotFoundError,
    RobotTimeoutError,
    RobotUnavailableError,
    SimulatedRobotBackend,
    to_product_error,
)
from .service import RobotService

__all__ = [
    "ControlAuthorityConflictError",
    "ControlAuthorityRequiredError",
    "RobotBackendError",
    "RobotBackend",
    "RobotNotReadyError",
    "RobotNotFoundError",
    "RobotService",
    "RobotTimeoutError",
    "RobotUnavailableError",
    "SimulatedRobotBackend",
    "to_product_error",
]
