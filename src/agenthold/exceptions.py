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