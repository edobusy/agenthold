"""Tests for the standard-mode (plug-and-play) server dispatch layer."""

import json

import pytest

from agenthold.coordinator import Coordinator
from agenthold.server import (
    COORDINATION_INSTRUCTIONS,
    _dispatch_standard,
    _standard_tools,
    _wait_standard,
    make_server,
)
from agenthold.store import StateStore

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def coordinator(store: StateStore) -> Coordinator:
    return Coordinator(store)


# ---------------------------------------------------------------------------
# make_server modes
# ---------------------------------------------------------------------------


def test_standard_mode_has_instructions() -> None:
    server = make_server(":memory:", tools_mode="standard")
    init_options = server.create_initialization_options()
    assert init_options.instructions is not None
    assert "agenthold_claim" in init_options.instructions


def test_advanced_mode_has_no_instructions() -> None:
    server = make_server(":memory:", tools_mode="advanced")
    init_options = server.create_initialization_options()
    assert init_options.instructions is None


def test_coordination_instructions_is_nonempty() -> None:
    assert len(COORDINATION_INSTRUCTIONS) > 0
    assert "agenthold_claim" in COORDINATION_INSTRUCTIONS


# ---------------------------------------------------------------------------
# Tool registration
# ---------------------------------------------------------------------------

EXPECTED_STANDARD_TOOLS = {
    "agenthold_claim",
    "agenthold_release",
    "agenthold_status",
    "agenthold_wait",
}

EXPECTED_ADVANCED_TOOLS = {
    "agenthold_get",
    "agenthold_set",
    "agenthold_list",
    "agenthold_history",
    "agenthold_delete",
    "agenthold_clear_namespace",
    "agenthold_export",
    "agenthold_watch",
}


def test_standard_mode_exposes_four_tools() -> None:
    tools = _standard_tools()
    tool_names = {t.name for t in tools}
    assert tool_names == EXPECTED_STANDARD_TOOLS


def test_standard_tools_and_advanced_tools_no_overlap() -> None:
    standard = {t.name for t in _standard_tools()}
    assert standard & EXPECTED_ADVANCED_TOOLS == set()


# ---------------------------------------------------------------------------
# Dispatch — claim
# ---------------------------------------------------------------------------


def test_dispatch_claim_unclaimed(coordinator: Coordinator) -> None:
    result = _dispatch_standard(
        coordinator, "agenthold_claim", {"resource": "intro.md", "agent": "a"}
    )
    assert result["status"] == "claimed"
    assert result["resource"] == "intro.md"
    assert result["version"] == 1


def test_dispatch_claim_busy(coordinator: Coordinator) -> None:
    _dispatch_standard(
        coordinator, "agenthold_claim", {"resource": "intro.md", "agent": "a"}
    )
    result = _dispatch_standard(
        coordinator, "agenthold_claim", {"resource": "intro.md", "agent": "b"}
    )
    assert result["status"] == "busy"
    assert "hint" in result


def test_dispatch_claim_already_claimed(coordinator: Coordinator) -> None:
    _dispatch_standard(
        coordinator, "agenthold_claim", {"resource": "intro.md", "agent": "a"}
    )
    result = _dispatch_standard(
        coordinator, "agenthold_claim", {"resource": "intro.md", "agent": "a"}
    )
    assert result["status"] == "already_claimed"


def test_dispatch_claim_empty_resource_returns_error(
    coordinator: Coordinator,
) -> None:
    result = _dispatch_standard(
        coordinator, "agenthold_claim", {"resource": "", "agent": "a"}
    )
    assert result["status"] == "error"
    assert "resource" in result["message"]


def test_dispatch_claim_output_is_json_serialisable(
    coordinator: Coordinator,
) -> None:
    result = _dispatch_standard(
        coordinator, "agenthold_claim", {"resource": "f.md", "agent": "a"}
    )
    text = json.dumps(result, indent=2)
    parsed = json.loads(text)
    assert parsed["status"] == "claimed"


# ---------------------------------------------------------------------------
# Dispatch — release
# ---------------------------------------------------------------------------


def test_dispatch_release_own_claim(coordinator: Coordinator) -> None:
    _dispatch_standard(
        coordinator, "agenthold_claim", {"resource": "f.md", "agent": "a"}
    )
    result = _dispatch_standard(
        coordinator, "agenthold_release", {"resource": "f.md", "agent": "a"}
    )
    assert result["status"] == "released"


def test_dispatch_release_other_agent(coordinator: Coordinator) -> None:
    _dispatch_standard(
        coordinator, "agenthold_claim", {"resource": "f.md", "agent": "a"}
    )
    result = _dispatch_standard(
        coordinator, "agenthold_release", {"resource": "f.md", "agent": "b"}
    )
    assert result["status"] == "error"


def test_dispatch_release_not_found(coordinator: Coordinator) -> None:
    result = _dispatch_standard(
        coordinator, "agenthold_release", {"resource": "f.md", "agent": "a"}
    )
    assert result["status"] == "not_found"


def test_dispatch_release_output_is_json_serialisable(
    coordinator: Coordinator,
) -> None:
    _dispatch_standard(
        coordinator, "agenthold_claim", {"resource": "f.md", "agent": "a"}
    )
    result = _dispatch_standard(
        coordinator, "agenthold_release", {"resource": "f.md", "agent": "a"}
    )
    json.dumps(result)  # must not raise


# ---------------------------------------------------------------------------
# Dispatch — status
# ---------------------------------------------------------------------------


def test_dispatch_status_available(coordinator: Coordinator) -> None:
    result = _dispatch_standard(coordinator, "agenthold_status", {"resource": "f.md"})
    assert result["status"] == "available"


def test_dispatch_status_claimed(coordinator: Coordinator) -> None:
    _dispatch_standard(
        coordinator, "agenthold_claim", {"resource": "f.md", "agent": "a"}
    )
    result = _dispatch_standard(coordinator, "agenthold_status", {"resource": "f.md"})
    assert result["status"] == "claimed"
    assert result["held_by"] == "a"


def test_dispatch_status_output_is_json_serialisable(
    coordinator: Coordinator,
) -> None:
    result = _dispatch_standard(coordinator, "agenthold_status", {"resource": "f.md"})
    json.dumps(result)


# ---------------------------------------------------------------------------
# Dispatch — wait
# ---------------------------------------------------------------------------


async def test_dispatch_wait_already_available(
    coordinator: Coordinator,
) -> None:
    result = await _wait_standard(coordinator, resource="f.md", timeout_seconds=0)
    assert result["status"] == "available"


async def test_dispatch_wait_fires_on_release(
    coordinator: Coordinator,
) -> None:
    """Claim, then release. wait should see it available immediately."""
    coordinator.claim("f.md", "a")
    coordinator.release("f.md", "a")
    result = await _wait_standard(coordinator, resource="f.md", timeout_seconds=1.0)
    assert result["status"] == "available"


async def test_dispatch_wait_timeout(coordinator: Coordinator) -> None:
    coordinator.claim("f.md", "a")
    result = await _wait_standard(coordinator, resource="f.md", timeout_seconds=0.3)
    assert result["status"] == "timeout"
    assert "held_by" in result
    assert "hint" in result
    assert result["elapsed_seconds"] >= 0.2


async def test_dispatch_wait_output_is_json_serialisable(
    coordinator: Coordinator,
) -> None:
    result = await _wait_standard(coordinator, resource="f.md", timeout_seconds=0)
    json.dumps(result)


async def test_dispatch_wait_negative_timeout(
    coordinator: Coordinator,
) -> None:
    result = await _wait_standard(coordinator, resource="f.md", timeout_seconds=-1)
    assert result["status"] == "error"
    assert "timeout_seconds" in result["message"]


async def test_dispatch_wait_empty_resource(
    coordinator: Coordinator,
) -> None:
    result = await _wait_standard(coordinator, resource="", timeout_seconds=0)
    assert result["status"] == "error"
    assert "resource" in result["message"]


async def test_dispatch_wait_dot_slash_only_returns_error(
    coordinator: Coordinator,
) -> None:
    """'./' normalizes to '' which must be rejected."""
    result = await _wait_standard(coordinator, resource="./", timeout_seconds=0)
    assert result["status"] == "error"
    assert "empty after normalization" in result["message"]


# ---------------------------------------------------------------------------
# Unknown tool
# ---------------------------------------------------------------------------


def test_dispatch_standard_unknown_tool(coordinator: Coordinator) -> None:
    result = _dispatch_standard(coordinator, "agenthold_foo", {"resource": "x"})
    assert result["status"] == "error"
    assert "Unknown tool" in result["message"]


# ---------------------------------------------------------------------------
# Tool drift guard
# ---------------------------------------------------------------------------


def test_all_standard_tools_are_handled() -> None:
    """Every standard tool must have a working handler."""
    store = StateStore(":memory:")
    coord = Coordinator(store)

    valid_calls: dict[str, dict[str, object]] = {
        "agenthold_claim": {"resource": "f.md", "agent": "a"},
        "agenthold_release": {"resource": "f.md", "agent": "a"},
        "agenthold_status": {"resource": "f.md"},
    }

    # Sync tools (wait is async, tested separately above)
    sync_tools = EXPECTED_STANDARD_TOOLS - {"agenthold_wait"}
    assert valid_calls.keys() == sync_tools

    for tool_name, args in valid_calls.items():
        result = _dispatch_standard(coord, tool_name, args)
        assert result.get("status") != "error", (
            f"Tool '{tool_name}' returned status='error': {result}"
        )
