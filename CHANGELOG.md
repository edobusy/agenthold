# Changelog

All notable changes to this project will be documented here.

Format follows [Keep a Changelog](https://keepachangelog.com/en/1.0.0/).
This project uses [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

---

## [0.5.0] - 2026-05-08

### Breaking Changes
- **Resource identification overhaul**: standard-mode tools now require
  resources to be passed as canonical strings, parsed against a configured
  workspace registry. Two forms are accepted: workspace-relative bare paths
  (e.g. `"src/main.py"`) and explicit URIs (e.g. `"file://myproj/src/main.py"`
  or `"custom://task-42"`). Bare paths resolve to the workspace named
  `default`, or the only configured workspace if exactly one exists.
  Equivalent inputs (`"src/main.py"`, `"./src/main.py"`, `"src\\main.py"`,
  `"src//main.py"`, an absolute path inside the workspace) all canonicalize
  to the same internal URI. Path traversal (`..`) and dot segments (`.`)
  are rejected at the boundary.
- **`agenthold_release` now takes an explicit `outcome`**: agents declare
  what they did to the resource — `released` (default), `modified`,
  `created`, `deleted`, or `moved`. For `moved`, a `moved_to` field is
  required. The outcome is preserved in the free-state record and surfaced
  on subsequent `agenthold_claim`, `agenthold_wait`, and `agenthold_status`
  responses as `previous_outcome`, so the next claimant can reason about
  what the previous holder did.
- **`Coordinator` constructor**: now requires a `WorkspaceRegistry` as its
  second argument.
- **`make_server` / `_run_server`**: now take a `workspaces: list[Workspace]`
  parameter (defaults to a single `default` workspace at the current working
  directory if not provided).

### Added
- **`--workspace name=path` CLI flag** (repeatable): configures a workspace
  for resource identification. Path-only form (e.g. `--workspace /abs/path`)
  derives the name from the path's basename. If omitted, a single workspace
  named `default` is created at the current working directory.
- **Server-set outcomes for involuntary release**:
  - `abandoned`: written by `release_all` during disconnect cleanup, telling
    the next claimant the holder did not get to declare an outcome.
  - `expired`: synthesized at TTL takeover and surfaced in
    `previous_outcome` so the next claimant knows the resource was reclaimed
    from an inactive holder.
- **Lifecycle hints**: `agenthold_claim` / `agenthold_wait` / `agenthold_status`
  responses include a `hint` string when the previous outcome was non-trivial
  (`deleted`, `moved`, `abandoned`, `expired`), nudging the agent toward the
  right next action.
- **Multi-workspace support**: same path under different workspaces is
  isolated. Cross-workspace moves are honest — `moved_to` may carry a URI
  in any configured workspace.
- New `agenthold.resources` module exposing `Workspace`, `WorkspaceRegistry`,
  `ResourceId`, `parse_resource_input`.
- 68 new tests in `tests/test_resources.py` covering workspace validation,
  URI parsing, bare-path canonicalization, longest-prefix matching, and
  rejection of path traversal / dot segments / oversize input.
- New tests in `test_coordinator.py` and `test_server_standard.py` covering
  outcomes, previous_outcome propagation, abandoned/expired surfacing,
  rename flows, and multi-workspace isolation.

### Changed
- **`COORDINATION_INSTRUCTIONS` is now dynamic**: the rendered instruction
  text includes the configured workspaces and identifies which one is the
  default for bare paths. The module-level `COORDINATION_INSTRUCTIONS`
  constant remains for back-compat (renders with no workspace block); use
  `coordination_instructions(registry)` to get the up-to-date version.
- Tool descriptions for `agenthold_claim`, `agenthold_release`,
  `agenthold_status`, `agenthold_wait` updated to document the new resource
  string format and outcome semantics.
- The `claims` namespace now stores entries keyed by canonical URI (e.g.
  `file://default/src/main.py`) instead of an ad-hoc normalized path. Old
  databases will work but their existing claim records will be unreachable
  through the new API; wipe and recreate the DB on upgrade.
- `Coordinator._normalize_resource` removed (replaced by
  `agenthold.resources.parse_resource_input`).

---

## [0.4.3] - 2026-04-15

### Added
- **MCP Registry publishing**: added `server.json` at the repo root describing the
  PyPI-distributed stdio server, with light/dark icons, `--tools` and `--db`
  argument hints, and schema-validated metadata for the
  `io.github.edobusy/agenthold` namespace.
- README now carries the `mcp-name:` ownership marker required by the official
  MCP registry to bind the PyPI package to this namespace.
- GitHub Actions `publish.yml` extended to auto-publish to the MCP registry
  (via GitHub OIDC) immediately after each successful PyPI release, with a
  pre-flight version-sync check and schema validation.

---

## [0.4.2] - 2026-03-20

### Changed
- **README overhaul**: rewrote opening with a stronger hook and "git for agents'
  working memory" framing; added PyPI downloads, coverage, and Ruff badges; added
  "Works with any agent framework" section covering LangChain, CrewAI, OpenAI Agents
  SDK, AutoGen, Google ADK, and PydanticAI; added Mermaid architecture diagram; added
  inline "What it looks like in practice" snippet; wrapped technical notes in a
  collapsible section; reduced README from 626 to 423 lines
- **Advanced tools extracted**: moved eight advanced tool docs to
  `docs/advanced-tools.md` with a link from the README
- **PyPI metadata enriched**: added `Development Status :: 4 - Beta`,
  `Intended Audience :: Developers`, `Typing :: Typed`, and other classifiers;
  added Changelog and Documentation URLs to `project.urls`

### Added
- `.github/ISSUE_TEMPLATE/bug_report.md`: structured bug report template
- `.github/ISSUE_TEMPLATE/feature_request.md`: structured feature request template
- `.github/ISSUE_TEMPLATE/config.yml`: allows blank issues alongside templates
- `.github/pull_request_template.md`: PR template with all five quality gates as a
  checklist

---

## [0.4.1] - 2026-03-18

### Fixed
- **Sequential claiming guidance**: `COORDINATION_INSTRUCTIONS` and
  `agenthold_claim` tool description now tell agents to claim one resource at a
  time, right before modifying it, and release before claiming the next. This
  prevents greedy batch-claiming observed in real-world multi-agent sessions.
- **Negative `claim_ttl` rejected**: `Coordinator.__init__` now raises
  `ValueError` if `claim_ttl < 0`, preventing a configuration where all claims
  instantly expire.
- **`interpret_state` input validation**: now calls `_validate_inputs()` instead
  of bare `_normalize_resource()`, so empty strings and null bytes are rejected
  consistently with `claim`/`release`/`status`.
- **`ConflictError` safety net in standard dispatch**: `_dispatch_standard` now
  catches `ConflictError` from the coordinator layer and returns a structured
  error with retry hint, instead of crashing the server.

---

## [0.4.0] - 2026-03-18

### Breaking Changes
- **`agenthold_claim`**: `agent` parameter renamed to `agent_id`. Agents must call
  `agenthold_register` first to receive a server-issued unique ID.
- **`agenthold_release`**: `agent` parameter renamed to `agent_id`. Same registration
  requirement as `agenthold_claim`.
- Standard mode now exposes **5 tools** (was 4). The new `agenthold_register` tool
  must be called before `agenthold_claim` or `agenthold_release`.
- `COORDINATION_INSTRUCTIONS` updated: rule 0 requires registration before any other
  coordination call. Rule 5 (consistent agent name) removed because the server now manages
  identity.

### Added
- **Agent registration** (`agenthold_register`): agents call this once per session to
  receive a unique `agent-<8-hex>` ID. The server stores agent metadata (name, model,
  registration time, last activity) in the `_agents` namespace.
- **Registration enforcement**: `agenthold_claim` and `agenthold_release` reject calls
  from unregistered agents with a structured error pointing to `agenthold_register`.
- **Activity tracking**: every `agenthold_claim` and `agenthold_release` call updates
  the agent's `last_activity` timestamp via `coordinator.refresh_agent()`.
- **TTL-based claim expiry** (`--claim-ttl` CLI flag): when set, claims held by agents
  whose `last_activity` exceeds the TTL are treated as expired. Expired claims can be
  taken by other agents. Falls back to `claimed_at` if the agent record is missing.
- **Disconnect cleanup**: on MCP stdio pipe close, the server releases all claims held
  by agents registered on that process and marks them as inactive.
- **Status enrichment**: `agenthold_status` now includes `agent_name` and `agent_model`
  when the holding agent has a registration record.
- **Expired state**: `interpret_state()` returns `"expired"` for claims past TTL;
  `agenthold_status` reports these as `"available"` with a note; `agenthold_wait`
  treats expired claims as available.
- `Coordinator` gains `claim_ttl` parameter, `register()`, `is_registered()`,
  `refresh_agent()`, `release_all()`, and `deactivate_agent()` methods.
- `make_server()` gains `claim_ttl` parameter.
- 25 new tests covering registration, TTL expiry, release_all, deactivate, status
  enrichment, and dispatch-layer registration enforcement.

---

## [0.3.0] - 2026-03-17

### Added
- **Plug-and-play coordination layer**: four high-level tools that work out of the
  box with zero configuration: no CLAUDE.md, no system prompt changes, no namespace
  design required
  - `agenthold_claim`: claim exclusive access to a resource before modifying it
  - `agenthold_release`: release a claim when done, immediately notifying waiting agents
  - `agenthold_status`: check whether a resource is available or held by another agent
  - `agenthold_wait`: block until a claimed resource becomes available (async poll loop)
- `Coordinator` class (`coordinator.py`): implements the claim lifecycle (unclaimed →
  claimed → free) on top of `StateStore` with OCC conflict handling and single-retry
  race recovery
- **Resource normalization**: `./intro.md`, `intro.md`, and `src\\main.py` all resolve
  to the same claim key (strips `./`, collapses slashes, normalizes backslashes)
- **MCP server instructions**: `COORDINATION_INSTRUCTIONS` constant embedded in
  `server.py`, returned to every MCP client on connection as reinforcement. Tool
  descriptions carry the protocol independently. If the client drops these
  instructions, the system still works.
- `--tools` CLI flag: `standard` (default, 4 high-level tools) or `advanced` (8
  low-level primitives). The store layer is identical in both modes.
- **Release-by-version-bump**: releasing a claim writes a `"free"` state (version
  bump) instead of deleting the key, so `agenthold_wait` fires immediately on
  release instead of timing out
- **Mode-mixing safety**: if an advanced-mode agent writes a non-claim value to the
  `"claims"` namespace, the coordinator treats it as unclaimed and overwrites with
  a proper claim structure on the next claim

### Changed
- Default tool set is now `standard` (4 high-level tools). Use `--tools advanced`
  for the 8 low-level primitives from v0.2.0. No breaking changes to the primitive
  tools; they are all still available in advanced mode.

---

## [0.2.0] - 2026-03-16

### Breaking Changes
- **`agenthold_set`**: `expected_version` is now a required parameter. Agents must
  pass the version from a prior `agenthold_get` (or `0` for a key that should not
  yet exist). To write unconditionally, set the new `force=true` parameter instead
  of omitting `expected_version`.
- **`agenthold_delete`**: `expected_version` is now a required parameter. Same
  pattern: pass the version from a prior read, or set `force=true` to delete
  unconditionally.

### Added
- `force` boolean parameter on `agenthold_set` and `agenthold_delete`: bypasses
  conflict detection when set to `true`; `expected_version` is ignored in this mode
- Input validation for `namespace` and `key` across all tools: rejects empty strings,
  strings containing null bytes, and strings exceeding 512 characters; returns a
  structured `{"status": "error", "message": "..."}` response
- `json.dumps` `TypeError` in `store.set()` is now caught and re-raised as
  `ValueError`, producing a structured error response instead of an unhandled
  exception
- `ValueError` catch in `_dispatch` and `_watch`: all validation errors from the
  store layer now surface as `{"status": "error", "message": "..."}` responses
- Tests for: `force` parameter (set + delete), input validation (empty, null bytes,
  overlength), non-serialisable values, dispatch-layer error responses

---

## [0.1.8] - 2026-03-16

### Fixed
- **Critical**: write transactions now use `BEGIN IMMEDIATE` instead of
  `BEGIN DEFERRED`, fixing a multi-process race where two agenthold processes
  sharing the same database file could both read the same version and both
  write version+1, bypassing OCC conflict detection entirely
- `close()` now acquires `self._lock` before closing the SQLite connection,
  preventing corruption if another thread is mid-operation

### Added
- `PRAGMA busy_timeout=5000`: a second writer now waits up to 5 seconds for
  the write lock instead of failing immediately with a raw
  `sqlite3.OperationalError`
- `BusyError` exception: raised when the busy timeout expires; the MCP server
  returns a structured `{"status": "busy", "hint": "..."}` response so agents
  can retry
- `_read_transaction()` context manager for consistent multi-statement reads
  using `BEGIN DEFERRED`; `export_namespace` now uses this instead of
  `_transaction()`, so exports no longer block writers in WAL mode
- `tests/test_concurrency.py`: 7 new tests covering multi-connection OCC
  correctness, concurrent export + write, busy timeout pragma, BusyError
  propagation, and thread-safe close

---

## [0.1.7] - 2026-03-16

### Added
- `agenthold_export` MCP tool: exports all live records and their complete version
  history for a namespace as a single JSON snapshot, grouped per key; intended for
  debugging coordination issues and building audit trails
- `StateStore.export_namespace()` returns a consistent snapshot via a single
  `_transaction()` call; queries `state_history` without a LIMIT clause so complete
  history is always returned; includes tombstones on re-created keys

---

## [0.1.6] - 2026-03-16

### Added
- `agenthold_clear_namespace` MCP tool: atomically deletes all live records in a
  namespace in a single transaction, writing a tombstone to `state_history` for
  every key so the full lifecycle remains auditable
- `StateStore.clear_namespace()` returns the sorted list of deleted key names;
  returns an empty list (not an error) if the namespace has no records

---

## [0.1.5] - 2026-03-16

### Added
- `agenthold_watch` MCP tool: blocks until a key's version exceeds `since_version`,
  then returns the new record. Polls every 200 ms. Returns `{"status": "timeout"}`
  with a `hint` if the key does not change within `timeout_seconds` (default 10 s).
- `_watch()` module-level async function in `server.py`; routed directly from
  `call_tool` rather than through `_dispatch`, keeping the synchronous dispatch
  path unmodified
- `ASYNC_TOOLS` constant in `test_server.py` as a static record of tools that
  bypass `_dispatch`; guarded by `test_async_tools_are_not_handled_by_dispatch`
- Full test suite for `_watch` in `tests/test_watch.py` (Groups 1–6: immediate
  return, polling, timeout, input validation, response format, isolation)

---

## [0.1.4] - 2026-03-15

### Added
- `agenthold_delete` MCP tool: permanently removes a state record and writes a
  tombstone entry to `state_history` (event_type=`"delete"`) so the full
  lifecycle of a key remains auditable
- `StateStore.delete()` accepts an optional `expected_version` for OCC-safe
  deletes; raises `ConflictError` if the stored version has changed since the
  caller's last read
- `deleted_by` parameter on `StateStore.delete()` records agent identity in the
  tombstone, independent of who last wrote the live record
- Registration drift guard test: `test_all_expected_tools_are_handled_by_dispatch`
  asserts every declared tool has a working `_dispatch` handler

### Fixed
- README now documents all five MCP tools including `agenthold_delete`

---

## [0.1.3] - 2026-03-15

### Fixed
- Corrected misleading `isolation_level=None` comment in `StateStore.__init__`
- `ConflictError` for non-existent keys now reports `updated_by` as
  `"(key does not exist)"` instead of an empty string
- Delete operations now write a tombstone entry to `state_history` so deletions
  are visible in the audit trail
- `StateRecordHistory` is now a subclass of `StateRecord` to enforce shared structure
- Version `__init__.py` was out of sync with `pyproject.toml`; both now read `0.1.3`

### Changed
- Tool descriptions for `agenthold_get`, `agenthold_set`, `agenthold_list`, and
  `agenthold_history` expanded with edge-case guidance and consistent field
  descriptions across all tools
- `agenthold_set` description now documents the `expected_version=0` create-only pattern
- `agenthold_get` description warns that omitting `expected_version` in a subsequent
  `agenthold_set` will silently overwrite concurrent changes
- `agenthold_history` response now includes an `event_type` field (`"write"` or
  `"delete"`) on each history entry
- Removed `default=str` fallback from `json.dumps` in the MCP tool handler;
  serialisation errors now raise explicitly instead of silently corrupting output
- WAL mode and transaction pattern documented accurately in `StateStore` class docstring

### Tests
- Added loop guard to `test_conflict_retry_pattern_converges`
- Added tests for delete tombstone, `ConflictError` `updated_by` sentinel, and
  `event_type` field on history entries
- Removed duplicate `store` fixture from `test_server.py`
- Added JSON serialisation tests for `_dispatch` output

---

## [0.1.0] - 2026-03-15

### Added

- MCP server exposing four tools: `agenthold_get`, `agenthold_set`, `agenthold_list`, `agenthold_history`
- SQLite state store with optimistic concurrency control via `expected_version`
- Append-only version history for every key (`state_history` table)
- `ConflictError` with full conflict detail: expected version, actual version, who wrote it, and when
- `NotFoundError` for missing keys (returned as `{"status": "not_found"}` at the tool level, never a hard error)
- Thread-safe store with a single `threading.Lock` protecting all reads and writes
- WAL mode enabled on the SQLite connection for improved concurrent read performance
- CLI entry point: `agenthold --db ./state.db`
- In-memory store option (`:memory:`) for testing without a file
- Order processing example: two agents updating the same order record concurrently, with and without conflict detection
- Budget allocation example: two agents drawing from a shared budget, demonstrating silent overcommit vs. safe retry
- GitHub Actions CI running on Python 3.11 and 3.12: ruff, mypy, pytest, and coverage gate at 80%
