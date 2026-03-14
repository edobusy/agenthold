from datetime import datetime
from typing import Any

from pydantic import BaseModel


class StateRecord(BaseModel):
    """A single versioned state entry."""

    namespace: str
    key: str
    value: Any  # any JSON-serialisable type
    version: int  # starts at 1, increments on each write
    updated_by: str  # agent identifier, e.g. "agent-a"
    updated_at: datetime


class StateRecordHistory(BaseModel):
    """A historical snapshot of a record."""

    namespace: str
    key: str
    value: Any
    version: int
    updated_by: str
    updated_at: datetime


class SetResult(BaseModel):
    """Returned after a successful write."""

    namespace: str
    key: str
    version: int  # the new version number
    previous_version: int | None  # None if this was the first write


class ConflictDetail(BaseModel):
    """Returned inside a ConflictError."""

    namespace: str
    key: str
    expected_version: int  # what the agent thought the version was
    actual_version: int  # what it actually is
    updated_by: str  # who wrote the conflicting version
    updated_at: datetime
