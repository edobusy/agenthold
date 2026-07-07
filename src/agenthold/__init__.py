"""Agenthold : shared versioned state for multi-agent AI workflows."""

__version__ = "0.7.0"

from agenthold.coordinator import Coordinator
from agenthold.exceptions import BusyError, ConflictError, NotFoundError
from agenthold.models import SetResult, StateRecord, StateRecordHistory
from agenthold.resources import (
    DEFAULT_WORKSPACE_NAME,
    ResourceId,
    Workspace,
    WorkspaceRegistry,
)
from agenthold.store import StateStore

__all__ = [
    "DEFAULT_WORKSPACE_NAME",
    "BusyError",
    "ConflictError",
    "Coordinator",
    "NotFoundError",
    "ResourceId",
    "SetResult",
    "StateRecord",
    "StateRecordHistory",
    "StateStore",
    "Workspace",
    "WorkspaceRegistry",
    "__version__",
]
