"""Tests for the Coordinator claim lifecycle."""

import json
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import pytest

from agenthold.coordinator import Coordinator
from agenthold.exceptions import BusyError, ConflictError
from agenthold.models import ConflictDetail
from agenthold.resources import Workspace, WorkspaceRegistry
from agenthold.store import StateStore


@pytest.fixture
def coordinator(store: StateStore, registry: WorkspaceRegistry) -> Coordinator:
    return Coordinator(store, registry)


@pytest.fixture
def ttl_coordinator(store: StateStore, registry: WorkspaceRegistry) -> Coordinator:
    return Coordinator(store, registry, claim_ttl=60.0)


# ---------------------------------------------------------------------------
# Claim
# ---------------------------------------------------------------------------


class TestClaim:
    def test_claim_unclaimed_resource(self, coordinator: Coordinator) -> None:
        result = coordinator.claim("intro.md", "agent-a")
        assert result["status"] == "claimed"
        assert result["resource"] == "file://default/intro.md"
        assert result["version"] == 1
        # No previous_outcome on a never-seen resource
        assert "previous_outcome" not in result

    def test_claim_free_resource_propagates_outcome(
        self, coordinator: Coordinator
    ) -> None:
        coordinator.claim("intro.md", "agent-a")
        coordinator.release("intro.md", "agent-a", outcome="modified")
        result = coordinator.claim("intro.md", "agent-b")
        assert result["status"] == "claimed"
        assert result["version"] == 3
        assert result["previous_outcome"] == "modified"
        assert result["previous_holder"] == "agent-a"
        # Hint only for noisy outcomes — modified is not noisy
        assert "hint" not in result

    def test_claim_after_delete_surfaces_hint(self, coordinator: Coordinator) -> None:
        coordinator.claim("temp.txt", "agent-a")
        coordinator.release("temp.txt", "agent-a", outcome="deleted")
        result = coordinator.claim("temp.txt", "agent-b")
        assert result["previous_outcome"] == "deleted"
        assert "hint" in result
        assert "may not exist" in result["hint"]

    def test_claim_after_move_surfaces_moved_to(self, coordinator: Coordinator) -> None:
        coordinator.claim("old.py", "agent-a")
        coordinator.release("old.py", "agent-a", outcome="moved", moved_to="new.py")
        result = coordinator.claim("old.py", "agent-b")
        assert result["previous_outcome"] == "moved"
        assert result["moved_to"] == "file://default/new.py"
        assert "hint" in result

    def test_claim_already_claimed_by_self(self, coordinator: Coordinator) -> None:
        coordinator.claim("intro.md", "agent-a")
        result = coordinator.claim("intro.md", "agent-a")
        assert result["status"] == "already_claimed"
        # No previous_outcome — same agent, idempotent
        assert "previous_outcome" not in result

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
        store.set(
            Coordinator.NAMESPACE,
            "file://default/intro.md",
            {"foo": "bar"},
            updated_by="x",
        )
        result = coordinator.claim("intro.md", "agent-a")
        assert result["status"] == "claimed"

    def test_claim_normalizes_resource(self, coordinator: Coordinator) -> None:
        """./intro.md and intro.md resolve to the same claim."""
        coordinator.claim("./intro.md", "agent-a")
        result = coordinator.claim("intro.md", "agent-a")
        assert result["status"] == "already_claimed"

    def test_claim_validates_resource_empty(self, coordinator: Coordinator) -> None:
        with pytest.raises(ValueError, match="empty"):
            coordinator.claim("", "agent-a")

    def test_claim_validates_resource_null_bytes(
        self, coordinator: Coordinator
    ) -> None:
        with pytest.raises(ValueError, match="null"):
            coordinator.claim("foo\x00bar", "agent-a")

    def test_claim_validates_resource_overlength(
        self, coordinator: Coordinator
    ) -> None:
        with pytest.raises(ValueError, match="exceeds"):
            coordinator.claim("x" * 600, "agent-a")

    def test_claim_dot_slash_only_raises(self, coordinator: Coordinator) -> None:
        with pytest.raises(ValueError, match="empty"):
            coordinator.claim("./", "agent-a")

    def test_claim_validates_agent_empty(self, coordinator: Coordinator) -> None:
        with pytest.raises(ValueError, match="agent"):
            coordinator.claim("intro.md", "")


# ---------------------------------------------------------------------------
# Claim race condition
# ---------------------------------------------------------------------------


class TestClaimRace:
    def test_claim_race_from_unclaimed(self, registry: WorkspaceRegistry) -> None:
        """Two threads race to claim an unclaimed resource."""
        import tempfile

        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            db_path = f.name

        store = StateStore(db_path)
        coord = Coordinator(store, registry)
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

    def test_claim_race_from_free(self, registry: WorkspaceRegistry) -> None:
        """Two threads race to claim a just-released resource."""
        import tempfile

        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            db_path = f.name

        store = StateStore(db_path)
        coord = Coordinator(store, registry)
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
    def test_release_own_claim_default_outcome(self, coordinator: Coordinator) -> None:
        coordinator.claim("intro.md", "agent-a")
        result = coordinator.release("intro.md", "agent-a")
        assert result["status"] == "released"
        assert result["outcome"] == "released"
        assert result["version"] == 2

    def test_release_with_modified(self, coordinator: Coordinator) -> None:
        coordinator.claim("intro.md", "agent-a")
        result = coordinator.release("intro.md", "agent-a", outcome="modified")
        assert result["outcome"] == "modified"

    def test_release_with_created(self, coordinator: Coordinator) -> None:
        coordinator.claim("new.py", "agent-a")
        result = coordinator.release("new.py", "agent-a", outcome="created")
        assert result["outcome"] == "created"

    def test_release_with_deleted(self, coordinator: Coordinator) -> None:
        coordinator.claim("temp.txt", "agent-a")
        result = coordinator.release("temp.txt", "agent-a", outcome="deleted")
        assert result["outcome"] == "deleted"

    def test_release_with_moved(self, coordinator: Coordinator) -> None:
        coordinator.claim("old.py", "agent-a")
        result = coordinator.release(
            "old.py", "agent-a", outcome="moved", moved_to="new.py"
        )
        assert result["outcome"] == "moved"
        assert result["moved_to"] == "file://default/new.py"

    def test_release_with_moved_uri_target(self, coordinator: Coordinator) -> None:
        coordinator.claim("old.py", "agent-a")
        result = coordinator.release(
            "old.py",
            "agent-a",
            outcome="moved",
            moved_to="file://default/new.py",
        )
        assert result["moved_to"] == "file://default/new.py"

    def test_release_invalid_outcome_raises(self, coordinator: Coordinator) -> None:
        coordinator.claim("intro.md", "agent-a")
        with pytest.raises(ValueError, match="Invalid outcome"):
            coordinator.release("intro.md", "agent-a", outcome="bogus")

    def test_release_abandoned_outcome_not_allowed_for_agent(
        self, coordinator: Coordinator
    ) -> None:
        """Agents can't set 'abandoned' — that's server-only."""
        coordinator.claim("intro.md", "agent-a")
        with pytest.raises(ValueError, match="Invalid outcome"):
            coordinator.release("intro.md", "agent-a", outcome="abandoned")

    def test_release_expired_outcome_not_allowed_for_agent(
        self, coordinator: Coordinator
    ) -> None:
        coordinator.claim("intro.md", "agent-a")
        with pytest.raises(ValueError, match="Invalid outcome"):
            coordinator.release("intro.md", "agent-a", outcome="expired")

    def test_release_moved_without_target_raises(
        self, coordinator: Coordinator
    ) -> None:
        coordinator.claim("old.py", "agent-a")
        with pytest.raises(ValueError, match="moved_to is required"):
            coordinator.release("old.py", "agent-a", outcome="moved")

    def test_release_non_moved_with_target_raises(
        self, coordinator: Coordinator
    ) -> None:
        coordinator.claim("intro.md", "agent-a")
        with pytest.raises(ValueError, match="only allowed with"):
            coordinator.release(
                "intro.md",
                "agent-a",
                outcome="modified",
                moved_to="other.md",
            )

    def test_release_moved_to_same_resource_raises(
        self, coordinator: Coordinator
    ) -> None:
        coordinator.claim("old.py", "agent-a")
        with pytest.raises(ValueError, match="differ from the source"):
            coordinator.release(
                "old.py",
                "agent-a",
                outcome="moved",
                moved_to="old.py",
            )

    def test_release_moved_to_canonicalizes_target(
        self, coordinator: Coordinator
    ) -> None:
        """Moved_to with equivalent paths is recognized as same."""
        coordinator.claim("old.py", "agent-a")
        with pytest.raises(ValueError, match="differ from the source"):
            coordinator.release(
                "old.py",
                "agent-a",
                outcome="moved",
                moved_to="./old.py",
            )

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
        assert "previous_outcome" not in result

    def test_status_claimed(self, coordinator: Coordinator) -> None:
        coordinator.claim("intro.md", "agent-a")
        result = coordinator.status("intro.md")
        assert result["status"] == "claimed"
        assert result["held_by"] == "agent-a"

    def test_status_free_with_outcome(self, coordinator: Coordinator) -> None:
        coordinator.claim("intro.md", "agent-a")
        coordinator.release("intro.md", "agent-a", outcome="modified")
        result = coordinator.status("intro.md")
        assert result["status"] == "available"
        assert result["previous_outcome"] == "modified"
        assert result["previous_holder"] == "agent-a"
        # No hint for modified
        assert "hint" not in result

    def test_status_free_with_deleted_has_hint(self, coordinator: Coordinator) -> None:
        coordinator.claim("temp.txt", "agent-a")
        coordinator.release("temp.txt", "agent-a", outcome="deleted")
        result = coordinator.status("temp.txt")
        assert result["previous_outcome"] == "deleted"
        assert "hint" in result

    def test_status_free_with_moved_has_moved_to(
        self, coordinator: Coordinator
    ) -> None:
        coordinator.claim("old.py", "agent-a")
        coordinator.release("old.py", "agent-a", outcome="moved", moved_to="new.py")
        result = coordinator.status("old.py")
        assert result["previous_outcome"] == "moved"
        assert result["moved_to"] == "file://default/new.py"

    def test_status_validates_resource(self, coordinator: Coordinator) -> None:
        with pytest.raises(ValueError, match="empty"):
            coordinator.status("")


# ---------------------------------------------------------------------------
# interpret_state
# ---------------------------------------------------------------------------


class TestInterpretState:
    def test_unclaimed(self, coordinator: Coordinator) -> None:
        state = coordinator.interpret_state("intro.md")
        assert state["state"] == "unclaimed"
        assert state["resource"] == "file://default/intro.md"

    def test_claimed(self, coordinator: Coordinator) -> None:
        coordinator.claim("intro.md", "agent-a")
        state = coordinator.interpret_state("intro.md")
        assert state["state"] == "claimed"
        assert state["held_by"] == "agent-a"

    def test_free(self, coordinator: Coordinator) -> None:
        coordinator.claim("intro.md", "agent-a")
        coordinator.release("intro.md", "agent-a", outcome="modified")
        state = coordinator.interpret_state("intro.md")
        assert state["state"] == "free"
        assert state["released_by"] == "agent-a"
        assert state["outcome"] == "modified"

    def test_free_with_moved_to(self, coordinator: Coordinator) -> None:
        coordinator.claim("old.py", "agent-a")
        coordinator.release("old.py", "agent-a", outcome="moved", moved_to="new.py")
        state = coordinator.interpret_state("old.py")
        assert state["state"] == "free"
        assert state["outcome"] == "moved"
        assert state["moved_to"] == "file://default/new.py"

    def test_malformed_value_is_unclaimed(
        self, coordinator: Coordinator, store: StateStore
    ) -> None:
        store.set(
            Coordinator.NAMESPACE,
            "file://default/intro.md",
            42,
            updated_by="x",
        )
        state = coordinator.interpret_state("intro.md")
        assert state["state"] == "unclaimed"


# ---------------------------------------------------------------------------
# JSON serialisability
# ---------------------------------------------------------------------------


class TestJsonSerialisability:
    def test_claim_response(self, coordinator: Coordinator) -> None:
        result = coordinator.claim("intro.md", "agent-a")
        json.dumps(result)

    def test_claim_with_previous_outcome(self, coordinator: Coordinator) -> None:
        coordinator.claim("intro.md", "agent-a")
        coordinator.release("intro.md", "agent-a", outcome="moved", moved_to="other.md")
        result = coordinator.claim("intro.md", "agent-b")
        json.dumps(result)

    def test_release_response(self, coordinator: Coordinator) -> None:
        coordinator.claim("intro.md", "agent-a")
        result = coordinator.release(
            "intro.md", "agent-a", outcome="moved", moved_to="x.md"
        )
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
        from agenthold.exceptions import NotFoundError

        detail = ConflictDetail(
            namespace="claims",
            key="file://default/intro.md",
            expected_version=0,
            actual_version=1,
            actual_value={"status": "claimed", "by": "agent-x"},
            updated_by="agent-x",
            updated_at=datetime.now(UTC),
        )
        get_side_effects = [
            NotFoundError("claims", "file://default/intro.md"),
            BusyError(),
        ]
        with (
            patch.object(store, "set", side_effect=ConflictError(detail)),
            patch.object(store, "get", side_effect=get_side_effects),
        ):
            result = coordinator.claim("intro.md", "agent-a")

        assert result["status"] == "busy"
        assert result["resource"] == "file://default/intro.md"
        assert result["held_by"] == "unknown"
        assert "temporarily locked" in result["hint"]
        json.dumps(result)

    def test_release_busy_on_reread(
        self, coordinator: Coordinator, store: StateStore
    ) -> None:
        coordinator.claim("intro.md", "agent-a")

        detail = ConflictDetail(
            namespace="claims",
            key="file://default/intro.md",
            expected_version=1,
            actual_version=2,
            actual_value={"status": "claimed", "by": "agent-a"},
            updated_by="agent-a",
            updated_at=datetime.now(UTC),
        )
        real_record = store.get("claims", "file://default/intro.md")
        get_side_effects = [real_record, BusyError()]
        with (
            patch.object(store, "set", side_effect=ConflictError(detail)),
            patch.object(store, "get", side_effect=get_side_effects),
        ):
            result = coordinator.release("intro.md", "agent-a")

        assert result["status"] == "error"
        assert "temporarily locked" in result["message"]
        json.dumps(result)


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


class TestRegister:
    def test_register_returns_agent_id(self, coordinator: Coordinator) -> None:
        result = coordinator.register("test-agent")
        assert result["status"] == "registered"
        assert result["agent_id"].startswith("agent-")
        assert len(result["agent_id"]) == 14
        assert result["name"] == "test-agent"

    def test_register_with_model(self, coordinator: Coordinator) -> None:
        result = coordinator.register("test-agent", model="claude-sonnet-4-6")
        assert result["status"] == "registered"

    def test_register_empty_name_raises(self, coordinator: Coordinator) -> None:
        with pytest.raises(ValueError, match="name"):
            coordinator.register("")

    def test_register_whitespace_name_raises(self, coordinator: Coordinator) -> None:
        with pytest.raises(ValueError, match="name"):
            coordinator.register("   ")

    def test_register_long_name_raises(self, coordinator: Coordinator) -> None:
        with pytest.raises(ValueError, match="256"):
            coordinator.register("x" * 257)

    def test_is_registered(self, coordinator: Coordinator) -> None:
        result = coordinator.register("test-agent")
        assert coordinator.is_registered(result["agent_id"])

    def test_is_not_registered(self, coordinator: Coordinator) -> None:
        assert not coordinator.is_registered("agent-unknown")

    def test_register_json_serialisable(self, coordinator: Coordinator) -> None:
        result = coordinator.register("test-agent")
        json.dumps(result)


# ---------------------------------------------------------------------------
# Durable registration  (DB-backed is_registered + session scoping)
# ---------------------------------------------------------------------------


class TestDurableRegistration:
    """Tests for the cross-process / cross-coordinator registration story.

    These exercise the failure modes documented in
    plan/durable-agent-registration.md: a Coordinator must recognize
    agents that were registered against the same DB by another
    Coordinator (post-restart, multi-process, or out-of-process CLI),
    but cleanup must remain scoped to agents this Coordinator's own
    register() created.
    """

    def _file_coord(self, db_path: Path, registry: WorkspaceRegistry) -> Coordinator:
        return Coordinator(StateStore(db_path), registry)

    def test_register_tracks_agent_in_both_sets(self, coordinator: Coordinator) -> None:
        result = coordinator.register("test-agent")
        agent_id = result["agent_id"]
        assert agent_id in coordinator._registered_agents
        assert agent_id in coordinator._session_agents
        assert agent_id in coordinator.session_agent_ids

    def test_is_registered_db_fallback_recognizes_agent_from_other_coordinator(
        self, tmp_path: Path, registry: WorkspaceRegistry
    ) -> None:
        db_path = tmp_path / "shared.db"
        coord_a = self._file_coord(db_path, registry)
        agent_id = coord_a.register("from-a")["agent_id"]

        coord_b = self._file_coord(db_path, registry)
        assert coord_b.is_registered(agent_id) is True

        coord_a._store.close()
        coord_b._store.close()

    def test_db_fallback_does_not_pollute_session_agents(
        self, tmp_path: Path, registry: WorkspaceRegistry
    ) -> None:
        db_path = tmp_path / "shared.db"
        coord_a = self._file_coord(db_path, registry)
        agent_id = coord_a.register("from-a")["agent_id"]

        coord_b = self._file_coord(db_path, registry)
        assert coord_b.is_registered(agent_id) is True
        # Recognition cache filled, session set untouched.
        assert agent_id in coord_b._registered_agents
        assert agent_id not in coord_b._session_agents
        assert coord_b.session_agent_ids == ()

        coord_a._store.close()
        coord_b._store.close()

    def test_is_registered_caches_db_fallback_result(
        self, tmp_path: Path, registry: WorkspaceRegistry
    ) -> None:
        db_path = tmp_path / "shared.db"
        coord_a = self._file_coord(db_path, registry)
        agent_id = coord_a.register("from-a")["agent_id"]

        coord_b = self._file_coord(db_path, registry)
        with patch.object(coord_b._store, "get", wraps=coord_b._store.get) as spy:
            assert coord_b.is_registered(agent_id) is True
            assert spy.call_count == 1
            # Second call hits the cache; no further DB read.
            assert coord_b.is_registered(agent_id) is True
            assert spy.call_count == 1

        coord_a._store.close()
        coord_b._store.close()

    def test_is_registered_rejects_inactive_agent_via_db(
        self, tmp_path: Path, registry: WorkspaceRegistry
    ) -> None:
        db_path = tmp_path / "shared.db"
        coord_a = self._file_coord(db_path, registry)
        agent_id = coord_a.register("from-a")["agent_id"]
        coord_a.deactivate_agent(agent_id)

        coord_b = self._file_coord(db_path, registry)
        assert coord_b.is_registered(agent_id) is False
        # Inactive agents must not pollute either set.
        assert agent_id not in coord_b._registered_agents
        assert agent_id not in coord_b._session_agents

        coord_a._store.close()
        coord_b._store.close()

    def test_is_registered_returns_false_on_busy_error(
        self, coordinator: Coordinator
    ) -> None:
        with patch.object(coordinator._store, "get", side_effect=BusyError()):
            assert coordinator.is_registered("agent-unknown") is False
        # Cache and session set untouched by a failed lookup.
        assert "agent-unknown" not in coordinator._registered_agents
        assert "agent-unknown" not in coordinator._session_agents

    def test_is_registered_unknown_agent_returns_false(
        self, coordinator: Coordinator
    ) -> None:
        assert coordinator.is_registered("agent-nonexistent") is False
        assert "agent-nonexistent" not in coordinator._registered_agents
        assert "agent-nonexistent" not in coordinator._session_agents

    def test_session_agent_ids_returns_snapshot_tuple(
        self, coordinator: Coordinator
    ) -> None:
        a = coordinator.register("a")["agent_id"]
        b = coordinator.register("b")["agent_id"]
        snap = coordinator.session_agent_ids
        assert isinstance(snap, tuple)
        assert set(snap) == {a, b}
        # Snapshot is independent of subsequent mutation.
        coordinator._session_agents.discard(a)
        assert set(snap) == {a, b}


# ---------------------------------------------------------------------------
# Refresh agent
# ---------------------------------------------------------------------------


class TestRefreshAgent:
    def test_refresh_updates_last_activity(
        self, coordinator: Coordinator, store: StateStore
    ) -> None:
        result = coordinator.register("test-agent")
        agent_id = result["agent_id"]
        record_before = store.get("_agents", agent_id)
        old_activity = record_before.value["last_activity"]

        coordinator.refresh_agent(agent_id)

        record_after = store.get("_agents", agent_id)
        new_activity = record_after.value["last_activity"]
        assert new_activity >= old_activity

    def test_refresh_nonexistent_is_noop(self, coordinator: Coordinator) -> None:
        coordinator.refresh_agent("agent-nonexist")


# ---------------------------------------------------------------------------
# Release all  (disconnect cleanup → outcome=abandoned)
# ---------------------------------------------------------------------------


class TestReleaseAll:
    def test_release_all_releases_claims(self, coordinator: Coordinator) -> None:
        coordinator.claim("a.md", "agent-a")
        coordinator.claim("b.md", "agent-a")
        released = coordinator.release_all("agent-a")
        assert set(released) == {
            "file://default/a.md",
            "file://default/b.md",
        }
        assert coordinator.status("a.md")["status"] == "available"
        assert coordinator.status("b.md")["status"] == "available"

    def test_release_all_marks_abandoned(self, coordinator: Coordinator) -> None:
        coordinator.claim("a.md", "agent-a")
        coordinator.release_all("agent-a")
        status = coordinator.status("a.md")
        assert status["previous_outcome"] == "abandoned"
        assert "hint" in status
        assert "disconnected" in status["hint"]

    def test_release_all_skips_other_agents(self, coordinator: Coordinator) -> None:
        coordinator.claim("a.md", "agent-a")
        coordinator.claim("b.md", "agent-b")
        released = coordinator.release_all("agent-a")
        assert released == ["file://default/a.md"]
        assert coordinator.status("b.md")["status"] == "claimed"

    def test_release_all_empty(self, coordinator: Coordinator) -> None:
        released = coordinator.release_all("agent-a")
        assert released == []

    def test_subsequent_claim_sees_abandoned(self, coordinator: Coordinator) -> None:
        coordinator.claim("a.md", "agent-a")
        coordinator.release_all("agent-a")
        result = coordinator.claim("a.md", "agent-b")
        assert result["status"] == "claimed"
        assert result["previous_outcome"] == "abandoned"
        assert result["previous_holder"] == "agent-a"


# ---------------------------------------------------------------------------
# Deactivate agent
# ---------------------------------------------------------------------------


class TestDeactivateAgent:
    def test_deactivate_marks_inactive(
        self, coordinator: Coordinator, store: StateStore
    ) -> None:
        result = coordinator.register("test-agent")
        agent_id = result["agent_id"]
        coordinator.deactivate_agent(agent_id)
        record = store.get("_agents", agent_id)
        assert record.value["status"] == "inactive"
        assert "disconnected_at" in record.value

    def test_deactivate_nonexistent_is_noop(self, coordinator: Coordinator) -> None:
        coordinator.deactivate_agent("agent-nonexist")


# ---------------------------------------------------------------------------
# TTL / claim expiry
# ---------------------------------------------------------------------------


class TestClaimTTLInit:
    def test_negative_ttl_raises(
        self, store: StateStore, registry: WorkspaceRegistry
    ) -> None:
        with pytest.raises(ValueError, match="claim_ttl must be >= 0"):
            Coordinator(store, registry, claim_ttl=-1.0)

    def test_zero_ttl_allowed(
        self, store: StateStore, registry: WorkspaceRegistry
    ) -> None:
        coord = Coordinator(store, registry, claim_ttl=0.0)
        assert coord._claim_ttl == 0.0

    def test_none_ttl_allowed(
        self, store: StateStore, registry: WorkspaceRegistry
    ) -> None:
        coord = Coordinator(store, registry, claim_ttl=None)
        assert coord._claim_ttl is None


class TestClaimTTL:
    def test_claim_active_within_ttl(self, ttl_coordinator: Coordinator) -> None:
        result = ttl_coordinator.register("test-agent")
        agent_id = result["agent_id"]
        ttl_coordinator.claim("f.md", agent_id)
        state = ttl_coordinator.interpret_state("f.md")
        assert state["state"] == "claimed"

    def _backdate_agent(
        self,
        store: StateStore,
        agent_id: str,
        seconds_ago: int = 120,
    ) -> None:
        agent_record = store.get("_agents", agent_id)
        old_time = (datetime.now(UTC) - timedelta(seconds=seconds_ago)).isoformat()
        agent_record.value["last_activity"] = old_time
        store.set(
            "_agents",
            agent_id,
            agent_record.value,
            updated_by=agent_id,
            expected_version=agent_record.version,
        )

    def test_claim_expired_after_ttl(
        self, ttl_coordinator: Coordinator, store: StateStore
    ) -> None:
        result = ttl_coordinator.register("test-agent")
        agent_id = result["agent_id"]
        ttl_coordinator.claim("f.md", agent_id)
        self._backdate_agent(store, agent_id)
        state = ttl_coordinator.interpret_state("f.md")
        assert state["state"] == "expired"

    def test_expired_claim_can_be_taken(
        self, ttl_coordinator: Coordinator, store: StateStore
    ) -> None:
        result = ttl_coordinator.register("agent-1")
        agent1 = result["agent_id"]
        ttl_coordinator.claim("f.md", agent1)
        self._backdate_agent(store, agent1)
        result2 = ttl_coordinator.claim("f.md", "agent-2")
        assert result2["status"] == "claimed"
        # Synthesized previous_outcome=expired
        assert result2["previous_outcome"] == "expired"
        assert result2["previous_holder"] == agent1
        assert "hint" in result2

    def test_status_expired_shows_available_with_outcome(
        self, ttl_coordinator: Coordinator, store: StateStore
    ) -> None:
        result = ttl_coordinator.register("test-agent")
        agent_id = result["agent_id"]
        ttl_coordinator.claim("f.md", agent_id)
        self._backdate_agent(store, agent_id)
        status = ttl_coordinator.status("f.md")
        assert status["status"] == "available"
        assert status["previous_outcome"] == "expired"
        assert status["previous_holder"] == agent_id
        assert "hint" in status

    def test_no_ttl_claims_never_expire(self, coordinator: Coordinator) -> None:
        coordinator.claim("f.md", "agent-a")
        state = coordinator.interpret_state("f.md")
        assert state["state"] == "claimed"

    def test_is_claim_active_fallback_to_claimed_at(
        self, store: StateStore, registry: WorkspaceRegistry
    ) -> None:
        coord = Coordinator(store, registry, claim_ttl=60.0)
        old_time = (datetime.now(UTC) - timedelta(seconds=120)).isoformat()
        claim_value = {
            "status": "claimed",
            "by": "ghost-agent",
            "at": old_time,
        }
        assert not coord._is_claim_active(claim_value)

    def test_is_claim_active_recent_claimed_at(
        self, store: StateStore, registry: WorkspaceRegistry
    ) -> None:
        coord = Coordinator(store, registry, claim_ttl=60.0)
        recent_time = datetime.now(UTC).isoformat()
        claim_value = {
            "status": "claimed",
            "by": "ghost-agent",
            "at": recent_time,
        }
        assert coord._is_claim_active(claim_value)


# ---------------------------------------------------------------------------
# Status enrichment with agent info
# ---------------------------------------------------------------------------


class TestStatusEnrichment:
    def test_status_includes_agent_metadata(self, coordinator: Coordinator) -> None:
        result = coordinator.register("editor", model="claude-sonnet-4-6")
        agent_id = result["agent_id"]
        coordinator.claim("f.md", agent_id)
        status = coordinator.status("f.md")
        assert status["agent_name"] == "editor"
        assert status["agent_model"] == "claude-sonnet-4-6"

    def test_status_without_agent_record(self, coordinator: Coordinator) -> None:
        coordinator.claim("f.md", "manual-agent")
        status = coordinator.status("f.md")
        assert status["status"] == "claimed"
        assert "agent_name" not in status


# ---------------------------------------------------------------------------
# Multi-workspace coordination
# ---------------------------------------------------------------------------


class TestMultiWorkspace:
    @pytest.fixture
    def multi_coord(self, store: StateStore) -> Coordinator:
        registry = WorkspaceRegistry(
            [
                Workspace(name="default", root="/work"),
                Workspace(name="other", root="/elsewhere"),
            ]
        )
        return Coordinator(store, registry)

    def test_same_path_different_workspaces_isolated(
        self, multi_coord: Coordinator
    ) -> None:
        multi_coord.claim("file://default/foo.py", "agent-a")
        result = multi_coord.claim("file://other/foo.py", "agent-b")
        assert result["status"] == "claimed"

    def test_bare_path_uses_default(self, multi_coord: Coordinator) -> None:
        result = multi_coord.claim("foo.py", "agent-a")
        assert result["resource"] == "file://default/foo.py"

    def test_cross_workspace_move(self, multi_coord: Coordinator) -> None:
        multi_coord.claim("file://default/foo.py", "agent-a")
        result = multi_coord.release(
            "file://default/foo.py",
            "agent-a",
            outcome="moved",
            moved_to="file://other/foo.py",
        )
        assert result["moved_to"] == "file://other/foo.py"


# ---------------------------------------------------------------------------
# Move scenario — realistic rename flow
# ---------------------------------------------------------------------------


class TestRenameFlow:
    def test_canonical_rename_pattern(self, coordinator: Coordinator) -> None:
        # 1. Claim source
        a = coordinator.claim("old.py", "agent-a")
        assert a["status"] == "claimed"
        # 2. Claim destination
        b = coordinator.claim("new.py", "agent-a")
        assert b["status"] == "claimed"
        # 3. (rename happens on disk)
        # 4. Release source with moved
        r1 = coordinator.release(
            "old.py", "agent-a", outcome="moved", moved_to="new.py"
        )
        assert r1["outcome"] == "moved"
        # 5. Release destination as created
        r2 = coordinator.release("new.py", "agent-a", outcome="created")
        assert r2["outcome"] == "created"
        # 6. Curious agent claims old.py — sees the move
        c = coordinator.claim("old.py", "agent-b")
        assert c["previous_outcome"] == "moved"
        assert c["moved_to"] == "file://default/new.py"
        # 7. Following the move, claims new.py — sees creation
        coordinator.release("old.py", "agent-b")
        d = coordinator.claim("new.py", "agent-b")
        assert d["previous_outcome"] == "created"


# ---------------------------------------------------------------------------
# Custom scope smoke tests
# ---------------------------------------------------------------------------


class TestCustomScope:
    def test_custom_claim_release(self, coordinator: Coordinator) -> None:
        result = coordinator.claim("custom://task-42", "agent-a")
        assert result["status"] == "claimed"
        assert result["resource"] == "custom://task-42"
        result = coordinator.release("custom://task-42", "agent-a", outcome="modified")
        assert result["outcome"] == "modified"

    def test_custom_busy(self, coordinator: Coordinator) -> None:
        coordinator.claim("custom://task-42", "agent-a")
        result = coordinator.claim("custom://task-42", "agent-b")
        assert result["status"] == "busy"
