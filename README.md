# agenthold

> Shared versioned state for multi-agent AI workflows.
> An MCP server that gives your agents a consistent, conflict-safe ground truth.

[![CI](https://github.com/edobusy/agenthold/actions/workflows/ci.yml/badge.svg)](https://github.com/edobusy/agenthold/actions/workflows/ci.yml)
[![PyPI version](https://img.shields.io/pypi/v/agenthold)](https://pypi.org/project/agenthold/)
[![Python 3.11+](https://img.shields.io/pypi/pyversions/agenthold)](https://pypi.org/project/agenthold/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

---

## The problem

When two agents update the same value at the same time, the second write silently overwrites the first. No exception is raised. The value is wrong. The system keeps running.

![Without Agenthold - the silent overcommit problem](assets/budget_without.gif)

In the example above, two agents both read a `$10,000` budget and each allocate from it independently. The total committed reaches `$15,000`. The budget dict never complains.

This is not a race condition in the traditional sense. It is a **read-modify-write conflict**: each agent reads a value, does work, and writes back a result that assumes nothing changed in between. With multiple agents running concurrently, that assumption is always wrong.

---

## How it works

agenthold solves this with **optimistic concurrency control (OCC)**, the same mechanism Postgres uses in `UPDATE ... WHERE version = N` and DynamoDB uses in conditional writes.

Every value stored in agenthold has a version number. When an agent writes, it passes the version it read. If the stored version has changed since the read, the write is **rejected** with a `ConflictError` that includes the current value. The agent re-reads, recalculates, and retries.

![With Agenthold - conflict-safe allocation](assets/budget_with.gif)

The losing agent detects the conflict, re-reads the real remaining budget (`$2,000`), and adjusts its allocation. The total committed is always exactly `$10,000`. Every write is tracked.

OCC is the right fit for agent workflows because:
- Agents do work between reads and writes (network calls, LLM inference). You cannot hold a database lock across that work.
- Conflicts are rare. Retrying once is cheaper than acquiring a lock on every read.
- The retry logic is simple, explicit, and fully in the agent's control.

---

## Quick start

### 1. Install

```bash
pip install agenthold
```

### 2. Add to your MCP client config

```json
{
  "mcpServers": {
    "agenthold": {
      "command": "agenthold",
      "args": ["--db", "/path/to/state.db"]
    }
  }
}
```

### 3. Done

Agents automatically coordinate. No CLAUDE.md, no system prompt changes, no namespace design.

When an agent connects, it sees four self-documenting tools: `agenthold_claim`, `agenthold_release`, `agenthold_status`, and `agenthold_wait`. The tool descriptions tell the agent when and how to use each one. Server instructions reinforce the protocol when the MCP client includes them.

Works with Claude Desktop, Cursor, Continue, and any MCP-compatible client.

---

## Tools

agenthold exposes four coordination tools by default.

### `agenthold_claim`

Claim exclusive access to a resource before modifying it.

```json
{ "resource": "intro.md", "agent": "writer-1" }
```

**Claimed** — you hold exclusive access:
```json
{ "status": "claimed", "resource": "intro.md", "version": 1 }
```

**Busy** — another agent is working on this resource:
```json
{
  "status": "busy",
  "resource": "intro.md",
  "held_by": "writer-2",
  "claimed_at": "2026-03-17T10:00:00+00:00",
  "hint": "Another agent holds this resource. Work on a different resource, or call agenthold_wait to be notified when it becomes available."
}
```

**Already claimed** — you already hold this claim (idempotent):
```json
{ "status": "already_claimed", "resource": "intro.md", "version": 1 }
```

---

### `agenthold_release`

Release your claim after finishing edits. This immediately notifies any agents waiting via `agenthold_wait`.

```json
{ "resource": "intro.md", "agent": "writer-1" }
```

```json
{ "status": "released", "resource": "intro.md", "version": 2 }
```

---

### `agenthold_status`

Check whether a resource is available or currently claimed.

```json
{ "resource": "intro.md" }
```

**Available:**
```json
{ "status": "available", "resource": "intro.md" }
```

**Claimed:**
```json
{
  "status": "claimed",
  "resource": "intro.md",
  "held_by": "writer-2",
  "claimed_at": "2026-03-17T10:00:00+00:00",
  "version": 3
}
```

---

### `agenthold_wait`

Wait for a claimed resource to become available. Blocks the agent turn until the holder releases, or the timeout expires.

```json
{ "resource": "intro.md", "timeout_seconds": 30 }
```

**Available** — resource was released:
```json
{ "status": "available", "resource": "intro.md", "elapsed_seconds": 2.4 }
```

**Timeout:**
```json
{
  "status": "timeout",
  "resource": "intro.md",
  "held_by": "writer-2",
  "elapsed_seconds": 30.2,
  "hint": "The resource was not released within the timeout. Try working on a different resource, or call agenthold_wait again with a longer timeout."
}
```

---

## Advanced usage

For power users building custom coordination protocols, agenthold exposes eight low-level primitives via `--tools advanced`:

```bash
agenthold --db ./state.db --tools advanced
```

This gives agents direct access to `agenthold_get`, `agenthold_set`, `agenthold_list`, `agenthold_history`, `agenthold_delete`, `agenthold_clear_namespace`, `agenthold_export`, and `agenthold_watch`. No server instructions are sent in this mode.

### `agenthold_get`

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

### `agenthold_set`

Write a value. `expected_version` is required — pass the version from a prior `agenthold_get`, or `0` for a key that should not yet exist.

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
| `0` | Create-only guard — succeeds only if the key does not yet exist; conflicts if it does |
| `N` (from a prior `agenthold_get`) | Conflict-safe write — rejected if another agent wrote since your read |

**`force` parameter:** Set `force: true` to write unconditionally, bypassing conflict detection. When `force` is true, `expected_version` is ignored. Use this only for idempotent writes or initial seeding where overwriting is intentional.

---

### `agenthold_list`

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

### `agenthold_history`

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

Each entry includes an `event_type` field: `"write"` for normal writes, `"delete"` for deletion events. Delete tombstones have `value: null`. An empty history list means no writes have been recorded for this key — the key may not exist. Use `agenthold_get` to check current state.

---

### `agenthold_delete`

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

`expected_version` is required — pass the version from a prior `agenthold_get` to prevent accidentally deleting a record that was updated since your read. Set `force: true` to delete unconditionally (bypasses conflict detection; `expected_version` is ignored).

---

### `agenthold_export`

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

### `agenthold_watch`

Wait for a key's version to change, then return the new value. Polls every 200 ms.

> **Important:** This call holds the agent turn until it returns. No other actions can be taken while waiting. Only use this when the agent has nothing else to do until the key changes.

```json
{
  "namespace": "pipeline",
  "key": "step_1_result",
  "since_version": 0,
  "timeout_seconds": 30
}
```

**Changed — key updated within timeout:**
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

**Timeout — nothing changed:**
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

### `agenthold_clear_namespace`

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

`deleted_keys` is sorted alphabetically. This operation has no conflict guard — it deletes unconditionally. If `deleted_keys` contains unexpected entries, use `agenthold_history` on those keys to investigate what was written and by whom.

---

## Conflict detection

The read-modify-write pattern with `expected_version` is the core of agenthold. Here is the canonical retry loop:

```python
from agenthold.store import StateStore
from agenthold.exceptions import ConflictError

store = StateStore("./state.db")

record = store.get("campaign", "budget")   # read once before doing work
do_expensive_work()                         # LLM call, API request, etc.

while True:
    new_value = compute_new_value(record.value)
    try:
        store.set(
            "campaign", "budget", new_value,
            updated_by="my-agent",
            expected_version=record.version,
        )
        break   # write succeeded
    except ConflictError:
        record = store.get("campaign", "budget")   # re-read and retry
```

**Why this works:** The version number is the contract. If the stored version has advanced since your read, another agent wrote first. You take the current value, recalculate, and try again. The number of retries is bounded by the number of concurrent writers. In practice, agents almost never conflict more than once.

**Why not locks?** Locks require a lease mechanism (what happens if the agent crashes holding a lock?), add latency on every read, and interact badly with the long I/O waits inherent in agent workflows. OCC pays a cost only when there actually is a conflict.

---

## Use as a Python library

```python
from agenthold.store import StateStore
from agenthold.exceptions import ConflictError

store = StateStore("./state.db")

# Write a value (first write, no conflict check needed)
store.set("order-1234", "status", "received", updated_by="intake-agent")

# Read it back; always get the version number too
record = store.get("order-1234", "status")
print(record.value)    # "received"
print(record.version)  # 1

# Write with conflict detection; pass the version you read
try:
    store.set(
        "order-1234", "status", "processing",
        updated_by="fulfillment-agent",
        expected_version=record.version,  # rejected if another agent wrote first
    )
except ConflictError as e:
    # Another agent wrote between your read and write.
    # e.detail has the current version, value, and who wrote it.
    record = store.get("order-1234", "status")
    # ... recalculate and retry
```

---

## Examples

Two worked examples are included, each with a "before" and "after" script.

### Order processing

Two agents update the same order record concurrently: an inventory agent marks it reserved and sets the status, a pricing agent applies a discount.

```bash
# The problem: one agent silently overwrites the other
uv run python examples/order_processing/without_agenthold.py

# The solution: conflict detection + retry
uv run python examples/order_processing/with_agenthold.py
```

### Budget allocation

Two agents draw from a shared marketing budget. Without conflict detection, the budget is silently overcommitted. With agenthold, the losing agent re-reads the remaining balance and adjusts its allocation.

```bash
# The problem: $10,000 budget committed to $15,000 of spend
uv run python examples/budget_allocation/without_agenthold.py

# The solution: exact allocation, full audit trail
uv run python examples/budget_allocation/with_agenthold.py
```

---

## Configuration

```bash
agenthold --db ./state.db                   # standard mode (default)
agenthold --db ./state.db --tools advanced  # advanced mode
```

| Flag | Default | Description |
|---|---|---|
| `--db` | `./agenthold.db` | Path to the SQLite database file. Use `:memory:` for an in-process store (testing only; data is lost when the process exits). |
| `--tools` | `standard` | Tool set: `standard` (claim/release/status/wait) or `advanced` (get/set/delete/watch/list/history/clear/export). |

The database file is created automatically on first run. Back it up like any other SQLite file.

---

## Development

```bash
git clone https://github.com/edobusy/agenthold.git
cd agenthold
uv sync --all-extras --dev
```

**Run the tests:**
```bash
uv run pytest tests/ -v
```

**Check coverage:**
```bash
uv run pytest tests/ --cov=agenthold --cov-report=term-missing
```

**Lint and type-check:**
```bash
uv run ruff check src/ tests/
uv run ruff format src/ tests/
uv run mypy src/
```

CI runs on Python 3.11 and 3.12 on every push to `main`.

---

## Technical notes

These notes are here for engineers who want to understand the design decisions.

**Why SQLite?**
SQLite is the right tool for this scope. It is zero-dependency, ships in the Python stdlib, and runs everywhere. WAL mode is enabled so that read-only operations (exports, watches) do not block writers across processes. Write transactions use `BEGIN IMMEDIATE` to acquire the write lock upfront, ensuring OCC conflict detection works correctly even when multiple agenthold processes share the same database file. `busy_timeout` is set to 5 seconds so a second writer waits rather than failing immediately. Postgres adds an ops dependency with no benefit at this scale. The storage backend is behind a clean interface (`StateStore`) that can be swapped for Postgres when the need arises. Choosing a simple tool deliberately is not a limitation.

**Why OCC instead of pessimistic locking?**
Locks require the holder to release them, which means the system must handle crashes, timeouts, and stale holders. That complexity is not worth it when conflicts are rare. OCC pays a cost only when a conflict actually occurs: one extra read and one retry. For multi-agent workflows where agents do significant work between reads and writes (LLM inference, API calls, tool execution), OCC is the correct choice.

**What the versioning guarantees:**
Each key has a version that starts at 1 and increments by exactly 1 on every write. The `state_history` table is append-only and records every write before the live record is updated, so a crash between the two writes leaves history consistent. Deletions also write a tombstone entry to `state_history` (with `event_type: "delete"`) before removing the live record, so the full lifecycle of a key is visible in history. The ordering guarantee is per-key, not global; two different keys can have their versions updated in any order.

**What would change for production scale:**
Three things. First, replace SQLite with Postgres: better concurrent write throughput, replication, and managed hosting. The `StateStore` interface is already designed to make this a contained change. Second, add authentication: the current server trusts any caller on the stdio transport. A production deployment needs at minimum an API key check. Third, add the HTTP transport: the MCP SDK supports `StreamableHTTPServer`, which would let remote agents connect over the network instead of requiring a local process.

---

## License

MIT. See [LICENSE](LICENSE).
