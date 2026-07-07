"""Tests for the read-only CLI inspector (`agenthold agents/claims/...`)."""

from __future__ import annotations

import argparse
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from agenthold import inspector
from agenthold import server as srv
from agenthold.coordinator import Coordinator
from agenthold.resources import Workspace, WorkspaceRegistry
from agenthold.store import StateStore


def _populate(db_path: Path) -> str:
    """Create a db with two agents, active + moved claims, and a user record."""
    store = StateStore(str(db_path))
    registry = WorkspaceRegistry([Workspace(name="default", root="/work")])
    coord = Coordinator(store, registry)
    editor = coord.register(name="editor", model="opus")["agent_id"]
    coord.claim("custom://res-a", editor)
    coord.claim("custom://old", editor)
    coord.release("custom://old", editor, outcome="moved", moved_to="custom://new")
    store.set("orders", "o1", {"status": "paid"}, updated_by="intake")
    store.close()
    return editor


def _run(argv: list[str]) -> int:
    """Parse argv with the real server parser and dispatch to the inspector."""
    args = srv._build_arg_parser().parse_args(argv)
    return inspector.run(args)


# ---------------------------------------------------------------------------
# Backward compatibility with the server CLI
# ---------------------------------------------------------------------------


def test_server_invocations_have_no_command() -> None:
    parser = srv._build_arg_parser()
    for argv in ([], ["--db", "x"], ["--tools", "advanced"], ["--transport", "http"]):
        assert parser.parse_args(argv).command is None


def test_inspector_invocations_set_command() -> None:
    parser = srv._build_arg_parser()
    assert parser.parse_args(["agents"]).command == "agents"
    assert parser.parse_args(["claims", "--all"]).command == "claims"
    assert parser.parse_args(["keys", "orders"]).namespace == "orders"


# ---------------------------------------------------------------------------
# list_namespaces store method
# ---------------------------------------------------------------------------


def test_list_namespaces_distinct_sorted(store: StateStore) -> None:
    store.set("z", "k", 1, updated_by="a")
    store.set("a", "k", 1, updated_by="a")
    store.set("a", "k2", 1, updated_by="a")
    assert store.list_namespaces() == ["a", "z"]


def test_list_namespaces_empty(store: StateStore) -> None:
    assert store.list_namespaces() == []


def test_list_namespaces_excludes_fully_deleted(store: StateStore) -> None:
    store.set("ns", "k", 1, updated_by="a")
    store.delete("ns", "k", deleted_by="a", expected_version=1)
    assert store.list_namespaces() == []


# ---------------------------------------------------------------------------
# agents
# ---------------------------------------------------------------------------


def test_agents_text(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    db = tmp_path / "a.db"
    editor = _populate(db)
    rc = _run(["agents", "--db", str(db)])
    out = capsys.readouterr().out
    assert rc == 0
    assert "AGENT ID" in out and "editor" in out and editor in out


def test_agents_json(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    db = tmp_path / "a.db"
    _populate(db)
    rc = _run(["agents", "--db", str(db), "--json"])
    data = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert any(a["name"] == "editor" and a["model"] == "opus" for a in data)


def test_agents_empty(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    db = tmp_path / "e.db"
    StateStore(str(db)).close()
    rc = _run(["agents", "--db", str(db)])
    assert rc == 0
    assert "No registered agents." in capsys.readouterr().out


# ---------------------------------------------------------------------------
# claims
# ---------------------------------------------------------------------------


def test_claims_active_only(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    db = tmp_path / "c.db"
    _populate(db)
    rc = _run(["claims", "--db", str(db)])
    out = capsys.readouterr().out
    assert rc == 0
    assert "custom://res-a" in out and "editor" in out
    assert "custom://old" not in out  # freed claim hidden by default


def test_claims_all_shows_freed(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    db = tmp_path / "c.db"
    _populate(db)
    _run(["claims", "--db", str(db), "--all"])
    out = capsys.readouterr().out
    assert "custom://old" in out and "moved" in out and "custom://new" in out


def test_claims_json(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    db = tmp_path / "c.db"
    _populate(db)
    _run(["claims", "--db", str(db), "--json"])
    data = json.loads(capsys.readouterr().out)
    assert any(
        c["resource"] == "custom://res-a" and c["held_by_name"] == "editor"
        for c in data
    )


def test_claims_unknown_agent_falls_back_to_id(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    db = tmp_path / "c.db"
    store = StateStore(str(db))
    store.set(
        "claims",
        "custom://x",
        {
            "status": "claimed",
            "by": "agent-ffffffff",
            "at": "2026-07-07T00:00:00+00:00",
        },
        updated_by="agent-ffffffff",
    )
    store.close()
    _run(["claims", "--db", str(db)])
    assert "agent-ffffffff" in capsys.readouterr().out


def test_claims_skips_malformed(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    db = tmp_path / "c.db"
    store = StateStore(str(db))
    store.set("claims", "custom://bad", "not-a-dict", updated_by="x")
    store.close()
    rc = _run(["claims", "--db", str(db)])
    assert rc == 0
    assert "No active claims." in capsys.readouterr().out


def test_claims_empty(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    db = tmp_path / "e.db"
    StateStore(str(db)).close()
    _run(["claims", "--db", str(db)])
    assert "No active claims." in capsys.readouterr().out


# ---------------------------------------------------------------------------
# namespaces / keys / history
# ---------------------------------------------------------------------------


def test_namespaces_text(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    db = tmp_path / "n.db"
    _populate(db)
    _run(["namespaces", "--db", str(db)])
    out = capsys.readouterr().out
    assert "_agents" in out and "claims" in out and "orders" in out


def test_namespaces_json_counts(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    db = tmp_path / "n.db"
    _populate(db)
    _run(["namespaces", "--db", str(db), "--json"])
    data = {x["namespace"]: x["keys"] for x in json.loads(capsys.readouterr().out)}
    assert data["orders"] == 1 and data["_agents"] == 1


def test_namespaces_empty(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    db = tmp_path / "e.db"
    StateStore(str(db)).close()
    _run(["namespaces", "--db", str(db)])
    assert "No namespaces." in capsys.readouterr().out


def test_keys_text(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    db = tmp_path / "k.db"
    _populate(db)
    _run(["keys", "orders", "--db", str(db)])
    assert "o1" in capsys.readouterr().out


def test_keys_json(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    db = tmp_path / "k.db"
    _populate(db)
    _run(["keys", "orders", "--db", str(db), "--json"])
    data = json.loads(capsys.readouterr().out)
    assert data[0]["key"] == "o1" and data[0]["version"] == 1


def test_keys_missing_namespace(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    db = tmp_path / "k.db"
    _populate(db)
    rc = _run(["keys", "nope", "--db", str(db)])
    assert rc == 0
    assert "No keys" in capsys.readouterr().out


def test_history_text(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    db = tmp_path / "h.db"
    _populate(db)
    _run(["history", "orders", "o1", "--db", str(db)])
    out = capsys.readouterr().out
    assert "write" in out and "VERSION" in out


def test_history_json(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    db = tmp_path / "h.db"
    _populate(db)
    _run(["history", "orders", "o1", "--db", str(db), "--json"])
    data = json.loads(capsys.readouterr().out)
    assert data[0]["event_type"] == "write" and data[0]["version"] == 1


def test_history_missing_key(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    db = tmp_path / "h.db"
    _populate(db)
    rc = _run(["history", "orders", "nope", "--db", str(db)])
    assert rc == 0
    assert "No history" in capsys.readouterr().out


def test_history_bad_limit(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    db = tmp_path / "h.db"
    _populate(db)
    rc = _run(["history", "orders", "o1", "--db", str(db), "--limit", "0"])
    assert rc == 1
    assert "limit" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# Error handling
# ---------------------------------------------------------------------------


def test_missing_db_errors_without_creating(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    db = tmp_path / "nope.db"
    rc = _run(["agents", "--db", str(db)])
    assert rc == 1
    assert "not found" in capsys.readouterr().err
    assert not db.exists()


def test_corrupt_db_errors_cleanly(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    db = tmp_path / "corrupt.db"
    db.write_bytes(b"this is not a sqlite database")
    rc = _run(["agents", "--db", str(db)])
    assert rc == 1
    assert "could not open database" in capsys.readouterr().err


def test_run_unknown_command_defensive(
    capsys: pytest.CaptureFixture[str],
) -> None:
    # argparse normally prevents this; exercise the defensive branch directly.
    ns = argparse.Namespace(command="bogus", db=":memory:")
    rc = inspector.run(ns)
    assert rc == 1
    assert "unknown command" in capsys.readouterr().err


def test_invalid_namespace_identifier_errors(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    db = tmp_path / "k.db"
    _populate(db)
    # A null byte is rejected by the store's identifier validation -> ValueError.
    rc = _run(["keys", "bad\x00ns", "--db", str(db)])
    assert rc == 1
    assert "error:" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# _age helper
# ---------------------------------------------------------------------------


def test_age_formats() -> None:
    now = datetime.now(UTC)
    assert inspector._age("") == ""
    assert inspector._age("not-a-date") == "not-a-date"
    assert inspector._age((now - timedelta(seconds=5)).isoformat()).endswith("s ago")
    assert inspector._age((now - timedelta(minutes=5)).isoformat()).endswith("m ago")
    assert inspector._age((now - timedelta(hours=5)).isoformat()).endswith("h ago")
    assert inspector._age((now - timedelta(days=5)).isoformat()).endswith("d ago")
    assert inspector._age((now + timedelta(seconds=30)).isoformat()) == "just now"
    # Naive timestamp is treated as UTC, not crashed on.
    naive = (now.replace(tzinfo=None) - timedelta(minutes=1)).isoformat()
    assert inspector._age(naive).endswith("ago")
