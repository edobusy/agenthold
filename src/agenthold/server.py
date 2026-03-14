"""
Agenthold MCP server.

Exposes four tools over the Model Context Protocol:
  agenthold_get : read a state record
  agenthold_set : write a state record (with optional conflict detection)
  agenthold_list : list all keys in a namespace
  agenthold_history : read the version history of a key

Usage:
  agenthold --db ./state.db

Add to your MCP client config:
  {
    "mcpServers": {
      "agenthold": {
        "command": "agenthold",
        "args": ["--db", "/path/to/state.db"]
      }
    }
  }
"""

import argparse
import asyncio
import json
import threading
from pathlib import Path
from typing import Any

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import TextContent, Tool

from agenthold.exceptions import ConflictError, NotFoundError
from agenthold.store import StateStore

# Thread lock: SQLite connections are not thread-safe across concurrent async
# tasks. A single lock is fine for this scope, contention is minimal.
_STORE_LOCK = threading.Lock()


def make_server(db_path: str | Path) -> Server:
    store = StateStore(db_path)
    server = Server("agenthold")

    @server.list_tools()  # type: ignore[no-untyped-call, untyped-decorator]
    async def list_tools() -> list[Tool]:  # pragma: no cover
        return [
            Tool(
                name="agenthold_get",
                description=(
                    "Read the current value of a state record. "
                    "Returns the value, version number, and metadata. "
                    "Use the returned version number in subsequent agenthold_set "
                    "calls to enable conflict detection."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "namespace": {
                            "type": "string",
                            "description": (
                                "Workflow or resource identifier, e.g. 'order-1234'"
                            ),
                        },
                        "key": {
                            "type": "string",
                            "description": "The state key, e.g. 'status'",
                        },
                    },
                    "required": ["namespace", "key"],
                },
            ),
            Tool(
                name="agenthold_set",
                description=(
                    "Write a value to a state record. "
                    "Pass expected_version (from a prior agenthold_get) to enable "
                    "conflict detection, if another agent has written since your read, "
                    "you will receive a conflict error with the current state so you "
                    "can re-read and retry. "
                    "Omit expected_version to write unconditionally."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "namespace": {"type": "string"},
                        "key": {"type": "string"},
                        "value": {
                            "description": "Any JSON-serialisable value",
                        },
                        "updated_by": {
                            "type": "string",
                            "description": (
                                "Your agent identifier, e.g. 'inventory-agent'"
                            ),
                        },
                        "expected_version": {
                            "type": "integer",
                            "description": (
                                "The version you read before making your changes. "
                                "If the stored version has changed since your read, "
                                "the write will be rejected with a conflict error."
                            ),
                        },
                    },
                    "required": ["namespace", "key", "value", "updated_by"],
                },
            ),
            Tool(
                name="agenthold_list",
                description="List all current state records in a namespace.",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "namespace": {"type": "string"},
                    },
                    "required": ["namespace"],
                },
            ),
            Tool(
                name="agenthold_history",
                description=(
                    "Read the version history of a state record, newest first. "
                    "Useful for debugging coordination issues."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "namespace": {"type": "string"},
                        "key": {"type": "string"},
                        "limit": {
                            "type": "integer",
                            "description": "Max versions to return (default 10)",
                            "default": 10,
                        },
                    },
                    "required": ["namespace", "key"],
                },
            ),
        ]

    @server.call_tool()  # type: ignore[untyped-decorator]
    async def call_tool(  # pragma: no cover
        name: str, arguments: dict[str, Any]
    ) -> list[TextContent]:
        result = _dispatch(store, name, arguments)
        text = json.dumps(result, indent=2, default=str)
        return [TextContent(type="text", text=text)]

    return server


def _dispatch(store: StateStore, name: str, args: dict[str, Any]) -> dict[str, Any]:
    """Synchronous dispatch, called from async context with the store lock held."""
    with _STORE_LOCK:
        if name == "agenthold_get":
            try:
                record = store.get(args["namespace"], args["key"])
                return {
                    "status": "ok",
                    "namespace": record.namespace,
                    "key": record.key,
                    "value": record.value,
                    "version": record.version,
                    "updated_by": record.updated_by,
                    "updated_at": record.updated_at.isoformat(),
                }
            except NotFoundError:
                return {
                    "status": "not_found",
                    "namespace": args["namespace"],
                    "key": args["key"],
                }

        elif name == "agenthold_set":
            try:
                result = store.set(
                    namespace=args["namespace"],
                    key=args["key"],
                    value=args["value"],
                    updated_by=args["updated_by"],
                    expected_version=args.get("expected_version"),
                )
                return {
                    "status": "ok",
                    "namespace": result.namespace,
                    "key": result.key,
                    "version": result.version,
                    "previous_version": result.previous_version,
                }
            except ConflictError as e:
                return {
                    "status": "conflict",
                    "message": str(e),
                    "namespace": e.detail.namespace,
                    "key": e.detail.key,
                    "expected_version": e.detail.expected_version,
                    "actual_version": e.detail.actual_version,
                    "actual_updated_by": e.detail.updated_by,
                    "actual_updated_at": e.detail.updated_at.isoformat(),
                    "hint": (
                        "Call agenthold_get to read the current state, "
                        "merge your changes, and retry with the new version."
                    ),
                }

        elif name == "agenthold_list":
            records = store.list_keys(args["namespace"])
            return {
                "status": "ok",
                "namespace": args["namespace"],
                "count": len(records),
                "records": [
                    {
                        "key": r.key,
                        "value": r.value,
                        "version": r.version,
                        "updated_by": r.updated_by,
                        "updated_at": r.updated_at.isoformat(),
                    }
                    for r in records
                ],
            }

        elif name == "agenthold_history":
            history_records = store.history(
                args["namespace"],
                args["key"],
                limit=args.get("limit", 10),
            )
            return {
                "status": "ok",
                "namespace": args["namespace"],
                "key": args["key"],
                "history": [
                    {
                        "version": r.version,
                        "value": r.value,
                        "updated_by": r.updated_by,
                        "updated_at": r.updated_at.isoformat(),
                    }
                    for r in history_records
                ],
            }

        else:
            return {"status": "error", "message": f"Unknown tool: {name}"}


async def _run_server(db_path: str | Path) -> None:  # pragma: no cover
    server = make_server(db_path)
    async with stdio_server() as (read_stream, write_stream):
        init_options = server.create_initialization_options()
        await server.run(read_stream, write_stream, init_options)


def main() -> None:  # pragma: no cover
    parser = argparse.ArgumentParser(description="Agenthold MCP server")
    parser.add_argument(
        "--db",
        default="./agenthold.db",
        help="Path to the SQLite database file (default: ./agenthold.db)",
    )
    args = parser.parse_args()
    asyncio.run(_run_server(args.db))


if __name__ == "__main__":  # pragma: no cover
    main()
