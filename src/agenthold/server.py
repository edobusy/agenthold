"""
Agenthold MCP server.

Two tool modes:

  standard (default):
    Four high-level tools for plug-and-play coordination:
      agenthold_claim   : claim exclusive access to a resource
      agenthold_release : release your claim when done
      agenthold_status  : check if a resource is available
      agenthold_wait    : wait for a resource to become available

  advanced (--tools advanced):
    Eight low-level primitives for custom protocols:
      agenthold_get             : read a state record
      agenthold_set             : write a state record (with conflict detection)
      agenthold_list            : list all keys in a namespace
      agenthold_history         : read the version history of a key
      agenthold_delete          : permanently delete a state record
      agenthold_clear_namespace : delete all records in a namespace
      agenthold_export          : export all records and full history
      agenthold_watch           : wait for a key's version to change

Usage:
  agenthold --db ./state.db                 # standard mode (default)
  agenthold --db ./state.db --tools advanced  # advanced mode

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

from agenthold.coordinator import Coordinator
from agenthold.exceptions import BusyError, ConflictError, NotFoundError
from agenthold.store import StateStore, _validate_identifier

_NS_FIELD: dict[str, str] = {
    "type": "string",
    "description": "Workflow or resource identifier, e.g. 'order-1234'",
}
_KEY_FIELD: dict[str, str] = {
    "type": "string",
    "description": "The state key, e.g. 'status'",
}

_RESOURCE_FIELD: dict[str, str] = {
    "type": "string",
    "description": (
        "Identifier for the resource, e.g. a filename like 'intro.md' or 'src/main.py'"
    ),
}
_AGENT_FIELD: dict[str, str] = {
    "type": "string",
    "description": ("Your agent identifier. Use a consistent name across all calls."),
}

COORDINATION_INSTRUCTIONS = """\
You have access to agenthold, a resource coordination system that \
prevents conflicts when multiple agents work in the same environment.

RULES:

1. BEFORE modifying any file or shared resource, call agenthold_claim \
   with the resource path and your agent name. Do not proceed until \
   the claim is granted.

2. If agenthold_claim returns "busy", do NOT modify the resource. \
   Choose a different resource, or call agenthold_wait.

3. AFTER finishing modifications, call agenthold_release immediately.

4. Use the filename as the resource identifier for files \
   (e.g. "intro.md", "src/main.py").

5. Use a consistent agent name across all your calls.\
"""


def _standard_tools() -> list[Tool]:
    """Return the four high-level coordination tools."""
    return [
        Tool(
            name="agenthold_claim",
            description=(
                "Claim exclusive access to a resource before modifying it. "
                "IMPORTANT: You MUST call this before editing any file or "
                "shared resource when other agents may be working in the "
                "same environment. Do not proceed with modifications until "
                "the claim is granted. "
                "Pass the filename as the resource identifier (e.g. "
                '"intro.md", "src/main.py"). Use a consistent agent name '
                "across all your calls. "
                "Possible responses: "
                '"claimed": You now hold exclusive access. Proceed with '
                "your edits, then call agenthold_release when done. "
                '"already_claimed": You already hold this claim. Safe to '
                "proceed. "
                '"busy": Another agent is working on this resource. Do NOT '
                "modify it. Work on a different resource, or call "
                "agenthold_wait to be notified when it becomes available. "
                "The response includes who holds the claim and when they "
                "claimed it."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "resource": _RESOURCE_FIELD,
                    "agent": _AGENT_FIELD,
                },
                "required": ["resource", "agent"],
            },
        ),
        Tool(
            name="agenthold_release",
            description=(
                "Release your exclusive claim on a resource after finishing "
                "your edits. "
                "IMPORTANT: You MUST call this when done modifying a "
                "resource. Holding claims longer than necessary blocks "
                "other agents. If you claimed a resource but decided not "
                "to modify it, release it anyway. "
                "The release immediately notifies any agents waiting via "
                "agenthold_wait. "
                "Possible responses: "
                '"released": Claim released successfully. Other agents can '
                "now claim the resource. "
                '"already_free": The resource was already free. No action '
                "needed. "
                '"not_found": The resource was never claimed. No action '
                "needed. "
                '"error": You tried to release a resource claimed by a '
                "different agent."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "resource": _RESOURCE_FIELD,
                    "agent": _AGENT_FIELD,
                },
                "required": ["resource", "agent"],
            },
        ),
        Tool(
            name="agenthold_status",
            description=(
                "Check if a resource is available or currently claimed by "
                "another agent. "
                "Use this to decide which resource to work on next when "
                "you have multiple options. If the resource is available, "
                "call agenthold_claim to secure it before modifying. If "
                "claimed by another agent, work on a different resource or "
                "call agenthold_wait. "
                "Possible responses: "
                '"available": The resource is free. Call agenthold_claim to '
                "secure it before editing. "
                '"claimed": Another agent holds this resource. The response '
                "tells you who and when."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "resource": _RESOURCE_FIELD,
                },
                "required": ["resource"],
            },
        ),
        Tool(
            name="agenthold_wait",
            description=(
                "Wait for a resource to become available. Blocks your turn "
                "until the current holder releases their claim, or the "
                "timeout expires. "
                "IMPORTANT: This call holds your agent turn until it "
                "returns — no other actions can be taken while waiting. "
                "Only use this when you need a specific resource and no "
                "other useful work can proceed without it. "
                "Pass a reasonable timeout (default 30 seconds). On "
                "timeout, the hint field suggests next steps. "
                "Possible responses: "
                '"available": The resource is now free. Call '
                "agenthold_claim immediately to secure it — another agent "
                "may also be waiting. "
                '"timeout": The resource was not released within the '
                "timeout. The response includes who still holds the claim."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "resource": _RESOURCE_FIELD,
                    "timeout_seconds": {
                        "type": "number",
                        "default": 30.0,
                        "description": (
                            "Maximum seconds to wait (default 30). "
                            "On timeout, the response includes who still "
                            "holds the claim."
                        ),
                    },
                },
                "required": ["resource"],
            },
        ),
    ]


def _advanced_tools() -> list[Tool]:
    """Return the eight low-level primitive tools."""
    return [
        Tool(
            name="agenthold_get",
            description=(
                "Read the current value of a state record. "
                "Returns the value, version number, and metadata. "
                "Always pass the returned version as expected_version in a "
                "subsequent agenthold_set call for conflict detection. "
                "If you set force=true in agenthold_set, your write will "
                "bypass conflict detection and overwrite without warning. "
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
                "expected_version is required. Pass the version from a prior "
                "agenthold_get (or 0 for a key that should not yet exist). "
                "If the stored version has changed since your read, you will "
                "receive a conflict response with the current state so you "
                "can re-read and retry. "
                "Pass expected_version=0 to write only if the key does not "
                "yet exist (create-only guard). "
                "To write unconditionally (bypassing conflict detection), "
                "set force=true — this is rarely needed and should only be "
                "used for idempotent writes or initial seeding. "
                "When force is true, expected_version is ignored. "
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
                            "the write will be rejected with a conflict response. "
                            "Pass 0 for a key that should not yet exist."
                        ),
                    },
                    "force": {
                        "type": "boolean",
                        "default": False,
                        "description": (
                            "Set to true to write unconditionally, bypassing "
                            "conflict detection. Use this only for idempotent "
                            "writes or initial seeding where overwriting is "
                            "intentional. When force is true, expected_version "
                            "is ignored."
                        ),
                    },
                },
                "required": [
                    "namespace",
                    "key",
                    "value",
                    "updated_by",
                    "expected_version",
                ],
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
                "expected_version is required. Pass the version from a prior "
                "agenthold_get to prevent accidentally deleting a record that "
                "was updated since your read. "
                "To delete unconditionally (bypassing conflict detection), "
                "set force=true. When force is true, expected_version is "
                "ignored. "
                "Do not pass expected_version=0 — unlike agenthold_set, "
                "version 0 means the key does not exist and any live key "
                "will always conflict."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "namespace": _NS_FIELD,
                    "key": _KEY_FIELD,
                    "deleted_by": {
                        "type": "string",
                        "description": ("Your agent identifier, e.g. 'cleanup-agent'"),
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
                    "force": {
                        "type": "boolean",
                        "default": False,
                        "description": (
                            "Set to true to delete unconditionally, bypassing "
                            "conflict detection. When force is true, "
                            "expected_version is ignored."
                        ),
                    },
                },
                "required": ["namespace", "key", "deleted_by", "expected_version"],
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
                        "description": ("Your agent identifier, e.g. 'cleanup-agent'"),
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


def make_server(
    db_path: str | Path,
    tools_mode: str = "standard",
) -> Server:
    store = StateStore(db_path)
    coordinator = Coordinator(store)

    if tools_mode == "standard":
        server = Server("agenthold", instructions=COORDINATION_INSTRUCTIONS)
    else:
        server = Server("agenthold")

    @server.list_tools()  # type: ignore[no-untyped-call, untyped-decorator]
    async def list_tools() -> list[Tool]:  # pragma: no cover
        if tools_mode == "advanced":
            return _advanced_tools()
        return _standard_tools()

    @server.call_tool()  # type: ignore[untyped-decorator]
    async def call_tool(  # pragma: no cover
        name: str, arguments: dict[str, Any]
    ) -> list[TextContent]:
        if tools_mode == "advanced":
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
        else:
            if name == "agenthold_wait":
                result = await _wait_standard(
                    coordinator,
                    resource=arguments["resource"],
                    timeout_seconds=float(arguments.get("timeout_seconds", 30.0)),
                )
            else:
                result = _dispatch_standard(coordinator, name, arguments)
        return [TextContent(type="text", text=json.dumps(result, indent=2))]

    return server


# -----------------------------------------------------------------------
# Standard mode dispatch (claim / release / status)
# -----------------------------------------------------------------------


def _dispatch_standard(
    coordinator: Coordinator,
    name: str,
    args: dict[str, Any],
) -> dict[str, Any]:
    """Synchronous dispatch for standard-mode tools."""
    try:
        return _dispatch_standard_tool(coordinator, name, args)
    except BusyError:
        return {
            "status": "busy",
            "message": "Database temporarily locked. Retry after a short delay.",
            "hint": "Retry after a short delay.",
        }
    except ValueError as e:
        return {"status": "error", "message": str(e)}


def _dispatch_standard_tool(
    coordinator: Coordinator,
    name: str,
    args: dict[str, Any],
) -> dict[str, Any]:
    """Route a standard-mode tool call to the coordinator."""
    if name == "agenthold_claim":
        return coordinator.claim(args["resource"], args["agent"])

    elif name == "agenthold_release":
        return coordinator.release(args["resource"], args["agent"])

    elif name == "agenthold_status":
        return coordinator.status(args["resource"])

    else:
        return {"status": "error", "message": f"Unknown tool: {name}"}


async def _wait_standard(
    coordinator: Coordinator,
    resource: str,
    timeout_seconds: float,
) -> dict[str, Any]:
    """Async poll loop for standard-mode agenthold_wait."""
    try:
        _validate_identifier(resource, "resource")
        normalized = Coordinator._normalize_resource(resource)
        if not normalized:
            raise ValueError("resource must not be empty after normalization")
    except ValueError as e:
        return {"status": "error", "message": str(e)}
    if timeout_seconds < 0:
        return {"status": "error", "message": "timeout_seconds must be >= 0"}

    start = time.monotonic()
    deadline = start + timeout_seconds

    while True:
        try:
            state = coordinator.interpret_state(resource)
        except BusyError:
            # Transient lock contention — keep polling.
            now = time.monotonic()
            if now >= deadline:
                return {
                    "status": "timeout",
                    "resource": coordinator._normalize_resource(resource),
                    "elapsed_seconds": round(now - start, 3),
                    "hint": (
                        "Timed out while the database was locked. "
                        "Retry after a short delay."
                    ),
                }
            await asyncio.sleep(0.2)
            continue

        if state["state"] in ("unclaimed", "free"):
            return {
                "status": "available",
                "resource": state["resource"],
                "elapsed_seconds": round(time.monotonic() - start, 3),
            }

        now = time.monotonic()
        if now >= deadline:
            return {
                "status": "timeout",
                "resource": state["resource"],
                "held_by": state.get("held_by", "unknown"),
                "claimed_at": state.get("claimed_at", "unknown"),
                "elapsed_seconds": round(now - start, 3),
                "hint": (
                    "The resource was not released within the timeout. "
                    "Try working on a different resource, or call "
                    "agenthold_wait again with a longer timeout."
                ),
            }

        await asyncio.sleep(0.2)


# -----------------------------------------------------------------------
# Advanced mode dispatch (get / set / list / history / delete / ...)
# -----------------------------------------------------------------------


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
    except ValueError as e:
        return {"status": "error", "message": str(e)}


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
            if args.get("force", False):
                expected_version = None
            else:
                expected_version = int(args["expected_version"])
            result = store.set(
                namespace=args["namespace"],
                key=args["key"],
                value=args["value"],
                updated_by=args["updated_by"],
                expected_version=expected_version,
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
            if args.get("force", False):
                expected_version = None
            else:
                expected_version = int(args["expected_version"])
            deleted_version = store.delete(
                namespace=args["namespace"],
                key=args["key"],
                deleted_by=args["deleted_by"],
                expected_version=expected_version,
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
    try:
        _validate_identifier(namespace, "namespace")
        _validate_identifier(key, "key")
    except ValueError as e:
        return {"status": "error", "message": str(e)}
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
        except BusyError:
            # Transient lock contention — keep polling.
            pass

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


async def _run_server(  # pragma: no cover
    db_path: str | Path, tools_mode: str = "standard"
) -> None:
    server = make_server(db_path, tools_mode=tools_mode)
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
    parser.add_argument(
        "--tools",
        choices=["standard", "advanced"],
        default="standard",
        help=(
            "Tool set: 'standard' (claim/release/status/wait) or "
            "'advanced' (raw get/set/delete/watch/list/history/clear/export)"
        ),
    )
    args = parser.parse_args()
    asyncio.run(_run_server(args.db, tools_mode=args.tools))


if __name__ == "__main__":  # pragma: no cover
    main()
