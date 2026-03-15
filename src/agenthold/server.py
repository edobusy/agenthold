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
from pathlib import Path
from typing import Any

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import TextContent, Tool

from agenthold.exceptions import ConflictError, NotFoundError
from agenthold.store import StateStore

_NS_FIELD: dict[str, str] = {
    "type": "string",
    "description": "Workflow or resource identifier, e.g. 'order-1234'",
}
_KEY_FIELD: dict[str, str] = {
    "type": "string",
    "description": "The state key, e.g. 'status'",
}


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
                    "Always pass the returned version as expected_version in a "
                    "subsequent agenthold_set call to enable conflict detection. "
                    "If you omit expected_version in agenthold_set, your write will "
                    "silently overwrite any concurrent changes without warning."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "namespace": _NS_FIELD,
                        "key": _KEY_FIELD,
                    },
                    "required": ["namespace", "key"],
                },
            ),
            Tool(
                name="agenthold_set",
                description=(
                    "Write a value to a state record. "
                    "Pass expected_version (from a prior agenthold_get) to enable "
                    "conflict detection — if another agent wrote since your read, "
                    "you will receive a conflict response with the current state "
                    "so you can re-read and retry. "
                    "Pass expected_version=0 to write only if the key does not "
                    "yet exist (create-only guard). "
                    "Omit expected_version entirely to write unconditionally, "
                    "overwriting any concurrent changes without warning."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "namespace": _NS_FIELD,
                        "key": _KEY_FIELD,
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
                                "the write will be rejected with a conflict response."
                            ),
                        },
                    },
                    "required": ["namespace", "key", "value", "updated_by"],
                },
            ),
            Tool(
                name="agenthold_list",
                description=(
                    "List all current state records in a namespace. "
                    "Returns each key's live value, current version, and "
                    "last-write metadata. "
                    "Does not return history — use agenthold_history for past "
                    "versions. "
                    "Returns an empty list if the namespace has no records or "
                    "does not exist; it does not return an error for missing "
                    "namespaces."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "namespace": _NS_FIELD,
                    },
                    "required": ["namespace"],
                },
            ),
            Tool(
                name="agenthold_history",
                description=(
                    "Read the version history of a state record, newest first. "
                    "Useful for debugging coordination issues and auditing writes. "
                    "Returns an empty list if no writes have been recorded for "
                    "this key — this does not confirm the key exists. "
                    "Use agenthold_get to check current state. "
                    "Each entry includes an event_type field: 'write' for "
                    "normal writes, 'delete' for deletion events."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "namespace": _NS_FIELD,
                        "key": _KEY_FIELD,
                        "limit": {
                            "type": "integer",
                            "description": "Max versions to return (default 10)",
                            "default": 10,
                        },
                    },
                    "required": ["namespace", "key"],
                },
            ),
            Tool(
                name="agenthold_delete",
                description=(
                    "Delete a state record permanently. "
                    "The deletion is recorded as a tombstone in agenthold_history "
                    "(event_type='delete') so the full lifecycle of the key "
                    "remains auditable. "
                    "Returns not_found if the key does not exist — this is not "
                    "an error; the key is absent either way. "
                    "Pass expected_version (from a prior agenthold_get) to prevent "
                    "accidentally deleting a record that was updated since your "
                    "read. Omit expected_version to delete unconditionally."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "namespace": _NS_FIELD,
                        "key": _KEY_FIELD,
                        "deleted_by": {
                            "type": "string",
                            "description": (
                                "Your agent identifier, e.g. 'cleanup-agent'"
                            ),
                        },
                        "expected_version": {
                            "type": "integer",
                            "description": (
                                "The version you last read. If the stored version "
                                "has changed since your read, the delete will be "
                                "rejected with a conflict response."
                            ),
                        },
                    },
                    "required": ["namespace", "key", "deleted_by"],
                },
            ),
        ]

    @server.call_tool()  # type: ignore[untyped-decorator]
    async def call_tool(  # pragma: no cover
        name: str, arguments: dict[str, Any]
    ) -> list[TextContent]:
        result = _dispatch(store, name, arguments)
        text = json.dumps(result, indent=2)
        return [TextContent(type="text", text=text)]

    return server


def _dispatch(store: StateStore, name: str, args: dict[str, Any]) -> dict[str, Any]:
    """Synchronous dispatch — the store is internally thread-safe."""
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
                    "event_type": r.event_type,
                }
                for r in history_records
            ],
        }

    elif name == "agenthold_delete":
        try:
            deleted_version = store.delete(
                namespace=args["namespace"],
                key=args["key"],
                deleted_by=args["deleted_by"],
                expected_version=args.get("expected_version"),
            )
            if deleted_version is None:
                return {
                    "status": "not_found",
                    "namespace": args["namespace"],
                    "key": args["key"],
                }
            return {
                "status": "ok",
                "namespace": args["namespace"],
                "key": args["key"],
                "deleted_version": deleted_version,
                "deleted_by": args["deleted_by"],
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
                    "Call agenthold_get to read the current state "
                    "and decide whether to proceed with deletion."
                ),
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
