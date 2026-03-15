"""Agenthold : shared versioned state for multi-agent AI workflows."""

__version__ = "0.1.4"

from agenthold.exceptions import ConflictError, NotFoundError
from agenthold.models import SetResult, StateRecord, StateRecordHistory
from agenthold.store import StateStore

__all__ = [
    "ConflictError",
    "NotFoundError",
    "SetResult",
    "StateRecord",
    "StateRecordHistory",
    "StateStore",
    "__version__",
]
