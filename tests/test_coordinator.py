"""Tests for the Coordinator claim lifecycle."""

import json
import threading
from datetime import UTC, datetime
from unittest.mock import patch

import pytest

from agenthold.coordinator import Coordinator
from agenthold.exceptions import BusyError, ConflictError
from agenthold.models import ConflictDetail
from agenthold.store import StateStore


@pytest.fixture
def coordinator(store: StateStore) -> Coordinator:
    return Coordinator(store)


# ---------------------------------------------------------------------------
# Normalization
# ---------------------------------------------------------------------------


class TestNormalization:
    def test_strips_dot_slash(self) -> None:
        assert Coordinator._normalize_resource("./foo.md") == "foo.md"

    def test_strips_repeated_dot_slash(self) -> None:
        assert Coordinator._normalize_resource("././foo.md") == "foo.md"

    def test_collapses_slashes(self) -> None:
        assert Coordinator._normalize_resource("a//b///c") == "a/b/c"

    def test_backslashes(self) -> None:
        assert Coordinator._normalize_resource("a\\b\\c") == "a/b/c"

    def test_no_change(self) -> None:
        assert Coordinator._normalize_resource("foo.md") == "foo.md"

    def test_combined(self) -> None:
        assert Coordinator._normalize_resource(".\\src\\\\main.py") == "src/main.py"


# ---------------------------------------------------------------------------
# Claim
# ---------------------------------------------------------------------------


class TestClaim:
    def test_claim_unclaimed_resource(self, coordinator: Coordinator) -> None:
        result = coordinator.claim("intro.md", "agent-a")
        assert result["status"] == "claimed"
        assert result["resource"] == "intro.md"
        assert result["version"] == 1

    def test_claim_free_resource(self, coordinator: Coordinator) -> None:
        coordinator.claim("intro.md", "agent-a")
        coordinator.release("intro.md", "agent-a")
        result = coordinator.claim("intro.md", "agent-b")
        assert result["status"] == "claimed"
        assert result["version"] == 3  # claim(v1) + release(v2) + claim(v3)

    def test_claim_already_claimed_by_self(self, coordinator: Coordinator) -> None:
        coordinator.claim("intro.md", "agent-a")
        result = coordinator.claim("intro.md", "agent-a")
        assert result["status"] == "already_claimed"
        assert result["resource"] == "intro.md"

    def test_claim_busy(self, coordinator: Coordinator) -> None:
        coordinator.claim("intro.md", "agent-a")
        result = coordinator.claim("intro.md", "agent-b")
        assert result["status"] == "busy"
        assert result["held_by"] == "agent-a"
        assert "hint" in result

    def test_claim_malformed_value(
        self, coordinator: Coordinator, store: StateStore
    ) -> None:
        """Existing value with no 'status' field is treated as unclaimed."""
        store.set("claims", "intro.md", {"foo": "bar"}, updated_by="x")
        result = coordinator.claim("intro.md", "agent-a")
        assert result["status"] == "claimed"

    def test_claim_malformed_value_not_a_dict(
        self, coordinator: Coordinator, store: StateStore
    ) -> None:
        """Plain string written via advanced mode is treated as unclaimed."""
        store.set("claims", "intro.md", "just-a-string", updated_by="x")
        result = coordinator.claim("intro.md", "agent-a")
        assert result["status"] == "claimed"

    def test_claim_validates_resource_empty(self, coordinator: Coordinator) -> None:
        with pytest.raises(ValueError, match="resource"):
            coordinator.claim("", "agent-a")

    def test_claim_validates_resource_null_bytes(
        self, coordinator: Coordinator
    ) -> None:
        with pytest.raises(ValueError, match="resource"):
            coordinator.claim("foo\x00bar", "agent-a")

    def test_claim_validates_resource_overlength(
        self, coordinator: Coordinator
    ) -> None:
        with pytest.raises(ValueError, match="resource"):
            coordinator.claim("x" * 513, "agent-a")

    def test_claim_dot_slash_only_raises(self, coordinator: Coordinator) -> None:
        """'./' normalizes to '' which must be rejected."""
        with pytest.raises(ValueError, match="resource.*empty after normalization"):
            coordinator.claim("./", "agent-a")

    def test_claim_repeated_dot_slash_only_raises(
        self, coordinator: Coordinator
    ) -> None:
        """'././' normalizes to '' which must be rejected."""
        with pytest.raises(ValueError, match="resource.*empty after normalization"):
            coordinator.claim("././", "agent-a")

    def test_claim_validates_agent_empty(self, coordinator: Coordinator) -> None:
        with pytest.raises(ValueError, match="agent"):
            coordinator.claim("intro.md", "")

    def test_claim_normalizes_resource(self, coordinator: Coordinator) -> None:
        """./intro.md and intro.md resolve to the same claim."""
        coordinator.claim("./intro.md", "agent-a")
        result = coordinator.claim("intro.md", "agent-a")
        assert result["status"] == "already_claimed"


# ---------------------------------------------------------------------------
# Claim race condition
# ---------------------------------------------------------------------------


class TestClaimRace:
    def test_claim_race_from_unclaimed(self) -> None:
        """Two threads race to claim an unclaimed resource."""
        import tempfile

        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            db_path = f.name

        store = StateStore(db_path)
        coord = Coordinator(store)
        results: list[dict[str, object]] = [{}] * 2
        barrier = threading.Barrier(2)

        def do_claim(idx: int) -> None:
            barrier.wait()
            results[idx] = coord.claim("race.md", f"agent-{idx}")

        threads = [threading.Thread(target=do_claim, args=(i,)) for i in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        statuses = {r["status"] for r in results}
        assert "claimed" in statuses
        # The loser gets either "busy" or "already_claimed" (if same agent)
        assert statuses <= {"claimed", "busy"}
        store.close()

    def test_claim_race_from_free(self) -> None:
        """Two threads race to claim a just-released resource."""
        import tempfile

        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            db_path = f.name

        store = StateStore(db_path)
        coord = Coordinator(store)
        coord.claim("race.md", "setup-agent")
        coord.release("race.md", "setup-agent")

        results: list[dict[str, object]] = [{}] * 2
        barrier = threading.Barrier(2)

        def do_claim(idx: int) -> None:
            barrier.wait()
            results[idx] = coord.claim("race.md", f"agent-{idx}")

        threads = [threading.Thread(target=do_claim, args=(i,)) for i in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        statuses = {r["status"] for r in results}
        assert "claimed" in statuses
        assert statuses <= {"claimed", "busy"}
        store.close()


# ---------------------------------------------------------------------------
# Release
# ---------------------------------------------------------------------------


class TestRelease:
    def test_release_own_claim(self, coordinator: Coordinator) -> None:
        coordinator.claim("intro.md", "agent-a")
        result = coordinator.release("intro.md", "agent-a")
        assert result["status"] == "released"
        assert result["version"] == 2

    def test_release_other_agents_claim(self, coordinator: Coordinator) -> None:
        coordinator.claim("intro.md", "agent-a")
        result = coordinator.release("intro.md", "agent-b")
        assert result["status"] == "error"
        assert "agent-a" in result["message"]

    def test_release_unclaimed(self, coordinator: Coordinator) -> None:
        result = coordinator.release("intro.md", "agent-a")
        assert result["status"] == "not_found"

    def test_release_already_free(self, coordinator: Coordinator) -> None:
        coordinator.claim("intro.md", "agent-a")
        coordinator.release("intro.md", "agent-a")
        result = coordinator.release("intro.md", "agent-a")
        assert result["status"] == "already_free"

    def test_release_validates_resource(self, coordinator: Coordinator) -> None:
        with pytest.raises(ValueError, match="resource"):
            coordinator.release("", "agent-a")

    def test_release_validates_agent(self, coordinator: Coordinator) -> None:
        with pytest.raises(ValueError, match="agent"):
            coordinator.release("intro.md", "")


# ---------------------------------------------------------------------------
# Status
# ---------------------------------------------------------------------------


class TestStatus:
    def test_status_unclaimed(self, coordinator: Coordinator) -> None:
        result = coordinator.status("intro.md")
        assert result["status"] == "available"

    def test_status_claimed(self, coordinator: Coordinator) -> None:
        coordinator.claim("intro.md", "agent-a")
        result = coordinator.status("intro.md")
        assert result["status"] == "claimed"
        assert result["held_by"] == "agent-a"

    def test_status_free(self, coordinator: Coordinator) -> None:
        coordinator.claim("intro.md", "agent-a")
        coordinator.release("intro.md", "agent-a")
        result = coordinator.status("intro.md")
        assert result["status"] == "available"
        assert result["last_held_by"] == "agent-a"

    def test_status_validates_resource(self, coordinator: Coordinator) -> None:
        with pytest.raises(ValueError, match="resource"):
            coordinator.status("")


# ---------------------------------------------------------------------------
# interpret_state
# ---------------------------------------------------------------------------


class TestInterpretState:
    def test_unclaimed(self, coordinator: Coordinator) -> None:
        state = coordinator.interpret_state("intro.md")
        assert state["state"] == "unclaimed"

    def test_claimed(self, coordinator: Coordinator) -> None:
        coordinator.claim("intro.md", "agent-a")
        state = coordinator.interpret_state("intro.md")
        assert state["state"] == "claimed"
        assert state["held_by"] == "agent-a"

    def test_free(self, coordinator: Coordinator) -> None:
        coordinator.claim("intro.md", "agent-a")
        coordinator.release("intro.md", "agent-a")
        state = coordinator.interpret_state("intro.md")
        assert state["state"] == "free"
        assert state["last_held_by"] == "agent-a"

    def test_malformed_value_is_unclaimed(
        self, coordinator: Coordinator, store: StateStore
    ) -> None:
        store.set("claims", "intro.md", 42, updated_by="x")
        state = coordinator.interpret_state("intro.md")
        assert state["state"] == "unclaimed"


# ---------------------------------------------------------------------------
# JSON serialisability
# ---------------------------------------------------------------------------


class TestJsonSerialisability:
    def test_claim_response(self, coordinator: Coordinator) -> None:
        result = coordinator.claim("intro.md", "agent-a")
        json.dumps(result)  # must not raise

    def test_release_response(self, coordinator: Coordinator) -> None:
        coordinator.claim("intro.md", "agent-a")
        result = coordinator.release("intro.md", "agent-a")
        json.dumps(result)

    def test_status_response(self, coordinator: Coordinator) -> None:
        result = coordinator.status("intro.md")
        json.dumps(result)

    def test_busy_response(self, coordinator: Coordinator) -> None:
        coordinator.claim("intro.md", "agent-a")
        result = coordinator.claim("intro.md", "agent-b")
        json.dumps(result)


# ---------------------------------------------------------------------------
# BusyError in conflict-recovery paths
# ---------------------------------------------------------------------------


class TestBusyErrorInConflictRecovery:
    def test_attempt_claim_busy_on_reread(
        self, coordinator: Coordinator, store: StateStore
    ) -> None:
        """If re-read after ConflictError raises BusyError, return busy."""
        from agenthold.exceptions import NotFoundError

        detail = ConflictDetail(
            namespace="claims",
            key="intro.md",
            expected_version=0,
            actual_version=1,
            actual_value={"status": "claimed", "by": "agent-x"},
            updated_by="agent-x",
            updated_at=datetime.now(UTC),
        )
        # First get raises NotFoundError (unclaimed), then re-read raises
        # BusyError after the ConflictError from set.
        get_side_effects = [
            NotFoundError("claims", "intro.md"),
            BusyError(),
        ]
        with (
            patch.object(
                store,
                "set",
                side_effect=ConflictError(detail),
            ),
            patch.object(
                store,
                "get",
                side_effect=get_side_effects,
            ),
        ):
            result = coordinator.claim("intro.md", "agent-a")

        assert result["status"] == "busy"
        assert result["resource"] == "intro.md"
        assert result["held_by"] == "unknown"
        assert "temporarily locked" in result["hint"]
        json.dumps(result)  # must be JSON-serialisable

    def test_release_busy_on_reread(
        self, coordinator: Coordinator, store: StateStore
    ) -> None:
        """If re-read after release ConflictError raises BusyError, return error."""
        # Set up a claimed resource normally first.
        coordinator.claim("intro.md", "agent-a")

        detail = ConflictDetail(
            namespace="claims",
            key="intro.md",
            expected_version=1,
            actual_version=2,
            actual_value={"status": "claimed", "by": "agent-a"},
            updated_by="agent-a",
            updated_at=datetime.now(UTC),
        )
        # First get returns the real record (release reads current state),
        # then set raises ConflictError, then re-read raises BusyError.
        real_record = store.get("claims", "intro.md")
        get_side_effects = [real_record, BusyError()]
        with (
            patch.object(
                store,
                "set",
                side_effect=ConflictError(detail),
            ),
            patch.object(
                store,
                "get",
                side_effect=get_side_effects,
            ),
        ):
            result = coordinator.release("intro.md", "agent-a")

        assert result["status"] == "error"
        assert "temporarily locked" in result["message"]
        json.dumps(result)  # must be JSON-serialisable
