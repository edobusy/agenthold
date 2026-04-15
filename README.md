# agenthold

**Stop your AI agents from silently overwriting each other.**

When two agents update the same value, the second write quietly destroys the first. No error, no exception, just wrong data and a system that keeps running. agenthold is an MCP server that gives agents shared, versioned state with conflict detection built in. Think of it as `git` for your agents' working memory.

[![CI](https://github.com/edobusy/agenthold/actions/workflows/ci.yml/badge.svg)](https://github.com/edobusy/agenthold/actions/workflows/ci.yml)
[![PyPI version](https://img.shields.io/pypi/v/agenthold)](https://pypi.org/project/agenthold/)
[![PyPI Downloads](https://static.pepy.tech/personalized-badge/agenthold?period=total&units=INTERNATIONAL_SYSTEM&left_color=BLACK&right_color=GREEN&left_text=downloads)](https://pepy.tech/projects/agenthold)
[![Coverage 80%+](https://img.shields.io/badge/coverage-80%25%2B-brightgreen)](https://github.com/edobusy/agenthold/actions/workflows/ci.yml)
[![Python 3.11+](https://img.shields.io/pypi/pyversions/agenthold)](https://pypi.org/project/agenthold/)
[![Ruff](https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/astral-sh/ruff/main/assets/badge/v2.json)](https://github.com/astral-sh/ruff)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

---

## The problem

When two agents update the same value at the same time, the second write silently overwrites the first. No exception is raised. The value is wrong. The system keeps running.

![Without agenthold: the silent overcommit problem](assets/budget_without.gif)

Two agents read a `$10,000` budget and allocate from it independently. Total committed: `$15,000`. The budget object never complains. This is a **read-modify-write conflict**: each agent's write assumes nothing changed since its read.

---

## How it works

agenthold solves this with **optimistic concurrency control (OCC)**, the same mechanism Postgres uses in `UPDATE ... WHERE version = N` and DynamoDB uses in conditional writes.

Every value stored in agenthold has a version number. When an agent writes, it passes the version it read. If the stored version has changed since the read, the write is **rejected** with a `ConflictError` that includes the current value. The agent re-reads, recalculates, and retries.

![With agenthold: conflict-safe allocation](assets/budget_with.gif)

The losing agent detects the conflict, re-reads the real remaining budget (`$2,000`), and adjusts its allocation. The total committed is always exactly `$10,000`. Every write is tracked.

OCC is the right fit for agent workflows because:
- Agents do work between reads and writes (network calls, LLM inference). You cannot hold a database lock across that work.
- Conflicts are rare. Retrying once is cheaper than acquiring a lock on every read.
- The retry logic is simple, explicit, and fully in the agent's control.

---

## Works with any agent framework

agenthold connects via [MCP (Model Context Protocol)](https://modelcontextprotocol.io), the open standard for tool integration. Any framework that speaks MCP can use agenthold with zero glue code.

| Framework | How to connect |
|---|---|
| **Claude Desktop / Claude Code** | Built-in: add to `mcpServers` config |
| **Cursor / Continue / Windsurf** | Built-in: add to MCP config |
| **LangChain / LangGraph** | [`langchain-mcp-adapters`](https://github.com/langchain-ai/langchain-mcp-adapters) |
| **CrewAI** | Native `mcps` field on Agent |
| **OpenAI Agents SDK** | Built-in `mcp_servers` param |
| **Google ADK** | Built-in MCP Toolbox |
| **AutoGen** | [`autogen_ext.tools.mcp`](https://microsoft.github.io/autogen/stable//user-guide/core-user-guide/components/workbench.html) |
| **PydanticAI** | Native MCP integration |

agenthold is not a framework. It is **shared infrastructure** that sits underneath your orchestration layer, the same way a database sits underneath your application. Your agents keep their existing tools and logic; agenthold adds the coordination primitive they are missing.

> **Not using MCP yet?** agenthold also works as a [Python library](#use-as-a-python-library) you can call directly from any framework. Import `StateStore`, call `.get()` and `.set()` with version checks, and you have conflict-safe shared state.

---

## Architecture

```mermaid
graph LR
    A1["Agent 1
    LangChain, CrewAI, etc."] -->|MCP| S["agenthold
    MCP Server"]
    A2["Agent 2
    Claude, OpenAI, etc."] -->|MCP| S
    A3["Agent 3
    AutoGen, ADK, etc."] -->|MCP| S
    S --> DB[("SQLite
    WAL mode")]
    DB -->|version 3| S
    S -->|"conflict! retry"| A2
```

Every write carries a version number. If the stored version has changed since an agent's read, the write is rejected and the agent retries with current data. This is the same mechanism used by Postgres conditional updates and DynamoDB conditional writes.

---

## Quick start

### 1. Install

```bash
pip install agenthold
# or
uv pip install agenthold
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

When an agent connects, it sees five self-documenting tools: `agenthold_register`, `agenthold_claim`, `agenthold_release`, `agenthold_status`, and `agenthold_wait`. The tool descriptions tell the agent when and how to use each one. Server instructions reinforce the protocol when the MCP client includes them.

---

## Tools

agenthold exposes five coordination tools by default.

### `agenthold_register`

Register yourself and receive a unique agent ID. Must be called once before using `agenthold_claim` or `agenthold_release`.

```json
{ "name": "editor-agent", "model": "claude-sonnet-4-6" }
```

```json
{
  "status": "registered",
  "agent_id": "agent-a1b2c3d4",
  "name": "editor-agent",
  "registered_at": "2026-03-18T10:00:00+00:00"
}
```

---

### `agenthold_claim`

Claim exclusive access to a resource before modifying it. Requires a registered `agent_id`.

```json
{ "resource": "intro.md", "agent_id": "agent-a1b2c3d4" }
```

**Claimed** (you hold exclusive access):
```json
{ "status": "claimed", "resource": "intro.md", "version": 1 }
```

**Busy** (another agent is working on this resource):
```json
{
  "status": "busy",
  "resource": "intro.md",
  "held_by": "agent-e5f6g7h8",
  "claimed_at": "2026-03-18T10:00:00+00:00",
  "hint": "Another agent holds this resource. Work on a different resource, or call agenthold_wait to be notified when it becomes available."
}
```

**Already claimed** (you already hold this claim, idempotent):
```json
{ "status": "already_claimed", "resource": "intro.md", "version": 1 }
```

---

### `agenthold_release`

Release your claim after finishing edits. This immediately notifies any agents waiting via `agenthold_wait`. Requires a registered `agent_id`.

```json
{ "resource": "intro.md", "agent_id": "agent-a1b2c3d4" }
```

```json
{ "status": "released", "resource": "intro.md", "version": 2 }
```

---

### `agenthold_status`

Check whether a resource is available or currently claimed. Does not require registration.

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
  "held_by": "agent-e5f6g7h8",
  "agent_name": "editor-agent",
  "agent_model": "claude-sonnet-4-6",
  "claimed_at": "2026-03-18T10:00:00+00:00",
  "version": 3
}
```

---

### `agenthold_wait`

Wait for a claimed resource to become available. Blocks the agent turn until the holder releases, or the timeout expires.

```json
{ "resource": "intro.md", "timeout_seconds": 30 }
```

**Available** (resource was released):
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

## Advanced tools

For custom coordination protocols, agenthold exposes eight low-level primitives via `--tools advanced`:

`agenthold_get` · `agenthold_set` · `agenthold_list` · `agenthold_history` · `agenthold_delete` · `agenthold_watch` · `agenthold_clear_namespace` · `agenthold_export`

These give agents direct read/write/watch access to the versioned state store with full OCC conflict detection. No server instructions are sent in this mode.

**[See the full advanced tools reference →](docs/advanced-tools.md)**

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

## What it looks like in practice

In a multi-agent session, the coordination is automatic. An agent's tool calls look like this:

```
Agent A: agenthold_register(name="writer", model="claude-sonnet-4-6")
         → agent_id: "agent-a1b2c3d4"

Agent A: agenthold_claim(resource="chapter-3.md", agent_id="agent-a1b2c3d4")
         → status: "claimed"

Agent B: agenthold_claim(resource="chapter-3.md", agent_id="agent-e5f6g7h8")
         → status: "busy", hint: "Work on a different resource..."

Agent A: agenthold_release(resource="chapter-3.md", agent_id="agent-a1b2c3d4")
         → status: "released"
```

No system prompt engineering. The tool descriptions guide the agents.

### Worked examples

Two worked examples are included, each with a "before" and "after" script.

**Order processing**: two agents update the same order record concurrently:

```bash
uv run python examples/order_processing/without_agenthold.py   # silent overwrite
uv run python examples/order_processing/with_agenthold.py      # conflict detection + retry
```

**Budget allocation**: two agents draw from a shared marketing budget:

```bash
uv run python examples/budget_allocation/without_agenthold.py  # $10k budget → $15k committed
uv run python examples/budget_allocation/with_agenthold.py     # exact allocation, full audit trail
```

---

## Configuration

```bash
agenthold --db ./state.db                      # standard mode (default)
agenthold --db ./state.db --tools advanced     # advanced mode
agenthold --db ./state.db --claim-ttl 1800     # standard + 30 min TTL
```

| Flag | Default | Description |
|---|---|---|
| `--db` | `./agenthold.db` | Path to the SQLite database file. Use `:memory:` for an in-process store (testing only; data is lost when the process exits). |
| `--tools` | `standard` | Tool set: `standard` (register/claim/release/status/wait) or `advanced` (get/set/delete/watch/list/history/clear/export). |
| `--claim-ttl` | None (no expiry) | Seconds before an inactive agent's claims expire. Only applies in standard mode. When set, claims held by agents whose last activity exceeds this value are treated as expired and can be taken by other agents. |

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

CI runs on Python 3.11 and 3.12 on every push to `main`. See [CONTRIBUTING.md](CONTRIBUTING.md) for detailed guidelines.

---

<details>
<summary><strong>Technical notes</strong> (design decisions for engineers)</summary>

**Why SQLite?**
SQLite is the right tool for this scope. It is zero-dependency, ships in the Python stdlib, and runs everywhere. WAL mode is enabled so that read-only operations (exports, watches) do not block writers across processes. Write transactions use `BEGIN IMMEDIATE` to acquire the write lock upfront, ensuring OCC conflict detection works correctly even when multiple agenthold processes share the same database file. `busy_timeout` is set to 5 seconds so a second writer waits rather than failing immediately. Postgres adds an ops dependency with no benefit at this scale. The storage backend is behind a clean interface (`StateStore`) that can be swapped for Postgres when the need arises. Choosing a simple tool deliberately is not a limitation.

**Why OCC instead of pessimistic locking?**
Locks require the holder to release them, which means the system must handle crashes, timeouts, and stale holders. That complexity is not worth it when conflicts are rare. OCC pays a cost only when a conflict actually occurs: one extra read and one retry. For multi-agent workflows where agents do significant work between reads and writes (LLM inference, API calls, tool execution), OCC is the correct choice.

**What the versioning guarantees:**
Each key has a version that starts at 1 and increments by exactly 1 on every write. The `state_history` table is append-only and records every write before the live record is updated, so a crash between the two writes leaves history consistent. Deletions also write a tombstone entry to `state_history` (with `event_type: "delete"`) before removing the live record, so the full lifecycle of a key is visible in history. The ordering guarantee is per-key, not global; two different keys can have their versions updated in any order.

**What would change for production scale:**
Three things. First, replace SQLite with Postgres: better concurrent write throughput, replication, and managed hosting. The `StateStore` interface is already designed to make this a contained change. Second, add authentication: the current server trusts any caller on the stdio transport. A production deployment needs at minimum an API key check. Third, add the HTTP transport: the MCP SDK supports `StreamableHTTPServer`, which would let remote agents connect over the network instead of requiring a local process.

</details>

---

## License

MIT. See [LICENSE](LICENSE).

---

mcp-name: io.github.edobusy/agenthold

