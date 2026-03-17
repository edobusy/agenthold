"""
Claim-based coordination layer built on top of StateStore.

Implements a resource claim lifecycle (unclaimed → claimed → free → claimed → ...)
using the generic get/set primitives with OCC. This is the only module that knows
about claim semantics — the store layer remains generic.

The four high-level operations:
  claim   : acquire exclusive access to a resource
  release : relinquish a claim after finishing work
  status  : check whether a resource is available or held
  wait    : (async, lives in server.py) poll until a resource becomes free
"""

import re
from datetime import UTC, datetime
from typing import Any

from agenthold.exceptions import BusyError, ConflictError, NotFoundError
from agenthold.store import StateStore, _validate_identifier


class Coordinator:
    """High-level claim coordination on top of StateStore."""

    NAMESPACE = "claims"

    def __init__(self, store: StateStore) -> None:
        self._store = store

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

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def interpret_state(self, resource: str) -> dict[str, Any]:
        """Read and interpret claim state for a resource.

        Used by both status() and the async wait loop in server.py.

        Returns one of:
        - {"state": "unclaimed", "resource": <normalized>}
        - {"state": "free", "resource": <normalized>,
           "last_held_by": str, "released_at": str, "version": int}
        - {"state": "claimed", "resource": <normalized>,
           "held_by": str, "claimed_at": str, "version": int}
        """
        key = self._normalize_resource(resource)
        try:
            record = self._store.get(self.NAMESPACE, key)
        except NotFoundError:
            return {"state": "unclaimed", "resource": key}

        value = record.value
        if not self._is_claim_value(value):
            # Malformed value (e.g. from advanced-mode mixing) — treat
            # as unclaimed so the next claim overwrites it.
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
            return {
                "state": "claimed",
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

        # claimed
        return {
            "status": "claimed",
            "resource": key,
            "held_by": state["held_by"],
            "claimed_at": state["claimed_at"],
            "version": state["version"],
        }

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
