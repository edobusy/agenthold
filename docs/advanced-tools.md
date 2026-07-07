# Advanced Tools Reference

For power users building custom coordination protocols, agenthold exposes eight low-level primitives via `--tools advanced`:

```bash
agenthold --db ./state.db --tools advanced
```

This gives agents direct access to the versioned state store with full OCC conflict detection. No server instructions are sent in this mode.

> **Transient locks.** Any of these tools may return `{"status": "unavailable", ...}` with a `hint` when the SQLite database is briefly write-locked by another writer. This is not an error and not a conflict — simply retry the same call after a short delay.

[← Back to main README](../README.md)

---

## `agenthold_get`

Read the current value of a state record.

```json
{
  "namespace": "order-1234",
  "key": "status"
}
```

```json
{
  "status": "ok",
  "namespace": "order-1234",
  "key": "status",
  "value": "processing",
  "version": 3,
  "updated_by": "fulfillment-agent",
  "updated_at": "2026-03-15T10:42:00.123456+00:00"
}
```

Returns `{"status": "not_found"}` if the key does not exist. No exception is raised.

---

## `agenthold_set`

Write a value. `expected_version` is required. Pass the version from a prior `agenthold_get`, or `0` for a key that should not yet exist.

```json
{
  "namespace": "order-1234",
  "key": "status",
  "value": "shipped",
  "updated_by": "logistics-agent",
  "expected_version": 3
}
```

**Success:**
```json
{
  "status": "ok",
  "namespace": "order-1234",
  "key": "status",
  "version": 4,
  "previous_version": 3
}
```

**Conflict** (another agent wrote before you):
```json
{
  "status": "conflict",
  "namespace": "order-1234",
  "key": "status",
  "expected_version": 3,
  "actual_version": 5,
  "actual_updated_by": "returns-agent",
  "actual_updated_at": "2026-03-15T10:42:01.456+00:00",
  "hint": "Call agenthold_get to read the current state, merge your changes, and retry with the new version."
}
```

**`expected_version` patterns:**

| Value | Behaviour |
|---|---|
| `0` | Create-only guard. Succeeds only if the key does not yet exist; conflicts if it does |
| `N` (from a prior `agenthold_get`) | Conflict-safe write. Rejected if another agent wrote since your read |

**`force` parameter:** Set `force: true` to write unconditionally, bypassing conflict detection. When `force` is true, `expected_version` is ignored. Use this only for idempotent writes or initial seeding where overwriting is intentional.

---

## `agenthold_list`

List all current state records in a namespace.

```json
{ "namespace": "order-1234" }
```

```json
{
  "status": "ok",
  "namespace": "order-1234",
  "count": 3,
  "records": [
    { "key": "reserved",  "value": true,        "version": 2, "updated_by": "inventory-agent", "updated_at": "..." },
    { "key": "status",    "value": "processing", "version": 3, "updated_by": "fulfillment-agent", "updated_at": "..." },
    { "key": "total",     "value": 80.99,        "version": 2, "updated_by": "pricing-agent",   "updated_at": "..." }
  ]
}
```

---

## `agenthold_history`

Read the version history of a state record, newest first. Useful for debugging coordination issues and auditing writes.

```json
{
  "namespace": "order-1234",
  "key": "status",
  "limit": 5
}
```

```json
{
  "status": "ok",
  "namespace": "order-1234",
  "key": "status",
  "history": [
    { "version": 3, "value": "processing",  "updated_by": "fulfillment-agent", "updated_at": "...", "event_type": "write" },
    { "version": 2, "value": "validated",   "updated_by": "validation-agent",  "updated_at": "...", "event_type": "write" },
    { "version": 1, "value": "received",    "updated_by": "intake-agent",      "updated_at": "...", "event_type": "write" }
  ]
}
```

Each entry includes an `event_type` field: `"write"` for normal writes, `"delete"` for deletion events. Delete tombstones have `value: null`. An empty history list means no writes have been recorded for this key. The key may not exist. Use `agenthold_get` to check current state.

---

## `agenthold_delete`

Permanently remove a state record. The deletion is written as a tombstone in `agenthold_history` so the full lifecycle of the key remains auditable.

```json
{
  "namespace": "order-1234",
  "key": "status",
  "deleted_by": "cleanup-agent",
  "expected_version": 4
}
```

**Success:**
```json
{
  "status": "ok",
  "namespace": "order-1234",
  "key": "status",
  "deleted_by": "cleanup-agent",
  "deleted_version": 4
}
```

`expected_version` is required. Pass the version from a prior `agenthold_get` to prevent accidentally deleting a record that was updated since your read. Set `force: true` to delete unconditionally (bypasses conflict detection; `expected_version` is ignored).

---

## `agenthold_export`

Export all live records and their complete version history for a namespace as a single JSON snapshot. Intended for debugging coordination issues and building audit trails.

```json
{ "namespace": "order-1234" }
```

```json
{
  "status": "ok",
  "namespace": "order-1234",
  "exported_at": "2026-03-16T10:00:00.123456+00:00",
  "record_count": 2,
  "history_count": 5,
  "records": [
    {
      "key": "status",
      "value": "shipped",
      "version": 3,
      "updated_by": "logistics-agent",
      "updated_at": "2026-03-16T09:59:00+00:00",
      "history": [
        {"version": 3, "value": "shipped",    "event_type": "write", "updated_by": "logistics-agent",   "updated_at": "..."},
        {"version": 2, "value": "processing", "event_type": "write", "updated_by": "fulfillment-agent", "updated_at": "..."},
        {"version": 1, "value": "received",   "event_type": "write", "updated_by": "intake-agent",      "updated_at": "..."}
      ]
    }
  ]
}
```

Records are sorted alphabetically by key. History entries are ordered newest first. `history_count` is the total across all keys and includes delete tombstones (which have `value: null`). Only live (non-deleted) keys are included; use `agenthold_history` to inspect deleted keys.

---

## `agenthold_watch`

Wait for a key's version to change, then return the new value. Returns
near-instantly when the write happens on the same server, and within ~200 ms for
changes made by another process (a bounded fallback check).

> **Important:** This call holds the agent turn until it returns. No other actions can be taken while waiting. Only use this when the agent has nothing else to do until the key changes.

```json
{
  "namespace": "pipeline",
  "key": "step_1_result",
  "since_version": 0,
  "timeout_seconds": 30
}
```

**Changed (key updated within timeout):**
```json
{
  "status": "ok",
  "namespace": "pipeline",
  "key": "step_1_result",
  "value": {"score": 0.92},
  "version": 1,
  "updated_by": "agent-a",
  "updated_at": "2026-03-16T10:00:01.123456+00:00"
}
```

**Timeout (nothing changed):**
```json
{
  "status": "timeout",
  "namespace": "pipeline",
  "key": "step_1_result",
  "since_version": 0,
  "elapsed_seconds": 30.001,
  "hint": "The key did not change within the timeout. Retry with the same since_version, or call agenthold_get to check current state before deciding whether to wait again."
}
```

---

## `agenthold_clear_namespace`

Delete all state records in a namespace in a single atomic operation. A tombstone is written to `agenthold_history` for every key removed.

```json
{
  "namespace": "order-1234",
  "deleted_by": "cleanup-agent"
}
```

```json
{
  "status": "ok",
  "namespace": "order-1234",
  "deleted_count": 3,
  "deleted_keys": ["items", "status", "total"],
  "deleted_by": "cleanup-agent"
}
```

`deleted_keys` is sorted alphabetically. This operation has no conflict guard; it deletes unconditionally. If `deleted_keys` contains unexpected entries, use `agenthold_history` on those keys to investigate what was written and by whom.
