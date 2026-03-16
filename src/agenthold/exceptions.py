from agenthold.models import ConflictDetail


class AgentholdError(Exception):
    """Base class for all agenthold errors."""


class ConflictError(AgentholdError):
    """
    Raised when a write fails because expected_version does not match
    the current version. This is optimistic concurrency control,
    the same mechanism as Postgres's UPDATE ... WHERE version = N.
    """

    def __init__(self, detail: ConflictDetail) -> None:
        self.detail = detail
        super().__init__(
            f"Version conflict on {detail.namespace}/{detail.key}: "
            f"expected {detail.expected_version}, "
            f"got {detail.actual_version} (written by {detail.updated_by})"
        )


class NotFoundError(AgentholdError):
    """Raised when a key does not exist in the given namespace."""

    def __init__(self, namespace: str, key: str) -> None:
        self.namespace = namespace
        self.key = key
        super().__init__(f"Key '{key}' not found in namespace '{namespace}'")


class BusyError(AgentholdError):
    """Raised when the database is locked by another writer.

    This happens when BEGIN IMMEDIATE cannot acquire the write lock
    within the busy_timeout window (another process is holding it).
    The caller should retry after a short delay.
    """

    def __init__(self) -> None:
        super().__init__(
            "The database is temporarily locked by another writer. "
            "Retry the operation after a short delay."
        )
