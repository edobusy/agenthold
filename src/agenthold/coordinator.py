"""
Claim-based coordination layer built on top of StateStore.

Implements a resource claim lifecycle (unclaimed → claimed → free → claimed → ...)
using the generic get/set primitives with OCC. This is the only module that knows
about claim semantics — the store layer remains generic.

The five high-level operations:
  register : register an agent and receive a unique ID
  claim    : acquire exclusive access to a resource
  release  : relinquish a claim after finishing work
  status   : check whether a resource is available or held
  wait     : (async, lives in server.py) poll until a resource becomes free
"""

import re
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from agenthold.exceptions import BusyError, ConflictError, NotFoundError
from agenthold.store import StateStore, _validate_identifier


class Coordinator:
    """High-level claim coordination on top of StateStore."""

    NAMESPACE = "claims"
    AGENTS_NAMESPACE = "_agents"

    def __init__(
        self,
        store: StateStore,
        claim_ttl: float | None = None,
    ) -> None:
        if claim_ttl is not None and claim_ttl < 0:
            raise ValueError("claim_ttl must be >= 0")
        self._store = store
        self._claim_ttl = claim_ttl
        self._registered_agents: set[str] = set()

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _now() -> str:
        return datetime.now(UTC).isoformat()

    @staticmethod
    def _normalize_resource(resource: str) -> str:
        """Normalize a resource identifier for consistent keying.

        1. Backslashes → forward slashes
        2. Collapse consecutive slashes
        3. Strip leading ./
        """
        resource = resource.replace("\\", "/")
        resource = re.sub(r"/+", "/", resource)
        resource = re.sub(r"^(\./)+", "", resource)
        return resource

    def _validate_inputs(self, resource: str, agent: str | None = None) -> str:
        """Validate and normalize inputs. Returns the normalized resource."""
        _validate_identifier(resource, "resource")
        if agent is not None:
            _validate_identifier(agent, "agent")
        normalized = self._normalize_resource(resource)
        if not normalized:
            raise ValueError("resource must not be empty after normalization")
        return normalized

    @staticmethod
    def _is_claim_value(value: Any) -> bool:
        """Check whether a stored value is a well-formed claim dict."""
        return isinstance(value, dict) and "status" in value

    def _is_claim_active(self, value: dict[str, Any]) -> bool:
        """Check if the agent holding a claim is still active.

        Returns True if:
        - TTL is disabled (claim_ttl is None)
        - The agent's last_activity is within the TTL window
        - Falling back to claimed_at if agent record is missing

        Returns False if the claim has expired.
        """
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

        return False  # Can't determine — treat as expired

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

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def interpret_state(self, resource: str) -> dict[str, Any]:
        """Read and interpret claim state for a resource.

        Used by both status() and the async wait loop in server.py.

        Returns one of:
        - {"state": "unclaimed", "resource": <normalized>}
        - {"state": "free", ...}
        - {"state": "claimed", ...}
        - {"state": "expired", ...}  (claimed but TTL elapsed)
        """
        key = self._validate_inputs(resource)
        try:
            record = self._store.get(self.NAMESPACE, key)
        except NotFoundError:
            return {"state": "unclaimed", "resource": key}

        value = record.value
        if not self._is_claim_value(value):
            return {"state": "unclaimed", "resource": key}

        if value.get("status") == "free":
            return {
                "state": "free",
                "resource": key,
                "last_held_by": value.get("released_by", "unknown"),
                "released_at": value.get("at", "unknown"),
                "version": record.version,
            }

        if value.get("status") == "claimed":
            if self._is_claim_active(value):
                return {
                    "state": "claimed",
                    "resource": key,
                    "held_by": value.get("by", "unknown"),
                    "claimed_at": value.get("at", "unknown"),
                    "version": record.version,
                }
            return {
                "state": "expired",
                "resource": key,
                "held_by": value.get("by", "unknown"),
                "claimed_at": value.get("at", "unknown"),
                "version": record.version,
            }

        # Unknown status field value — treat as unclaimed.
        return {"state": "unclaimed", "resource": key}

    def claim(self, resource: str, agent: str) -> dict[str, Any]:
        """Claim exclusive access to a resource."""
        key = self._validate_inputs(resource, agent)
        now = self._now()
        claim_value = {"status": "claimed", "by": agent, "at": now}

        try:
            record = self._store.get(self.NAMESPACE, key)
        except NotFoundError:
            # UNCLAIMED — first claim, use expected_version=0
            return self._attempt_claim(key, claim_value, agent, expected_version=0)
        except BusyError:
            raise

        value = record.value

        # Malformed or unknown status → treat as unclaimed, overwrite
        if not self._is_claim_value(value):
            return self._attempt_claim(
                key, claim_value, agent, expected_version=record.version
            )

        status = value.get("status")

        if status == "free":
            return self._attempt_claim(
                key, claim_value, agent, expected_version=record.version
            )

        if status == "claimed":
            if value.get("by") == agent:
                return {
                    "status": "already_claimed",
                    "resource": key,
                    "version": record.version,
                }
            # Check if the claim has expired (TTL elapsed)
            if not self._is_claim_active(value):
                return self._attempt_claim(
                    key,
                    claim_value,
                    agent,
                    expected_version=record.version,
                )
            return {
                "status": "busy",
                "resource": key,
                "held_by": value.get("by", "unknown"),
                "claimed_at": value.get("at", "unknown"),
                "version": record.version,
                "hint": (
                    "Another agent holds this resource. Work on a "
                    "different resource, or call agenthold_wait to "
                    "be notified when it becomes available."
                ),
            }

        # Unknown status value → treat as unclaimed
        return self._attempt_claim(
            key, claim_value, agent, expected_version=record.version
        )

    def release(self, resource: str, agent: str) -> dict[str, Any]:
        """Release a claim on a resource."""
        key = self._validate_inputs(resource, agent)
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

        if status == "claimed":
            if value.get("by") != agent:
                return {
                    "status": "error",
                    "message": (
                        f"Resource is claimed by {value.get('by')}, not {agent}"
                    ),
                }

            free_value = {
                "status": "free",
                "released_by": agent,
                "at": now,
            }
            try:
                result = self._store.set(
                    self.NAMESPACE,
                    key,
                    free_value,
                    updated_by=agent,
                    expected_version=record.version,
                )
            except ConflictError:
                # Should not normally happen — only the holder releases.
                # Re-read and return diagnostic info.
                try:
                    current = self._store.get(self.NAMESPACE, key)
                    return {
                        "status": "error",
                        "message": (
                            "Conflict while releasing. Current state may have changed."
                        ),
                        "current_version": current.version,
                        "current_value": current.value,
                    }
                except NotFoundError:
                    return {"status": "not_found", "resource": key}
                except BusyError:
                    return {
                        "status": "error",
                        "message": (
                            "Conflict while releasing and the database is "
                            "temporarily locked. Retry after a short delay."
                        ),
                    }
            return {
                "status": "released",
                "resource": key,
                "version": result.version,
            }

        # Unknown status → treat as not claimable
        return {"status": "not_found", "resource": key}

    def status(self, resource: str) -> dict[str, Any]:
        """Check whether a resource is available or claimed."""
        key = self._validate_inputs(resource)
        state = self.interpret_state(key)

        if state["state"] == "unclaimed":
            return {"status": "available", "resource": key}

        if state["state"] == "free":
            return {
                "status": "available",
                "resource": key,
                "last_held_by": state["last_held_by"],
                "released_at": state["released_at"],
            }

        if state["state"] == "expired":
            return {
                "status": "available",
                "resource": key,
                "note": (f"previous claim by {state['held_by']} expired"),
            }

        # claimed — enrich with agent metadata
        result: dict[str, Any] = {
            "status": "claimed",
            "resource": key,
            "held_by": state["held_by"],
            "claimed_at": state["claimed_at"],
            "version": state["version"],
        }
        info = self._get_agent_info(state["held_by"])
        if info:
            result["agent_name"] = info["agent_name"]
            result["agent_model"] = info["agent_model"]
        return result

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
        self._registered_agents.add(agent_id)
        return {
            "status": "registered",
            "agent_id": agent_id,
            "name": name,
            "registered_at": now,
        }

    def is_registered(self, agent_id: str) -> bool:
        """Check if an agent_id was registered on this coordinator."""
        return agent_id in self._registered_agents

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
        """Release all claims held by an agent.

        Best-effort: skips claims that can't be released due to
        conflicts or DB locks. Returns list of released resources.
        """
        released: list[str] = []
        try:
            records = self._store.list_keys(self.NAMESPACE)
        except BusyError:
            return released
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
                    "at": self._now(),
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

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _attempt_claim(
        self,
        key: str,
        claim_value: dict[str, str],
        agent: str,
        expected_version: int,
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
            return {
                "status": "claimed",
                "resource": key,
                "version": result.version,
            }
        except ConflictError:
            # Another agent won the race. Re-read to get winner's details.
            try:
                record = self._store.get(self.NAMESPACE, key)
                value = record.value
                if self._is_claim_value(value):
                    return {
                        "status": "busy",
                        "resource": key,
                        "held_by": value.get("by", "unknown"),
                        "claimed_at": value.get("at", "unknown"),
                        "version": record.version,
                        "hint": (
                            "Another agent holds this resource. Work on a "
                            "different resource, or call agenthold_wait to "
                            "be notified when it becomes available."
                        ),
                    }
                # Winner wrote a non-claim value (mode mixing) — treat as
                # busy with limited info.
                return {
                    "status": "busy",
                    "resource": key,
                    "held_by": "unknown",
                    "claimed_at": "unknown",
                    "version": record.version,
                    "hint": (
                        "Another agent holds this resource. Work on a "
                        "different resource, or call agenthold_wait to "
                        "be notified when it becomes available."
                    ),
                }
            except NotFoundError:
                # Extremely unlikely: winner claimed then deleted between
                # our conflict and re-read. Treat as available — caller
                # can retry claim.
                return {
                    "status": "error",
                    "message": (
                        "Conflict occurred but resource no longer exists. "
                        "Retry the claim."
                    ),
                }
            except BusyError:
                # Database locked during re-read after conflict.
                # We know someone else won, but can't get their details.
                return {
                    "status": "busy",
                    "resource": key,
                    "held_by": "unknown",
                    "claimed_at": "unknown",
                    "hint": (
                        "Another agent claimed this resource but the "
                        "database is temporarily locked. Work on a "
                        "different resource, or retry after a short delay."
                    ),
                }
