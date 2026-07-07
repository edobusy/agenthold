"""
Claim-based coordination layer built on top of StateStore.

Implements a resource claim lifecycle with explicit lifecycle outcomes:

    unclaimed → claimed → free(outcome) → claimed → free(outcome) → ...

Outcomes carried on release tell the next claimant what happened to the
underlying entity (modified, created, deleted, moved, abandoned, expired).
This is how agenthold solves the name-vs-entity problem: the claim coordinates
access to a *name*, but the holder declares what they did to the entity, and
that declaration is preserved for the next holder to see.

Resources are identified by canonical URIs built from the configured
workspaces (see resources.py). All public methods accept the raw string an
agent provides; canonicalization happens at the boundary.

The five high-level operations:
  register : register an agent and receive a unique ID
  claim    : acquire exclusive access to a resource
  release  : relinquish a claim with an explicit outcome
  status   : check whether a resource is available or held
  wait     : (async, lives in server.py) poll until a resource becomes free
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from agenthold.exceptions import BusyError, ConflictError, NotFoundError
from agenthold.resources import (
    ResourceId,
    WorkspaceRegistry,
    parse_resource_input,
)
from agenthold.store import StateStore, _validate_identifier

# Outcomes the agent may declare on release.
AGENT_OUTCOMES: frozenset[str] = frozenset(
    {"released", "modified", "created", "deleted", "moved"}
)
# All outcome values that can appear in stored free-state records or in
# previous_outcome fields. abandoned/expired are server-set.
ALL_OUTCOMES: frozenset[str] = AGENT_OUTCOMES | frozenset({"abandoned", "expired"})

# Outcomes that warrant a hint string for the next claimant.
_NOISY_OUTCOMES: frozenset[str] = frozenset(
    {"deleted", "moved", "abandoned", "expired"}
)

_HINT_TEXT: dict[str, str] = {
    "deleted": (
        "The previous holder deleted this resource. The path is yours, "
        "but the file may not exist on disk."
    ),
    "moved": (
        "The previous holder moved this resource. If you wanted to follow "
        "the move, claim 'moved_to' instead."
    ),
    "abandoned": (
        "The previous holder disconnected without explicitly releasing. "
        "The disk state is unknown."
    ),
    "expired": (
        "The previous holder's claim expired before they released. "
        "They may have left the resource in an inconsistent state."
    ),
}


class Coordinator:
    """High-level claim coordination on top of StateStore."""

    NAMESPACE = "claims"
    AGENTS_NAMESPACE = "_agents"

    def __init__(
        self,
        store: StateStore,
        registry: WorkspaceRegistry,
        claim_ttl: float | None = None,
    ) -> None:
        if claim_ttl is not None and not claim_ttl > 0:
            # Rejects 0 (every claim instantly reclaimable), negatives, and NaN
            # (NaN > 0 is False, so `not (NaN > 0)` is True).
            raise ValueError("claim_ttl must be a positive number")
        self._store = store
        self._registry = registry
        self._claim_ttl = claim_ttl
        # Recognition cache: agents this Coordinator has seen, either via
        # register() in this process or via DB fallback in is_registered().
        self._registered_agents: set[str] = set()
        # Session set: agents this Coordinator's register() created during
        # this process lifetime. Used by _cleanup_agents to scope cleanup
        # so we never release/deactivate agents owned by other processes.
        self._session_agents: set[str] = set()

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @property
    def registry(self) -> WorkspaceRegistry:
        return self._registry

    @staticmethod
    def _now() -> str:
        return datetime.now(UTC).isoformat()

    def canonicalize(self, raw: Any) -> ResourceId:
        """Parse a raw resource input into a canonical ResourceId."""
        return parse_resource_input(raw, self._registry)

    @staticmethod
    def _is_claim_value(value: Any) -> bool:
        """Check whether a stored value is a well-formed claim dict."""
        return isinstance(value, dict) and "status" in value

    def _is_claim_active(self, value: dict[str, Any]) -> bool:
        """Check if the agent holding a claim is still active."""
        if self._claim_ttl is None:
            return True

        agent_id = value.get("by")
        if not agent_id:
            return False

        # Primary: check agent's last_activity
        try:
            agent_record = self._store.get(self.AGENTS_NAMESPACE, agent_id)
            last_activity = agent_record.value.get("last_activity")
            if last_activity:
                deadline = datetime.fromisoformat(last_activity) + timedelta(
                    seconds=self._claim_ttl
                )
                return deadline > datetime.now(UTC)
        except (NotFoundError, BusyError):
            pass
        except (ValueError, TypeError, AttributeError):
            pass

        # Fallback: use claimed_at from the claim itself
        claimed_at = value.get("at")
        if claimed_at:
            try:
                deadline = datetime.fromisoformat(claimed_at) + timedelta(
                    seconds=self._claim_ttl
                )
                return deadline > datetime.now(UTC)
            except (ValueError, TypeError):
                pass

        return False

    def _get_agent_info(self, agent_id: str) -> dict[str, str] | None:
        """Look up agent metadata from the _agents namespace."""
        try:
            record = self._store.get(self.AGENTS_NAMESPACE, agent_id)
            if not isinstance(record.value, dict):
                return None
            return {
                "agent_name": record.value.get("name", ""),
                "agent_model": record.value.get("model", ""),
            }
        except (NotFoundError, BusyError):
            return None

    @staticmethod
    def _hint_for_outcome(outcome: str | None) -> str | None:
        if outcome is None:
            return None
        return _HINT_TEXT.get(outcome)

    @staticmethod
    def _outcome_info_from_free_value(value: dict[str, Any]) -> dict[str, Any]:
        """Build previous_outcome fields from a stored free-state record."""
        outcome = value.get("outcome", "released")
        info: dict[str, Any] = {
            "previous_outcome": outcome,
            "previous_holder": value.get("released_by", "unknown"),
            "previous_outcome_at": value.get("at", "unknown"),
        }
        if outcome == "moved":
            moved_to = value.get("moved_to")
            if moved_to is not None:
                info["moved_to"] = moved_to
        return info

    # ------------------------------------------------------------------
    # interpret_state
    # ------------------------------------------------------------------

    def interpret_state(self, raw: Any) -> dict[str, Any]:
        """Read and interpret claim state for a resource.

        Used by both status() and the async wait loop in server.py.
        Returns a dict with a "state" key naming one of:
            unclaimed | free | claimed | expired
        plus the canonical resource URI and state-specific fields.
        """
        rid = self.canonicalize(raw)
        return self._interpret_state_by_uri(rid.to_uri())

    def _interpret_state_by_uri(self, key: str) -> dict[str, Any]:
        try:
            record = self._store.get(self.NAMESPACE, key)
        except NotFoundError:
            return {"state": "unclaimed", "resource": key}

        value = record.value
        if not self._is_claim_value(value):
            return {"state": "unclaimed", "resource": key}

        status = value.get("status")

        if status == "free":
            outcome = value.get("outcome", "released")
            info: dict[str, Any] = {
                "state": "free",
                "resource": key,
                "released_by": value.get("released_by", "unknown"),
                "released_at": value.get("at", "unknown"),
                "outcome": outcome,
                "version": record.version,
            }
            if outcome == "moved":
                moved_to = value.get("moved_to")
                if moved_to is not None:
                    info["moved_to"] = moved_to
            return info

        if status == "claimed":
            held_by = value.get("by", "unknown")
            claimed_at = value.get("at", "unknown")
            if self._is_claim_active(value):
                return {
                    "state": "claimed",
                    "resource": key,
                    "held_by": held_by,
                    "claimed_at": claimed_at,
                    "version": record.version,
                }
            return {
                "state": "expired",
                "resource": key,
                "held_by": held_by,
                "claimed_at": claimed_at,
                "version": record.version,
            }

        return {"state": "unclaimed", "resource": key}

    # ------------------------------------------------------------------
    # claim
    # ------------------------------------------------------------------

    def claim(self, raw: Any, agent: str) -> dict[str, Any]:
        """Claim exclusive access to a resource."""
        rid = self.canonicalize(raw)
        _validate_identifier(agent, "agent")
        key = rid.to_uri()
        now = self._now()
        claim_value = {"status": "claimed", "by": agent, "at": now}

        try:
            record = self._store.get(self.NAMESPACE, key)
        except NotFoundError:
            return self._attempt_claim(
                key, claim_value, agent, expected_version=0, prior_info=None
            )
        except BusyError:
            raise

        value = record.value

        # Malformed value → treat as unclaimed
        if not self._is_claim_value(value):
            return self._attempt_claim(
                key,
                claim_value,
                agent,
                expected_version=record.version,
                prior_info=None,
            )

        status = value.get("status")

        if status == "free":
            prior_info = self._outcome_info_from_free_value(value)
            return self._attempt_claim(
                key,
                claim_value,
                agent,
                expected_version=record.version,
                prior_info=prior_info,
            )

        if status == "claimed":
            if value.get("by") == agent:
                return {
                    "status": "already_claimed",
                    "resource": key,
                    "version": record.version,
                }
            if not self._is_claim_active(value):
                # TTL takeover — synthesize previous_outcome=expired
                prior_info = {
                    "previous_outcome": "expired",
                    "previous_holder": value.get("by", "unknown"),
                    "previous_outcome_at": value.get("at", "unknown"),
                }
                return self._attempt_claim(
                    key,
                    claim_value,
                    agent,
                    expected_version=record.version,
                    prior_info=prior_info,
                )
            return self._busy_response(key, value, record.version)

        # Unknown status → treat as unclaimed
        return self._attempt_claim(
            key,
            claim_value,
            agent,
            expected_version=record.version,
            prior_info=None,
        )

    def _attempt_claim(
        self,
        key: str,
        claim_value: dict[str, str],
        agent: str,
        expected_version: int,
        prior_info: dict[str, Any] | None,
    ) -> dict[str, Any]:
        """Try to write a claim. On conflict, re-read and return busy."""
        try:
            result = self._store.set(
                self.NAMESPACE,
                key,
                claim_value,
                updated_by=agent,
                expected_version=expected_version,
            )
        except ConflictError:
            return self._handle_claim_conflict(key)

        response: dict[str, Any] = {
            "status": "claimed",
            "resource": key,
            "version": result.version,
        }
        if prior_info:
            response.update(prior_info)
            hint = self._hint_for_outcome(prior_info.get("previous_outcome"))
            if hint:
                response["hint"] = hint
        return response

    def _handle_claim_conflict(self, key: str) -> dict[str, Any]:
        """Recover from a ConflictError during _attempt_claim."""
        try:
            record = self._store.get(self.NAMESPACE, key)
        except NotFoundError:
            return {
                "status": "error",
                "message": (
                    "Conflict occurred but resource no longer exists. Retry the claim."
                ),
            }
        except BusyError:
            # We know a conflict happened but can't read who holds it (lock).
            # 'unavailable' (retry), not 'busy' — 'busy' always names held_by.
            return {
                "status": "unavailable",
                "resource": key,
                "hint": (
                    "A conflict occurred but the database is temporarily "
                    "locked; retry the claim after a short delay to get a "
                    "definitive result."
                ),
            }
        value = record.value
        if self._is_claim_value(value):
            return self._busy_response(key, value, record.version)
        # Conflict, but the current value is not a well-formed claim (no known
        # holder) — retry rather than report a holderless 'busy'.
        return {
            "status": "unavailable",
            "resource": key,
            "version": record.version,
            "hint": (
                "A conflict occurred and the current state is unexpected; "
                "retry the claim to get a definitive result."
            ),
        }

    @staticmethod
    def _busy_response(key: str, value: dict[str, Any], version: int) -> dict[str, Any]:
        return {
            "status": "busy",
            "resource": key,
            "held_by": value.get("by", "unknown"),
            "claimed_at": value.get("at", "unknown"),
            "version": version,
            "hint": (
                "Another agent holds this resource. Work on a different "
                "resource, or call agenthold_wait to be notified when it "
                "becomes available."
            ),
        }

    # ------------------------------------------------------------------
    # release
    # ------------------------------------------------------------------

    def release(
        self,
        raw: Any,
        agent: str,
        outcome: str = "released",
        moved_to: Any = None,
    ) -> dict[str, Any]:
        """Release a claim with an explicit lifecycle outcome.

        Allowed outcomes: released, modified, created, deleted, moved.
        For outcome=moved, moved_to must be provided as a resource string;
        for any other outcome, moved_to must be omitted.
        """
        rid = self.canonicalize(raw)
        _validate_identifier(agent, "agent")
        key = rid.to_uri()

        # Validate outcome
        if outcome not in AGENT_OUTCOMES:
            raise ValueError(
                f"Invalid outcome {outcome!r}. Allowed: {sorted(AGENT_OUTCOMES)}"
            )

        # Validate moved_to vs outcome
        moved_to_uri: str | None = None
        if outcome == "moved":
            if moved_to is None:
                raise ValueError("moved_to is required when outcome='moved'")
            target_rid = self.canonicalize(moved_to)
            target_uri = target_rid.to_uri()
            if target_uri == key:
                raise ValueError("moved_to must differ from the source resource")
            moved_to_uri = target_uri
        else:
            if moved_to is not None:
                raise ValueError(
                    f"moved_to is only allowed with outcome='moved' "
                    f"(got outcome={outcome!r})"
                )

        return self._do_release(key, agent, outcome, moved_to_uri)

    def _do_release(
        self,
        key: str,
        agent: str,
        outcome: str,
        moved_to_uri: str | None,
    ) -> dict[str, Any]:
        now = self._now()
        try:
            record = self._store.get(self.NAMESPACE, key)
        except NotFoundError:
            return {"status": "not_found", "resource": key}
        except BusyError:
            raise

        value = record.value
        if not self._is_claim_value(value):
            return {"status": "not_found", "resource": key}

        status = value.get("status")

        if status == "free":
            return {"status": "already_free", "resource": key}

        if status != "claimed":
            return {"status": "not_found", "resource": key}

        if value.get("by") != agent:
            return {
                "status": "error",
                "message": (f"Resource is claimed by {value.get('by')}, not {agent}"),
                "held_by": value.get("by"),
                "hint": (
                    "You do not hold this claim; only the holder can release it. "
                    "Do not retry with your agent_id."
                ),
            }

        free_value: dict[str, Any] = {
            "status": "free",
            "released_by": agent,
            "at": now,
            "outcome": outcome,
        }
        if moved_to_uri is not None:
            free_value["moved_to"] = moved_to_uri

        try:
            result = self._store.set(
                self.NAMESPACE,
                key,
                free_value,
                updated_by=agent,
                expected_version=record.version,
            )
        except ConflictError:
            try:
                current = self._store.get(self.NAMESPACE, key)
                return {
                    "status": "error",
                    "message": (
                        "Conflict while releasing. Current state may have changed."
                    ),
                    "current_version": current.version,
                    "current_value": current.value,
                    "hint": (
                        "Transient conflict — re-read status and retry the release."
                    ),
                }
            except NotFoundError:
                return {"status": "not_found", "resource": key}
            except BusyError:
                # Conflict, then the re-read was DB-locked: transient, retry.
                return {
                    "status": "unavailable",
                    "resource": key,
                    "message": (
                        "Conflict while releasing and the database is "
                        "temporarily locked."
                    ),
                    "hint": "Transient — re-read status and retry the release.",
                }

        response: dict[str, Any] = {
            "status": "released",
            "resource": key,
            "version": result.version,
            "outcome": outcome,
        }
        if moved_to_uri is not None:
            response["moved_to"] = moved_to_uri
        return response

    # ------------------------------------------------------------------
    # status
    # ------------------------------------------------------------------

    def status(self, raw: Any) -> dict[str, Any]:
        """Check whether a resource is available or claimed."""
        state = self.interpret_state(raw)
        key = state["resource"]

        if state["state"] == "unclaimed":
            return {"status": "available", "resource": key}

        if state["state"] == "free":
            outcome = state["outcome"]
            response: dict[str, Any] = {
                "status": "available",
                "resource": key,
                "previous_outcome": outcome,
                "previous_holder": state["released_by"],
                "previous_outcome_at": state["released_at"],
            }
            if outcome == "moved" and state.get("moved_to"):
                response["moved_to"] = state["moved_to"]
            hint = self._hint_for_outcome(outcome)
            if hint:
                response["hint"] = hint
            return response

        if state["state"] == "expired":
            response = {
                "status": "available",
                "resource": key,
                "previous_outcome": "expired",
                "previous_holder": state["held_by"],
                "previous_outcome_at": state["claimed_at"],
            }
            hint = self._hint_for_outcome("expired")
            if hint:
                response["hint"] = hint
            return response

        # claimed — enrich with agent metadata
        claimed_response: dict[str, Any] = {
            "status": "claimed",
            "resource": key,
            "held_by": state["held_by"],
            "claimed_at": state["claimed_at"],
            "version": state["version"],
        }
        info = self._get_agent_info(state["held_by"])
        if info:
            claimed_response["agent_name"] = info["agent_name"]
            claimed_response["agent_model"] = info["agent_model"]
        return claimed_response

    # ------------------------------------------------------------------
    # Registration
    # ------------------------------------------------------------------

    def register(
        self,
        name: str,
        model: str = "",
        purpose: str = "",
    ) -> dict[str, Any]:
        """Register a new agent. Returns agent_id and metadata."""
        if not isinstance(name, str) or not name.strip():
            raise ValueError("name must not be empty")
        if len(name) > 256:
            raise ValueError("name must not exceed 256 characters")
        agent_id = "agent-" + uuid.uuid4().hex[:8]
        now = self._now()
        value = {
            "name": name,
            "model": model,
            "purpose": purpose,
            "registered_at": now,
            "last_activity": now,
            "status": "active",
        }
        self._store.set(
            self.AGENTS_NAMESPACE,
            agent_id,
            value,
            updated_by=agent_id,
            expected_version=0,
        )
        self._track_registration(agent_id)
        return {
            "status": "registered",
            "agent_id": agent_id,
            "name": name,
            "registered_at": now,
        }

    def _track_registration(self, agent_id: str) -> None:
        """Add agent_id to both the recognition cache and the session set.

        Encapsulated so future maintenance cannot insert work between the
        two .add() calls and silently break the cleanup contract that
        _cleanup_agents iterates the same set register() populates.
        """
        self._registered_agents.add(agent_id)
        self._session_agents.add(agent_id)

    @property
    def session_agent_ids(self) -> tuple[str, ...]:
        """Snapshot of agent IDs registered through this Coordinator.

        Returns a tuple — immutable, safe to iterate while another thread
        calls register(). Used by server._cleanup_agents to scope the
        disconnect cleanup to agents this process owns.
        """
        return tuple(self._session_agents)

    def is_registered(self, agent_id: str) -> bool:
        """Check whether an agent_id is recognized.

        Fast path: in-memory cache. Slow path: lookup in the _agents
        namespace. Found agents are added to the recognition cache so
        subsequent calls skip the DB. Agents discovered via the slow
        path are NOT added to _session_agents — recognition is not the
        same as session ownership.

        Returns False on BusyError (DB temporarily locked). The store's
        5-second busy_timeout normally clears transient locks before this
        call returns; if it does not, the agent sees "Unknown agent_id"
        and may retry the original tool call.
        """
        if agent_id in self._registered_agents:
            return True
        try:
            record = self._store.get(self.AGENTS_NAMESPACE, agent_id)
        except (NotFoundError, BusyError):
            return False
        value = record.value
        if isinstance(value, dict) and value.get("status") == "active":
            self._registered_agents.add(agent_id)
            return True
        return False

    def refresh_agent(self, agent_id: str) -> None:
        """Update last_activity for an agent. Best-effort."""
        try:
            record = self._store.get(self.AGENTS_NAMESPACE, agent_id)
            value = dict(record.value)
            value["last_activity"] = self._now()
            self._store.set(
                self.AGENTS_NAMESPACE,
                agent_id,
                value,
                updated_by=agent_id,
                expected_version=record.version,
            )
        except (NotFoundError, ConflictError, BusyError):
            pass

    def release_all(self, agent_id: str) -> list[str]:
        """Release all claims held by an agent on disconnect.

        All released claims are marked outcome='abandoned' since the agent
        did not get a chance to declare a lifecycle outcome explicitly.
        """
        released: list[str] = []
        try:
            records = self._store.list_keys(self.NAMESPACE)
        except BusyError:
            return released
        now = self._now()
        for record in records:
            value = record.value
            if (
                isinstance(value, dict)
                and value.get("by") == agent_id
                and value.get("status") == "claimed"
            ):
                free_value = {
                    "status": "free",
                    "released_by": agent_id,
                    "at": now,
                    "outcome": "abandoned",
                }
                try:
                    self._store.set(
                        self.NAMESPACE,
                        record.key,
                        free_value,
                        updated_by=agent_id,
                        expected_version=record.version,
                    )
                    released.append(record.key)
                except (ConflictError, BusyError):
                    pass
        return released

    def deactivate_agent(self, agent_id: str) -> None:
        """Mark an agent as inactive. Best-effort."""
        try:
            record = self._store.get(self.AGENTS_NAMESPACE, agent_id)
            value = dict(record.value)
            value["status"] = "inactive"
            value["disconnected_at"] = self._now()
            self._store.set(
                self.AGENTS_NAMESPACE,
                agent_id,
                value,
                updated_by=agent_id,
                expected_version=record.version,
            )
        except (NotFoundError, ConflictError, BusyError):
            pass
