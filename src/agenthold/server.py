"""
Agenthold MCP server.

Exposes eight tools over the Model Context Protocol:
  agenthold_get             : read a state record
  agenthold_set             : write a state record (with optional conflict detection)
  agenthold_list            : list all keys in a namespace
  agenthold_history         : read the version history of a key
  agenthold_delete          : permanently delete a state record
  agenthold_clear_namespace : delete all records in a namespace
  agenthold_export          : export all records and full history for a namespace
  agenthold_watch           : wait for a key's version to change

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
import time
from pathlib import Path
from typing import Any

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import TextContent, Tool

from agenthold.exceptions import BusyError, ConflictError, NotFoundError
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
                    "silently overwrite any concurrent changes without warning. "
                    "If the key does not exist, the response has status 'not_found' "
                    "with no version — use expected_version=0 in agenthold_set to "
                    "write only if the key is still absent."
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
                    "overwriting any concurrent changes without warning. "
                    "On success, the response includes the new version — use it "
                    "directly as expected_version for subsequent writes without "
                    "calling agenthold_get again. "
                    "previous_version is null for the first write to a key."
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
                    "normal writes, 'delete' for deletion events. "
                    "If the response contains exactly limit entries, more history "
                    "may exist — pass a larger limit to see further back."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "namespace": _NS_FIELD,
                        "key": _KEY_FIELD,
                        "limit": {
                            "type": "integer",
                            "description": "Max versions to return (default 10, min 1)",
                            "default": 10,
                            "minimum": 1,
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
                                "rejected with a conflict response. "
                                "Do not pass 0 — unlike agenthold_set, version 0 "
                                "means the key does not exist and any live key "
                                "will always conflict."
                            ),
                        },
                    },
                    "required": ["namespace", "key", "deleted_by"],
                },
            ),
            Tool(
                name="agenthold_clear_namespace",
                description=(
                    "Delete all state records in a namespace in a single "
                    "atomic operation. "
                    "Intended for cleanup at the end of a workflow. "
                    "A deletion tombstone is written to agenthold_history for "
                    "every key removed, so the full lifecycle remains auditable. "
                    "This operation has no conflict guard — it deletes "
                    "unconditionally. "
                    "If you need to inspect the namespace before clearing, call "
                    "agenthold_list first — but note that agenthold_list "
                    "followed by agenthold_clear_namespace is not atomic: "
                    "concurrent writes between the two calls may change "
                    "what gets deleted. "
                    "If other agents are writing to this namespace "
                    "concurrently, keys may reappear immediately after "
                    "this call returns. "
                    "If deleted_keys contains unexpected entries, call "
                    "agenthold_history on those keys to investigate "
                    "what was written and by whom. "
                    "Agents calling agenthold_watch on keys in this namespace "
                    "will not be immediately notified of deletion — "
                    "their watches will time out. "
                    "Returns the list of deleted keys (sorted alphabetically) "
                    "and a count. "
                    "Returns deleted_count=0 with an empty list if the namespace "
                    "has no records or does not exist — this is not an error."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "namespace": _NS_FIELD,
                        "deleted_by": {
                            "type": "string",
                            "description": (
                                "Your agent identifier, e.g. 'cleanup-agent'"
                            ),
                        },
                    },
                    "required": ["namespace", "deleted_by"],
                },
            ),
            Tool(
                name="agenthold_export",
                description=(
                    "Export all live records and their complete version history "
                    "for a namespace as a single JSON snapshot. "
                    "Intended for debugging coordination issues and building "
                    "audit trails. "
                    "Records are sorted alphabetically by key. "
                    "Each record entry contains the current value and the full "
                    "history of that key, newest event first. "
                    "History includes all event types — 'write' for normal writes "
                    "and 'delete' for tombstones. Delete tombstones have value null. "
                    "record_count is the number of live keys in the namespace. "
                    "history_count is the total number of history entries across all "
                    "keys, including tombstones — it is not a count of writes only. "
                    "Check history_count after receiving the response to understand "
                    "total history volume without iterating all records. "
                    "If a key was deleted and recreated, its history contains "
                    "tombstones from the prior lifecycle alongside the new writes. "
                    "Only live (non-deleted) keys are included. "
                    "To inspect the history of a deleted key, use agenthold_history "
                    "— you must already know the key name; there is no tool to list "
                    "deleted keys. "
                    "exported_at is the ISO timestamp of when the snapshot was taken. "
                    "For large namespaces, this response can be very large. "
                    "If the namespace is unfamiliar, call agenthold_list first to "
                    "preview how many keys exist before calling agenthold_export. "
                    "This call holds a read transaction for the full duration "
                    "of all reads; do not call it in tight loops or polling patterns. "
                    "Returns record_count=0 and an empty records list if the "
                    "namespace has no live records — this is not an error. "
                    "If you expected records but got record_count=0, verify the "
                    "namespace name. If records were recently deleted, use "
                    "agenthold_history on individual keys to see their tombstones."
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
                name="agenthold_watch",
                description=(
                    "Wait for a key's version to change, then return the new value. "
                    "Polls every 200 ms. Returns when version exceeds since_version, "
                    'or returns {"status": "timeout"} with a hint if nothing changed '
                    "within timeout_seconds. "
                    "IMPORTANT: This call holds the agent turn until it returns — no "
                    "other actions can be taken while waiting. Only use this when the "
                    "agent has nothing else to do until the key changes. "
                    "Use since_version=0 to wait for a key that does not exist yet. "
                    "On timeout, read the hint field for guidance on next steps. "
                    "Warning: if a key is deleted and recreated, its version restarts "
                    "at 1; a watch with since_version >= 1 will not fire on recreation "
                    "— call agenthold_get after timeout to check current state."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "namespace": _NS_FIELD,
                        "key": _KEY_FIELD,
                        "since_version": {
                            "type": "integer",
                            "description": (
                                "Return when version exceeds this value. "
                                "Pass the version you last read — from agenthold_get, "
                                "agenthold_list, or an agenthold_set response. "
                                "Use 0 to wait for the very first write to a key."
                            ),
                        },
                        "timeout_seconds": {
                            "type": "number",
                            "default": 10.0,
                            "description": (
                                "Maximum seconds to wait (default 10). "
                                "Pass a larger value if the writing agent may take "
                                "longer. 0 returns immediately after one check. "
                                "Actual wait may exceed this by up to 200ms due "
                                "to the polling interval."
                            ),
                        },
                    },
                    "required": ["namespace", "key", "since_version"],
                },
            ),
        ]

    @server.call_tool()  # type: ignore[untyped-decorator]
    async def call_tool(  # pragma: no cover
        name: str, arguments: dict[str, Any]
    ) -> list[TextContent]:
        if name == "agenthold_watch":
            result = await _watch(
                store,
                namespace=arguments["namespace"],
                key=arguments["key"],
                since_version=int(arguments["since_version"]),
                timeout_seconds=float(arguments.get("timeout_seconds", 10.0)),
            )
        else:
            result = _dispatch(store, name, arguments)
        return [TextContent(type="text", text=json.dumps(result, indent=2))]

    return server


def _dispatch(store: StateStore, name: str, args: dict[str, Any]) -> dict[str, Any]:
    """Synchronous dispatch — the store is internally thread-safe."""
    try:
        return _dispatch_tool(store, name, args)
    except BusyError:
        return {
            "status": "busy",
            "message": ("The database is temporarily locked by another writer."),
            "hint": "Retry the operation after a short delay.",
        }


def _dispatch_tool(
    store: StateStore, name: str, args: dict[str, Any]
) -> dict[str, Any]:
    """Route a tool call to the appropriate store method."""
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
                expected_version=(
                    int(args["expected_version"])
                    if "expected_version" in args
                    else None
                ),
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
                "actual_value": e.detail.actual_value,
                "actual_updated_by": e.detail.updated_by,
                "actual_updated_at": e.detail.updated_at.isoformat(),
                "hint": (
                    "The current value is in actual_value. "
                    "Merge your changes with it and retry "
                    "with expected_version=actual_version."
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
        limit = int(args.get("limit", 10))
        if limit < 1:
            return {"status": "error", "message": "limit must be >= 1"}
        history_records = store.history(
            args["namespace"],
            args["key"],
            limit=limit,
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
                expected_version=(
                    int(args["expected_version"])
                    if "expected_version" in args
                    else None
                ),
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
                "actual_value": e.detail.actual_value,
                "actual_updated_by": e.detail.updated_by,
                "actual_updated_at": e.detail.updated_at.isoformat(),
                "hint": (
                    "The current value is in actual_value. "
                    "Inspect it and retry the delete "
                    "with expected_version=actual_version."
                ),
            }

    elif name == "agenthold_clear_namespace":
        deleted_keys = store.clear_namespace(
            namespace=args["namespace"],
            deleted_by=args["deleted_by"],
        )
        return {
            "status": "ok",
            "namespace": args["namespace"],
            "deleted_count": len(deleted_keys),
            "deleted_keys": deleted_keys,
            "deleted_by": args["deleted_by"],
        }

    elif name == "agenthold_export":
        exported_at, entries = store.export_namespace(
            namespace=args["namespace"],
        )
        return {
            "status": "ok",
            "namespace": args["namespace"],
            "exported_at": exported_at,
            "record_count": len(entries),
            "history_count": sum(len(hist) for _, hist in entries),
            "records": [
                {
                    "key": record.key,
                    "value": record.value,
                    "version": record.version,
                    "updated_by": record.updated_by,
                    "updated_at": record.updated_at.isoformat(),
                    "history": [
                        {
                            "version": entry.version,
                            "value": entry.value,
                            "event_type": entry.event_type,
                            "updated_by": entry.updated_by,
                            "updated_at": entry.updated_at.isoformat(),
                        }
                        for entry in history
                    ],
                }
                for record, history in entries
            ],
        }

    else:
        return {"status": "error", "message": f"Unknown tool: {name}"}


async def _watch(
    store: StateStore,
    namespace: str,
    key: str,
    since_version: int,
    timeout_seconds: float,
) -> dict[str, Any]:
    """Async polling loop. Called directly from call_tool, not via _dispatch."""
    if since_version < 0:
        return {"status": "error", "message": "since_version must be >= 0"}
    if timeout_seconds < 0:
        return {"status": "error", "message": "timeout_seconds must be >= 0"}

    start = time.monotonic()
    deadline = start + timeout_seconds

    while True:
        try:
            record = store.get(namespace, key)
            if record.version > since_version:
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
            pass  # version 0; keep waiting

        now = time.monotonic()
        if now >= deadline:
            return {
                "status": "timeout",
                "namespace": namespace,
                "key": key,
                "since_version": since_version,
                "elapsed_seconds": round(now - start, 3),
                "hint": (
                    "The key did not change within the timeout. "
                    "Retry with the same since_version, or call agenthold_get "
                    "to check current state before deciding whether to wait again."
                ),
            }

        await asyncio.sleep(0.2)


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
