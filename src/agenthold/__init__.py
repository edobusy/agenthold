"""Agenthold : shared versioned state for multi-agent AI workflows."""

__version__ = "0.2.0"

from agenthold.exceptions import BusyError, ConflictError, NotFoundError
from agenthold.models import SetResult, StateRecord, StateRecordHistory
from agenthold.store import StateStore

__all__ = [
    "BusyError",
    "ConflictError",
    "NotFoundError",
    "SetResult",
    "StateRecord",
    "StateRecordHistory",
    "StateStore",
    "__version__",
]
