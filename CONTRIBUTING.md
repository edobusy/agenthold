# Contributing to agenthold

Thank you for your interest in contributing.

---

## Development environment

You will need [uv](https://docs.astral.sh/uv/) to manage the Python environment.

```bash
git clone https://github.com/edobusy/agenthold.git
cd agenthold
uv sync --all-extras --dev
```

This creates a virtual environment in `.venv/` and installs all runtime and development dependencies.

---

## Running the tests

```bash
uv run pytest tests/ -v
```

To check coverage:

```bash
uv run pytest tests/ --cov=agenthold --cov-report=term-missing
```

The test suite must pass at 80%+ coverage before a PR can be merged. CI enforces this.

---

## Linting and formatting

agenthold uses [ruff](https://docs.astral.sh/ruff/) for linting and formatting, and [mypy](https://mypy.readthedocs.io/) in strict mode for type checking.

```bash
# Check and auto-fix lint issues
uv run ruff check --fix src/ tests/

# Format the code
uv run ruff format src/ tests/

# Type checking
uv run mypy src/
```

All three must pass with zero errors before a PR is reviewed. The CI workflow runs them on every push.

---

## PR process

1. Fork the repository and create a branch from `main`.
2. Make your change. Keep commits focused: one logical change per commit.
3. Follow the commit message convention: `feat:`, `fix:`, `test:`, `docs:`, `chore:`, `refactor:`.
4. Make sure tests pass, ruff is clean, and mypy has no errors.
5. Open a pull request against `main`. Describe what you changed and why.

There are no contributor licence agreements or special requirements. Small, well-scoped PRs are much easier to review than large ones.

---

## Project structure

```
src/agenthold/
    __init__.py      public API and version
    server.py        MCP server, argparse, tool registration, dispatch
    coordinator.py   claim lifecycle (register/claim/release/status), outcomes
    store.py         all SQLite operations (the core of the project)
    resources.py     workspace registry, resource canonicalization, ResourceId
    models.py        Pydantic models for records and errors
    exceptions.py    ConflictError, NotFoundError, BusyError

tests/
    conftest.py              shared fixtures (in-memory store, registry)
    test_store.py            unit tests for all store operations
    test_conflicts.py        concurrent write edge cases (OCC patterns)
    test_concurrency.py      multi-connection / multi-process safety
    test_resources.py        workspace registry and canonicalization
    test_coordinator.py      claim lifecycle, outcomes, TTL
    test_server.py           advanced-mode dispatch
    test_server_standard.py  standard-mode dispatch + workspace flag parsing
    test_watch.py            advanced-mode async polling
    test_release_script.py   pure-function tests for scripts/release.py

examples/
    order_processing/   two-agent order workflow demo
    budget_allocation/  two-agent budget allocation demo
```

The `StateStore` class in `store.py` is the place to start if you are adding a low-level state-store capability. For coordination semantics (claims, outcomes), edit `coordinator.py`. The MCP tool layer in `server.py` is intentionally thin: it validates inputs, calls the store or coordinator, and serialises the result.
