import pytest

from agenthold.resources import Workspace, WorkspaceRegistry
from agenthold.store import StateStore


@pytest.fixture
def store() -> StateStore:
    """In-memory store, isolated per test, no cleanup needed."""
    return StateStore(":memory:")


@pytest.fixture
def populated_store(store: StateStore) -> StateStore:
    """Store pre-loaded with a few records for read-focused tests."""
    store.set("ns-a", "key1", "value1", updated_by="agent-a")
    store.set("ns-a", "key2", 42, updated_by="agent-a")
    store.set("ns-b", "key1", {"nested": True}, updated_by="agent-b")
    return store


@pytest.fixture
def registry() -> WorkspaceRegistry:
    """A single-workspace registry rooted at /work, named 'default'.

    Tests that need a different layout build their own registry inline.
    """
    return WorkspaceRegistry([Workspace(name="default", root="/work")])
