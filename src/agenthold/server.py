"""
Agenthold MCP server.

Two tool modes:

  standard (default):
    Five high-level tools for plug-and-play coordination:
      agenthold_register : register and receive a unique agent ID
      agenthold_claim    : claim exclusive access to a resource
      agenthold_release  : release your claim with an explicit outcome
      agenthold_status   : check if a resource is available
      agenthold_wait     : wait for a resource to become available

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

Resource identification (standard mode):
  Resources are identified by a single ``resource`` string in either:
    - URI form: 'file://<workspace>/<path>' or 'custom://<name>'
    - Bare path: 'src/main.py' (resolved against the 'default' workspace,
      or the only workspace if exactly one is configured)
  Configured workspaces come from --workspace flags.

Usage:
  agenthold --db ./state.db                        # default workspace = CWD
  agenthold --workspace myproj=/abs/path           # named workspace
  agenthold --workspace a=/x --workspace b=/y      # multiple workspaces
  agenthold --tools advanced                       # advanced mode
  agenthold --claim-ttl 1800                       # standard + 30 min TTL
  agenthold --transport http --port 8417           # serve over Streamable HTTP
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import uvicorn
from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import TextContent, Tool
from starlette.applications import Starlette
from starlette.routing import Route
from starlette.types import Receive, Scope, Send

from agenthold.coordinator import Coordinator
from agenthold.exceptions import BusyError, ConflictError, NotFoundError
from agenthold.resources import (
    DEFAULT_WORKSPACE_NAME,
    Workspace,
    WorkspaceRegistry,
)
from agenthold.store import StateStore, _validate_identifier

# ---------------------------------------------------------------------------
# Tool input field schemas
# ---------------------------------------------------------------------------

# Standard-mode resource string field (claim/release/status/wait input)
_RESOURCE_FIELD: dict[str, Any] = {
    "type": "string",
    "description": (
        "The resource identifier. Either a workspace-relative path "
        "(e.g. 'src/main.py'), or a URI of the form "
        "'file://<workspace>/<path>' or 'custom://<name>'. Bare paths "
        "resolve against the workspace named 'default'; if no 'default' "
        "exists, the only configured workspace is used; otherwise pass a "
        "URI. Forward slashes only. No '..' segments."
    ),
}

_AGENT_ID_FIELD: dict[str, Any] = {
    "type": "string",
    "description": (
        "Your agent ID, received from agenthold_register. "
        "You must register before calling this tool."
    ),
}

_OUTCOME_FIELD: dict[str, Any] = {
    "type": "string",
    "enum": ["released", "modified", "created", "deleted", "moved"],
    "default": "released",
    "description": (
        "What you did to the resource while holding the claim. "
        "'released' (default) means no lifecycle change. 'modified' means "
        "you changed it in place. 'created' means it didn't exist before. "
        "'deleted' means it no longer exists at this resource. 'moved' "
        "means you moved it — also pass moved_to with the new resource. "
        "The outcome is preserved and shown to the next claimant so they "
        "don't act on stale assumptions."
    ),
}

_MOVED_TO_FIELD: dict[str, Any] = {
    "type": "string",
    "description": (
        "Required when outcome='moved'. The resource string for the new "
        "location, in the same form as 'resource'. Cross-workspace moves "
        "are allowed: e.g. moved_to='file://other-workspace/path'."
    ),
}

# Advanced-mode (existing) field constants
_NS_FIELD: dict[str, Any] = {
    "type": "string",
    "description": "Workflow or resource identifier, e.g. 'order-1234'",
}
_KEY_FIELD: dict[str, Any] = {
    "type": "string",
    "description": "The state key, e.g. 'status'",
}


# ---------------------------------------------------------------------------
# Coordination instructions (standard mode only)
# ---------------------------------------------------------------------------

_INSTRUCTIONS_TEMPLATE = """\
You have access to agenthold, a resource coordination system that \
prevents conflicts when multiple agents work in the same environment.

Resources are identified by either:
  - A workspace-relative path, e.g. "src/main.py" (uses the 'default' \
workspace).
  - A URI, e.g. "file://<workspace>/<path>" for files, or \
"custom://<name>" for opaque names.
{workspace_section}
RULES:

0. FIRST, call agenthold_register with your name and model to receive \
a unique agent_id. Use this agent_id for all subsequent calls.

1. BEFORE modifying any file or shared resource, call agenthold_claim \
with the resource and your agent_id. Do not proceed until the claim is \
granted. Claim each resource right before you modify it — do not claim \
multiple resources in advance.

2. If agenthold_claim returns "busy", do NOT modify the resource. \
Choose a different resource, or call agenthold_wait.

3. AFTER finishing modifications, call agenthold_release with an \
explicit `outcome`:
   - "released" (default): no lifecycle change to declare
   - "modified": you modified the resource in place
   - "created": you created the resource (didn't exist before)
   - "deleted": you deleted the resource at this location
   - "moved": you moved it elsewhere — also pass `moved_to` with the new \
resource

4. For renames (mv old new): claim BOTH old and new, do the rename, \
then release old with outcome="moved" and moved_to="new", and release \
new with outcome="created".

5. When a claim response includes `previous_outcome`, the previous \
holder did something significant (deleted, moved, abandoned, expired). \
Read the `hint` to decide how to proceed.\
"""


def _format_workspace_section(registry: WorkspaceRegistry) -> str:
    """Render the configured-workspaces block for the instructions."""
    workspaces = registry.workspaces
    if not workspaces:
        return ""
    bare_default = registry.default_for_bare_paths()
    bare_default_name = bare_default.name if bare_default is not None else None
    lines = ["", "Configured workspaces:"]
    for ws in workspaces:
        marker = " (default for bare paths)" if ws.name == bare_default_name else ""
        lines.append(f"  - {ws.name}: {ws.root}{marker}")
    return "\n".join(lines) + "\n"


def coordination_instructions(registry: WorkspaceRegistry) -> str:
    return _INSTRUCTIONS_TEMPLATE.format(
        workspace_section=_format_workspace_section(registry)
    )


# Kept for backward-compatibility with anyone importing the constant; will
# render with no workspaces, which is fine for that usage.
COORDINATION_INSTRUCTIONS = _INSTRUCTIONS_TEMPLATE.format(workspace_section="")


# ---------------------------------------------------------------------------
# Tool definitions
# ---------------------------------------------------------------------------


def _standard_tools() -> list[Tool]:
    """Return the five high-level coordination tools."""
    return [
        Tool(
            name="agenthold_register",
            description=(
                "Register yourself and receive a unique agent_id. "
                "IMPORTANT: You MUST call this once before using any "
                "other agenthold tool that requires an agent_id. "
                "Pass your name (e.g. 'editor-agent') and optionally "
                "the model you are running on (e.g. 'claude-sonnet-4-6'). "
                "The returned agent_id is your identity for this session "
                "— use it in all subsequent agenthold_claim and "
                "agenthold_release calls. "
                "Do not call this more than once per session."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "name": {
                        "type": "string",
                        "description": (
                            "A short descriptive name for your agent, "
                            "e.g. 'editor-agent' or 'review-bot'."
                        ),
                    },
                    "model": {
                        "type": "string",
                        "description": (
                            "The model you are running on, "
                            "e.g. 'claude-sonnet-4-6'. Optional."
                        ),
                    },
                },
                "required": ["name"],
            },
        ),
        Tool(
            name="agenthold_claim",
            description=(
                "Claim exclusive access to a resource before modifying it. "
                "IMPORTANT: You MUST call this before editing any file or "
                "shared resource when other agents may be working in the "
                "same environment. Do not proceed with modifications until "
                "the claim is granted. "
                "Claim each resource right before you modify it — do not "
                "claim multiple resources in advance. Finish editing and "
                "release one resource before claiming the next. "
                "You must call agenthold_register first to get an agent_id. "
                "Pass the resource string (e.g. 'src/main.py' or "
                "'file://myproj/src/main.py'). "
                "Possible responses: "
                '"claimed": You now hold exclusive access. The response '
                "may include `previous_outcome` if the resource has prior "
                "history (e.g. 'deleted', 'moved', 'abandoned', 'expired'); "
                "read the `hint` to decide how to proceed. "
                '"already_claimed": You already hold this claim. Safe to '
                "proceed. "
                '"busy": Another agent is working on this resource. Do NOT '
                "modify it. Work on a different resource, or call "
                "agenthold_wait to be notified when it becomes available."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "resource": _RESOURCE_FIELD,
                    "agent_id": _AGENT_ID_FIELD,
                },
                "required": ["resource", "agent_id"],
            },
        ),
        Tool(
            name="agenthold_release",
            description=(
                "Release your exclusive claim on a resource after finishing "
                "your edits. "
                "IMPORTANT: Always pass an explicit `outcome` describing "
                "what you did: 'released' (default) for no change, "
                "'modified' for in-place edits, 'created' for new "
                "resources, 'deleted' for removed resources, or 'moved' "
                "for renames (with `moved_to` set to the new resource). "
                "The outcome is preserved and shown to the next claimant "
                "so they don't act on stale assumptions. "
                "For renames (mv old new), claim BOTH paths first, do the "
                "rename, then release old with outcome='moved' and "
                "moved_to='new', and release new with outcome='created'. "
                "Possible responses: "
                '"released": Claim released. Other agents can now claim '
                "the resource. The response echoes the outcome and any "
                "moved_to. "
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
                    "agent_id": _AGENT_ID_FIELD,
                    "outcome": _OUTCOME_FIELD,
                    "moved_to": _MOVED_TO_FIELD,
                },
                "required": ["resource", "agent_id"],
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
                '"available": The resource is free. The response may '
                "include `previous_outcome` and a `hint` if the previous "
                "holder did something significant (deleted, moved, etc.). "
                "Call agenthold_claim to secure it before editing. "
                '"claimed": Another agent holds this resource. The '
                "response tells you who and when."
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
                '"available": The resource is now free. The response may '
                "include `previous_outcome` describing what the previous "
                "holder did (e.g. 'moved' with moved_to). Call "
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


# ---------------------------------------------------------------------------
# Server construction
# ---------------------------------------------------------------------------


def make_server(
    db_path: str | Path,
    workspaces: list[Workspace] | None = None,
    tools_mode: str = "standard",
    claim_ttl: float | None = None,
) -> Server:
    if workspaces is None:
        workspaces = [Workspace(name=DEFAULT_WORKSPACE_NAME, root=os.getcwd())]
    registry = WorkspaceRegistry(workspaces)
    store = StateStore(db_path)
    coordinator = Coordinator(store, registry, claim_ttl=claim_ttl)
    return _make_server_from_parts(store, coordinator, registry, tools_mode)


def _make_server_from_parts(
    store: StateStore,
    coordinator: Coordinator,
    registry: WorkspaceRegistry,
    tools_mode: str = "standard",
) -> Server:
    if tools_mode == "standard":
        server = Server(
            "agenthold",
            instructions=coordination_instructions(registry),
        )
    else:
        server = Server("agenthold")

    @server.list_tools()  # type: ignore[no-untyped-call, untyped-decorator]
    async def list_tools() -> list[Tool]:
        if tools_mode == "advanced":
            return _advanced_tools()
        return _standard_tools()

    @server.call_tool()  # type: ignore[untyped-decorator]
    async def call_tool(name: str, arguments: dict[str, Any]) -> list[TextContent]:
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


# ---------------------------------------------------------------------------
# HTTP (Streamable HTTP) transport
# ---------------------------------------------------------------------------


class _StreamableHTTPASGIApp:
    """ASGI wrapper around the session manager's request handler.

    Passed to a Starlette ``Route`` as the endpoint. A class instance (rather
    than a bound method) is required so Starlette treats it as a raw ASGI app
    and matches the path exactly — a bound method would be wrapped as a
    request/response endpoint and trigger a trailing-slash redirect on every
    request.
    """

    def __init__(self, manager: StreamableHTTPSessionManager) -> None:
        self._manager = manager

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        await self._manager.handle_request(scope, receive, send)


def _security_settings_from_allowed_hosts(
    allowed_hosts: list[str] | None,
) -> TransportSecuritySettings | None:
    """Build DNS-rebinding protection settings, or None to leave it disabled.

    Passing None to the transport disables DNS-rebinding protection (the SDK's
    backward-compatible default), so out-of-the-box localhost clients are not
    rejected. Supplying at least one allowed host opts into protection.
    """
    if not allowed_hosts:
        return None
    return TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=list(allowed_hosts),
    )


def build_http_app(
    store: StateStore,
    coordinator: Coordinator,
    registry: WorkspaceRegistry,
    tools_mode: str = "standard",
    *,
    path: str = "/mcp",
    json_response: bool = False,
    security_settings: TransportSecuritySettings | None = None,
) -> Starlette:
    """Build a Starlette ASGI app serving agenthold over Streamable HTTP.

    The MCP server is the same low-level Server used for stdio; only the
    transport differs. A StreamableHTTPSessionManager tracks one MCP session
    per connected client. The session manager's task group is established by
    the app's lifespan; on shutdown, standard-mode agent claims are released
    (mirroring the stdio cleanup path).
    """
    server = _make_server_from_parts(store, coordinator, registry, tools_mode)
    manager = StreamableHTTPSessionManager(
        server,
        json_response=json_response,
        stateless=False,
        security_settings=security_settings,
    )

    @asynccontextmanager
    async def lifespan(_app: Starlette) -> AsyncIterator[None]:
        async with manager.run():
            yield
        if tools_mode == "standard":
            _cleanup_agents(coordinator)

    return Starlette(
        routes=[Route(path, endpoint=_StreamableHTTPASGIApp(manager))],
        lifespan=lifespan,
    )


# ---------------------------------------------------------------------------
# Standard mode dispatch (claim / release / status)
# ---------------------------------------------------------------------------


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
    except ConflictError as e:
        return {
            "status": "error",
            "message": f"Unexpected conflict: {e}",
            "hint": "Retry the operation.",
        }
    except ValueError as e:
        return {"status": "error", "message": str(e)}


def _dispatch_standard_tool(
    coordinator: Coordinator,
    name: str,
    args: dict[str, Any],
) -> dict[str, Any]:
    """Route a standard-mode tool call to the coordinator."""
    if name == "agenthold_register":
        return coordinator.register(
            name=args["name"],
            model=args.get("model", ""),
        )

    if name == "agenthold_claim":
        agent_id = args["agent_id"]
        if not coordinator.is_registered(agent_id):
            return {
                "status": "error",
                "message": "Unknown agent_id. Call agenthold_register first.",
            }
        coordinator.refresh_agent(agent_id)
        return coordinator.claim(args["resource"], agent_id)

    if name == "agenthold_release":
        agent_id = args["agent_id"]
        if not coordinator.is_registered(agent_id):
            return {
                "status": "error",
                "message": "Unknown agent_id. Call agenthold_register first.",
            }
        coordinator.refresh_agent(agent_id)
        return coordinator.release(
            args["resource"],
            agent_id,
            outcome=args.get("outcome", "released"),
            moved_to=args.get("moved_to"),
        )

    if name == "agenthold_status":
        return coordinator.status(args["resource"])

    return {"status": "error", "message": f"Unknown tool: {name}"}


async def _wait_standard(
    coordinator: Coordinator,
    resource: Any,
    timeout_seconds: float,
) -> dict[str, Any]:
    """Async poll loop for standard-mode agenthold_wait."""
    try:
        rid = coordinator.canonicalize(resource)
    except ValueError as e:
        return {"status": "error", "message": str(e)}
    if timeout_seconds < 0:
        return {"status": "error", "message": "timeout_seconds must be >= 0"}

    key = rid.to_uri()
    start = time.monotonic()
    deadline = start + timeout_seconds

    while True:
        try:
            state = coordinator._interpret_state_by_uri(key)
        except BusyError:
            now = time.monotonic()
            if now >= deadline:
                return {
                    "status": "timeout",
                    "resource": key,
                    "elapsed_seconds": round(now - start, 3),
                    "hint": (
                        "Timed out while the database was locked. "
                        "Retry after a short delay."
                    ),
                }
            await asyncio.sleep(0.2)
            continue

        if state["state"] in ("unclaimed", "free", "expired"):
            response: dict[str, Any] = {
                "status": "available",
                "resource": key,
                "elapsed_seconds": round(time.monotonic() - start, 3),
            }
            if state["state"] == "free":
                outcome = state.get("outcome", "released")
                response["previous_outcome"] = outcome
                response["previous_holder"] = state.get("released_by", "unknown")
                response["previous_outcome_at"] = state.get("released_at", "unknown")
                if outcome == "moved" and state.get("moved_to"):
                    response["moved_to"] = state["moved_to"]
                hint = Coordinator._hint_for_outcome(outcome)
                if hint:
                    response["hint"] = hint
            elif state["state"] == "expired":
                response["previous_outcome"] = "expired"
                response["previous_holder"] = state.get("held_by", "unknown")
                response["previous_outcome_at"] = state.get("claimed_at", "unknown")
                hint = Coordinator._hint_for_outcome("expired")
                if hint:
                    response["hint"] = hint
            return response

        now = time.monotonic()
        if now >= deadline:
            return {
                "status": "timeout",
                "resource": key,
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


# ---------------------------------------------------------------------------
# Advanced mode dispatch (get / set / list / history / delete / ...)
# ---------------------------------------------------------------------------


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
            pass
        except BusyError:
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


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _parse_workspace_arg(arg: str) -> Workspace:
    """Parse a --workspace argument.

    Accepted forms:
      name=path           Both required; explicit naming.
      /abs/path           Absolute path; name derived from final component.
                          On Windows, the path may use backslashes.
    """
    if "=" in arg:
        name, _, root = arg.partition("=")
        if not name or not root:
            raise ValueError(f"Invalid --workspace value {arg!r}: expected 'name=path'")
        return Workspace(name=name, root=root)
    # Path-only form — derive name from basename
    norm = arg.replace("\\", "/").rstrip("/")
    if not norm.startswith("/") and not (
        len(norm) >= 3 and norm[1] == ":" and norm[2] == "/"
    ):
        raise ValueError(
            f"Invalid --workspace value {arg!r}: expected 'name=path' or "
            "an absolute path"
        )
    name = norm.rsplit("/", 1)[-1]
    if not name:
        name = DEFAULT_WORKSPACE_NAME
    return Workspace(name=name, root=arg)


def _build_workspaces(raw_args: list[str] | None) -> list[Workspace]:
    if not raw_args:
        return [Workspace(name=DEFAULT_WORKSPACE_NAME, root=os.getcwd())]
    return [_parse_workspace_arg(a) for a in raw_args]


async def _run_server(  # pragma: no cover
    db_path: str | Path,
    workspaces: list[Workspace],
    tools_mode: str = "standard",
    claim_ttl: float | None = None,
) -> None:
    registry = WorkspaceRegistry(workspaces)
    store = StateStore(db_path)
    coordinator = Coordinator(store, registry, claim_ttl=claim_ttl)
    server = _make_server_from_parts(
        store, coordinator, registry, tools_mode=tools_mode
    )
    async with stdio_server() as (read_stream, write_stream):
        init_options = server.create_initialization_options()
        try:
            await server.run(read_stream, write_stream, init_options)
        finally:
            if tools_mode == "standard":
                _cleanup_agents(coordinator)


async def _run_http(  # pragma: no cover
    db_path: str | Path,
    workspaces: list[Workspace],
    tools_mode: str = "standard",
    claim_ttl: float | None = None,
    *,
    host: str = "127.0.0.1",
    port: int = 8417,
    path: str = "/mcp",
    json_response: bool = False,
    allowed_hosts: list[str] | None = None,
) -> None:
    registry = WorkspaceRegistry(workspaces)
    store = StateStore(db_path)
    coordinator = Coordinator(store, registry, claim_ttl=claim_ttl)
    app = build_http_app(
        store,
        coordinator,
        registry,
        tools_mode,
        path=path,
        json_response=json_response,
        security_settings=_security_settings_from_allowed_hosts(allowed_hosts),
    )
    config = uvicorn.Config(app, host=host, port=port, log_level="info")
    await uvicorn.Server(config).serve()


def _cleanup_agents(  # pragma: no cover
    coordinator: Coordinator,
) -> None:
    """Release claims and deactivate agents on disconnect.

    Scoped to agents registered through this Coordinator's process via
    session_agent_ids. Agents recognized via DB fallback (registered by
    other processes) are intentionally NOT cleaned up here — only their
    owning process should.
    """
    for agent_id in coordinator.session_agent_ids:
        coordinator.release_all(agent_id)
        coordinator.deactivate_agent(agent_id)


def _build_arg_parser() -> argparse.ArgumentParser:
    """Construct the CLI argument parser.

    Factored out of main() so argument parsing is unit-testable without
    spawning a server.
    """
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
    parser.add_argument(
        "--claim-ttl",
        type=float,
        default=None,
        help=(
            "Seconds before an inactive agent's claims expire "
            "(default: no expiry). Only applies in standard mode. "
            "Recommended when using --transport http, where the server is "
            "long-lived and serves many agents over time."
        ),
    )
    parser.add_argument(
        "--workspace",
        action="append",
        default=None,
        help=(
            "Configure a workspace as 'name=path' (e.g. 'myproj=/abs/path') "
            "or as an absolute path (name derived from basename). "
            "Repeatable. If omitted, a single workspace named 'default' is "
            "created at the current working directory."
        ),
    )
    parser.add_argument(
        "--transport",
        choices=["stdio", "http"],
        default="stdio",
        help=(
            "Transport to serve on: 'stdio' (default; one local subprocess per "
            "agent) or 'http' (a single long-lived server that many agents "
            "connect to over Streamable HTTP)."
        ),
    )
    parser.add_argument(
        "--host",
        default="127.0.0.1",
        help=(
            "HTTP bind address (default: 127.0.0.1). Only used with "
            "--transport http. Binding beyond localhost exposes agenthold with "
            "no authentication — not recommended."
        ),
    )
    parser.add_argument(
        "--port",
        type=int,
        default=8417,
        help="HTTP port (default: 8417). Only used with --transport http.",
    )
    parser.add_argument(
        "--path",
        default="/mcp",
        help=(
            "HTTP endpoint path the MCP transport is mounted at "
            "(default: /mcp). Only used with --transport http."
        ),
    )
    parser.add_argument(
        "--json-response",
        action="store_true",
        help=(
            "Return JSON responses instead of SSE streams over HTTP. "
            "Only used with --transport http."
        ),
    )
    parser.add_argument(
        "--allowed-host",
        action="append",
        default=None,
        dest="allowed_host",
        help=(
            "Enable DNS-rebinding protection and allow this Host header value. "
            "Repeatable. When omitted, protection stays disabled "
            "(localhost-friendly default). Only used with --transport http."
        ),
    )
    return parser


def main() -> None:  # pragma: no cover
    args = _build_arg_parser().parse_args()
    workspaces = _build_workspaces(args.workspace)
    if args.transport == "http":
        asyncio.run(
            _run_http(
                args.db,
                workspaces=workspaces,
                tools_mode=args.tools,
                claim_ttl=args.claim_ttl,
                host=args.host,
                port=args.port,
                path=args.path,
                json_response=args.json_response,
                allowed_hosts=args.allowed_host,
            )
        )
    else:
        asyncio.run(
            _run_server(
                args.db,
                workspaces=workspaces,
                tools_mode=args.tools,
                claim_ttl=args.claim_ttl,
            )
        )


if __name__ == "__main__":  # pragma: no cover
    main()
