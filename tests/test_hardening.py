"""Tests for the pre-#5 hardening batch: CLI validation/warnings, the
transient-lock 'unavailable' status, and release-error detail."""

from __future__ import annotations

import argparse
from unittest.mock import patch

import pytest

from agenthold import server as srv
from agenthold.coordinator import Coordinator
from agenthold.exceptions import BusyError
from agenthold.resources import Workspace, WorkspaceRegistry
from agenthold.store import StateStore


def _coord() -> Coordinator:
    store = StateStore(":memory:")
    registry = WorkspaceRegistry([Workspace(name="default", root="/work")])
    return Coordinator(store, registry)


def _config_args(**kw: object) -> argparse.Namespace:
    ns = argparse.Namespace(workspace=None, claim_ttl=None)
    for key, value in kw.items():
        setattr(ns, key, value)
    return ns


# ---------------------------------------------------------------------------
# _validate_config_or_exit  (clean CLI errors instead of tracebacks)
# ---------------------------------------------------------------------------


def test_validate_config_ok() -> None:
    assert srv._validate_config_or_exit(_config_args())  # default workspace


def test_validate_config_bad_workspace_value() -> None:
    with pytest.raises(SystemExit) as ei:
        srv._validate_config_or_exit(_config_args(workspace=["foo="]))
    assert str(ei.value).startswith("error:")


def test_validate_config_duplicate_workspace_names() -> None:
    with pytest.raises(SystemExit) as ei:
        srv._validate_config_or_exit(_config_args(workspace=["a=/x", "a=/y"]))
    assert str(ei.value).startswith("error:")


def test_validate_config_bad_claim_ttl() -> None:
    for bad in (0.0, -3.0):
        with pytest.raises(SystemExit):
            srv._validate_config_or_exit(_config_args(claim_ttl=bad))


# ---------------------------------------------------------------------------
# _http_only_flags_active  (warn when HTTP flags set under stdio)
# ---------------------------------------------------------------------------


def test_http_flags_inactive_by_default() -> None:
    args = srv._build_arg_parser().parse_args([])
    assert srv._http_only_flags_active(args) is False


def test_http_flags_active_when_set() -> None:
    for argv in (
        ["--port", "9000"],
        ["--host", "0.0.0.0"],
        ["--path", "/x"],
        ["--json-response"],
        ["--allowed-host", "h"],
    ):
        args = srv._build_arg_parser().parse_args(argv)
        assert srv._http_only_flags_active(args) is True, argv


# ---------------------------------------------------------------------------
# 'unavailable' vs 'busy'
# ---------------------------------------------------------------------------


def test_standard_dispatch_busy_returns_unavailable() -> None:
    coord = _coord()
    with patch.object(coord, "register", side_effect=BusyError()):
        result = srv._dispatch_standard(coord, "agenthold_register", {"name": "a"})
    assert result["status"] == "unavailable"
    assert "hint" in result


def test_coordination_busy_still_reports_busy_with_holder() -> None:
    # A peer holding the claim must still be 'busy' (not 'unavailable').
    coord = _coord()
    a = coord.register(name="a")["agent_id"]
    b = coord.register(name="b")["agent_id"]
    coord.claim("custom://r", a)
    result = coord.claim("custom://r", b)
    assert result["status"] == "busy"
    assert result["held_by"] == a


# ---------------------------------------------------------------------------
# release-error detail (held_by + hint)
# ---------------------------------------------------------------------------


def test_claim_conflict_lock_returns_unavailable() -> None:
    # A conflict during claim where the re-read is DB-locked: cannot name a
    # holder, so 'unavailable' (retry), not a holderless 'busy'.
    coord = _coord()
    with patch.object(coord._store, "get", side_effect=BusyError()):
        result = coord._handle_claim_conflict("custom://x")
    assert result["status"] == "unavailable"


def test_claim_conflict_malformed_value_returns_unavailable() -> None:
    coord = _coord()
    coord._store.set("claims", "custom://y", "not-a-claim", updated_by="z")
    result = coord._handle_claim_conflict("custom://y")
    assert result["status"] == "unavailable"


def test_claim_conflict_real_holder_returns_busy_with_held_by() -> None:
    coord = _coord()
    coord._store.set(
        "claims",
        "custom://z",
        {"status": "claimed", "by": "agent-1", "at": "t"},
        updated_by="agent-1",
    )
    result = coord._handle_claim_conflict("custom://z")
    assert result["status"] == "busy" and result["held_by"] == "agent-1"


def test_release_by_wrong_agent_reports_held_by_and_hint() -> None:
    coord = _coord()
    a = coord.register(name="a")["agent_id"]
    b = coord.register(name="b")["agent_id"]
    coord.claim("custom://r", a)
    result = coord.release("custom://r", b, outcome="modified")
    assert result["status"] == "error"
    assert result["held_by"] == a
    assert "hint" in result
