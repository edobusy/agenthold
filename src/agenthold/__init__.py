"""Agenthold : shared versioned state for multi-agent AI workflows."""

__version__ = "0.4.0"

from agenthold.coordinator import Coordinator
from agenthold.exceptions import BusyError, ConflictError, NotFoundError
from agenthold.models import SetResult, StateRecord, StateRecordHistory
from agenthold.store import StateStore

__all__ = [
    "BusyError",
    "ConflictError",
    "Coordinator",
    "NotFoundError",
    "SetResult",
    "StateRecord",
    "StateRecordHistory",
    "StateStore",
    "__version__",
]
