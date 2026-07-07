"""
Read-only CLI inspector for an agenthold store.

Exposes `agenthold` subcommands that let a human operator look inside the SQLite
store without going through MCP:

    agenthold agents                 # registered agents
    agenthold claims [--all]         # resource claims
    agenthold namespaces             # namespaces + record counts
    agenthold keys NAMESPACE         # keys in a namespace
    agenthold history NAMESPACE KEY  # version history of a key

Every command is read-only, supports --db PATH (default ./agenthold.db) and
--json, and never mutates the store. The subcommands are registered on the same
argparse parser as the server; a bare `agenthold` (no subcommand) still starts
the MCP server unchanged.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from agenthold.coordinator import Coordinator
from agenthold.store import StateStore

CLAIMS_NAMESPACE = Coordinator.NAMESPACE
AGENTS_NAMESPACE = Coordinator.AGENTS_NAMESPACE


# ---------------------------------------------------------------------------
# Argument registration
# ---------------------------------------------------------------------------


def register_subcommands(subparsers: Any) -> None:
    """Register the inspector subcommands on the given argparse subparsers.

    `subparsers` is the object returned by `parser.add_subparsers(...)`. Typed
    as Any because argparse's `_SubParsersAction` is a private generic.
    """

    def _add_common(parser: argparse.ArgumentParser) -> None:
        parser.add_argument(
            "--db",
            default="./agenthold.db",
            help="Path to the SQLite database (default: ./agenthold.db)",
        )
        parser.add_argument(
            "--json",
            action="store_true",
            help="Output JSON instead of a text table",
        )

    p_agents = subparsers.add_parser("agents", help="List registered agents")
    _add_common(p_agents)

    p_claims = subparsers.add_parser("claims", help="List resource claims")
    _add_common(p_claims)
    p_claims.add_argument(
        "--all",
        action="store_true",
        help="Include freed/released claims, not just active ones",
    )

    p_ns = subparsers.add_parser(
        "namespaces", help="List namespaces with record counts"
    )
    _add_common(p_ns)

    p_keys = subparsers.add_parser("keys", help="List keys in a namespace")
    p_keys.add_argument("namespace", help="Namespace to list keys from")
    _add_common(p_keys)

    p_hist = subparsers.add_parser("history", help="Show version history of a key")
    p_hist.add_argument("namespace", help="Namespace")
    p_hist.add_argument("key", help="Key")
    p_hist.add_argument(
        "--limit", type=int, default=10, help="Max versions to show (default 10)"
    )
    _add_common(p_hist)


# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------


def run(args: argparse.Namespace) -> int:
    """Run an inspector subcommand. Returns a process exit code."""
    db = args.db
    if db != ":memory:" and not Path(db).exists():
        print(f"error: database not found at {db!r}", file=sys.stderr)
        return 1

    try:
        store = StateStore(db)
    except sqlite3.Error as e:
        print(f"error: could not open database {db!r}: {e}", file=sys.stderr)
        return 1
    try:
        if args.command == "agents":
            return _cmd_agents(store, args.json)
        if args.command == "claims":
            return _cmd_claims(store, args.json, args.all)
        if args.command == "namespaces":
            return _cmd_namespaces(store, args.json)
        if args.command == "keys":
            return _cmd_keys(store, args.namespace, args.json)
        if args.command == "history":
            return _cmd_history(store, args.namespace, args.key, args.limit, args.json)
        # Unreachable: argparse restricts command to the registered set.
        print(f"error: unknown command {args.command!r}", file=sys.stderr)
        return 1
    except ValueError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    finally:
        store.close()


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------


def _cmd_agents(store: StateStore, as_json: bool) -> int:
    agents = []
    for record in store.list_keys(AGENTS_NAMESPACE):
        value = record.value if isinstance(record.value, dict) else {}
        agents.append(
            {
                "agent_id": record.key,
                "name": value.get("name", ""),
                "model": value.get("model", ""),
                "status": value.get("status", ""),
                "registered_at": value.get("registered_at", ""),
                "last_activity": value.get("last_activity", ""),
            }
        )
    agents.sort(key=lambda a: a["agent_id"])

    if as_json:
        print(json.dumps(agents, indent=2))
        return 0
    if not agents:
        print("No registered agents.")
        return 0
    _print_table(
        ["AGENT ID", "NAME", "MODEL", "STATUS", "LAST ACTIVITY"],
        [
            [
                a["agent_id"],
                a["name"],
                a["model"],
                a["status"],
                _age(a["last_activity"]),
            ]
            for a in agents
        ],
    )
    return 0


def _cmd_claims(store: StateStore, as_json: bool, show_all: bool) -> int:
    names = _load_agent_names(store)
    claims: list[dict[str, Any]] = []
    for record in store.list_keys(CLAIMS_NAMESPACE):
        value = record.value
        if not isinstance(value, dict) or "status" not in value:
            continue  # skip malformed / legacy values defensively
        status = value.get("status")
        if status == "claimed":
            held_by = value.get("by", "")
            claims.append(
                {
                    "resource": record.key,
                    "state": "claimed",
                    "held_by": held_by,
                    "held_by_name": names.get(held_by, ""),
                    "claimed_at": value.get("at", ""),
                }
            )
        elif status == "free" and show_all:
            released_by = value.get("released_by", "")
            claims.append(
                {
                    "resource": record.key,
                    "state": "free",
                    "released_by": released_by,
                    "released_by_name": names.get(released_by, ""),
                    "outcome": value.get("outcome", "released"),
                    "moved_to": value.get("moved_to"),
                    "at": value.get("at", ""),
                }
            )
    claims.sort(key=lambda c: c["resource"])

    if as_json:
        print(json.dumps(claims, indent=2))
        return 0
    if not claims:
        print("No claims." if show_all else "No active claims.")
        return 0

    rows: list[list[str]] = []
    for claim in claims:
        if claim["state"] == "claimed":
            who = claim["held_by_name"] or claim["held_by"]
            rows.append([claim["resource"], "claimed", who, _age(claim["claimed_at"])])
        else:
            who = claim["released_by_name"] or claim["released_by"]
            state = f"free ({claim['outcome']}"
            if claim.get("moved_to"):
                state += f" -> {claim['moved_to']}"
            state += ")"
            rows.append([claim["resource"], state, who, _age(claim["at"])])
    _print_table(["RESOURCE", "STATE", "BY", "SINCE"], rows)
    return 0


def _cmd_namespaces(store: StateStore, as_json: bool) -> int:
    data = [
        {"namespace": ns, "keys": len(store.list_keys(ns))}
        for ns in store.list_namespaces()
    ]
    if as_json:
        print(json.dumps(data, indent=2))
        return 0
    if not data:
        print("No namespaces.")
        return 0
    _print_table(
        ["NAMESPACE", "KEYS"],
        [[str(d["namespace"]), str(d["keys"])] for d in data],
    )
    return 0


def _cmd_keys(store: StateStore, namespace: str, as_json: bool) -> int:
    data = [
        {
            "key": r.key,
            "version": r.version,
            "updated_by": r.updated_by,
            "updated_at": r.updated_at.isoformat(),
        }
        for r in store.list_keys(namespace)
    ]
    if as_json:
        print(json.dumps(data, indent=2))
        return 0
    if not data:
        print(f"No keys in namespace {namespace!r}.")
        return 0
    _print_table(
        ["KEY", "VERSION", "UPDATED BY", "UPDATED AT"],
        [
            [
                str(d["key"]),
                str(d["version"]),
                str(d["updated_by"]),
                str(d["updated_at"]),
            ]
            for d in data
        ],
    )
    return 0


def _cmd_history(
    store: StateStore, namespace: str, key: str, limit: int, as_json: bool
) -> int:
    if limit < 1:
        print("error: --limit must be >= 1", file=sys.stderr)
        return 1
    data = [
        {
            "version": e.version,
            "event_type": e.event_type,
            "value": e.value,
            "updated_by": e.updated_by,
            "updated_at": e.updated_at.isoformat(),
        }
        for e in store.history(namespace, key, limit=limit)
    ]
    if as_json:
        print(json.dumps(data, indent=2))
        return 0
    if not data:
        print(f"No history for {namespace!r}/{key!r}.")
        return 0
    _print_table(
        ["VERSION", "EVENT", "UPDATED BY", "UPDATED AT"],
        [
            [
                str(d["version"]),
                str(d["event_type"]),
                str(d["updated_by"]),
                str(d["updated_at"]),
            ]
            for d in data
        ],
    )
    return 0


# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------


def _load_agent_names(store: StateStore) -> dict[str, str]:
    names: dict[str, str] = {}
    for record in store.list_keys(AGENTS_NAMESPACE):
        if isinstance(record.value, dict):
            names[record.key] = record.value.get("name", "")
    return names


def _age(iso: str) -> str:
    """Render an ISO timestamp as a compact age like '3m ago'."""
    if not iso:
        return ""
    try:
        then = datetime.fromisoformat(iso)
    except ValueError:
        return iso
    if then.tzinfo is None:
        then = then.replace(tzinfo=UTC)
    seconds = int((datetime.now(UTC) - then).total_seconds())
    if seconds < 0:
        return "just now"
    if seconds < 60:
        return f"{seconds}s ago"
    if seconds < 3600:
        return f"{seconds // 60}m ago"
    if seconds < 86400:
        return f"{seconds // 3600}h ago"
    return f"{seconds // 86400}d ago"


def _print_table(headers: list[str], rows: list[list[str]]) -> None:
    widths = [len(h) for h in headers]
    for row in rows:
        for i, cell in enumerate(row):
            widths[i] = max(widths[i], len(cell))
    print("  ".join(h.ljust(widths[i]) for i, h in enumerate(headers)))
    print("  ".join("-" * widths[i] for i in range(len(headers))))
    for row in rows:
        print("  ".join(cell.ljust(widths[i]) for i, cell in enumerate(row)))
