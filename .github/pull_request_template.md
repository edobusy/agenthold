## What this PR does

<!-- Brief description -->

## Why

<!-- Motivation / issue number -->

## How to test

<!-- Steps or commands to verify -->

## Checklist

- [ ] `uv run ruff check src/ tests/` passes
- [ ] `uv run ruff format --check src/ tests/` passes
- [ ] `uv run mypy src/` passes
- [ ] `uv run pytest tests/ -v --tb=short` passes
- [ ] Coverage >= 80% (`uv run pytest tests/ --cov=agenthold --cov-report=term-missing --cov-fail-under=80`)
- [ ] New/changed behaviour is tested
