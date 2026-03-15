# Changelog

All notable changes to this project will be documented here.

Format follows [Keep a Changelog](https://keepachangelog.com/en/1.0.0/).
This project uses [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

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
