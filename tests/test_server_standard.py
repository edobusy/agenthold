"""Tests for the standard-mode (plug-and-play) server dispatch layer."""

import json
import os
from datetime import UTC, datetime
from unittest.mock import patch

import pytest

from agenthold.coordinator import Coordinator
from agenthold.exceptions import ConflictError
from agenthold.models import ConflictDetail
from agenthold.resources import Workspace, WorkspaceRegistry
from agenthold.server import (
    COORDINATION_INSTRUCTIONS,
    _build_workspaces,
    _dispatch_standard,
    _parse_workspace_arg,
    _standard_tools,
    _wait_standard,
    coordination_instructions,
    make_server,
)
from agenthold.store import StateStore

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def coordinator(store: StateStore, registry: WorkspaceRegistry) -> Coordinator:
    return Coordinator(store, registry)


def _register(coordinator: Coordinator) -> str:
    """Helper: register an agent and return its agent_id."""
    result = _dispatch_standard(
        coordinator,
        "agenthold_register",
        {"name": "test-agent"},
    )
    return result["agent_id"]


# ---------------------------------------------------------------------------
# make_server modes
# ---------------------------------------------------------------------------


def test_standard_mode_has_instructions() -> None:
    server = make_server(":memory:", tools_mode="standard")
    init_options = server.create_initialization_options()
    assert init_options.instructions is not None
    assert "agenthold_claim" in init_options.instructions


def test_standard_mode_instructions_include_workspaces() -> None:
    server = make_server(
        ":memory:",
        workspaces=[Workspace(name="myproj", root="/abs/path")],
        tools_mode="standard",
    )
    init_options = server.create_initialization_options()
    assert "myproj" in init_options.instructions
    assert "/abs/path" in init_options.instructions


def test_advanced_mode_has_no_instructions() -> None:
    server = make_server(":memory:", tools_mode="advanced")
    init_options = server.create_initialization_options()
    assert init_options.instructions is None


def test_make_server_accepts_claim_ttl() -> None:
    server = make_server(":memory:", claim_ttl=60.0)
    assert server is not None


def test_make_server_default_workspace_is_cwd() -> None:
    """With no workspaces, defaults to a single 'default' at CWD."""
    server = make_server(":memory:")
    init_options = server.create_initialization_options()
    assert "default" in init_options.instructions
    # CWD should appear somewhere in the rendered instructions
    assert os.getcwd().replace("\\", "/") in init_options.instructions.replace(
        "\\", "/"
    )


def test_coordination_instructions_constant_nonempty() -> None:
    assert len(COORDINATION_INSTRUCTIONS) > 0
    assert "agenthold_claim" in COORDINATION_INSTRUCTIONS
    assert "agenthold_register" in COORDINATION_INSTRUCTIONS


def test_coordination_instructions_with_registry() -> None:
    registry = WorkspaceRegistry(
        [
            Workspace(name="default", root="/work"),
            Workspace(name="other", root="/elsewhere"),
        ]
    )
    text = coordination_instructions(registry)
    assert "default" in text
    assert "other" in text
    assert "/work" in text
    assert "/elsewhere" in text


# ---------------------------------------------------------------------------
# Tool registration
# ---------------------------------------------------------------------------

EXPECTED_STANDARD_TOOLS = {
    "agenthold_register",
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


def test_standard_mode_exposes_five_tools() -> None:
    tools = _standard_tools()
    tool_names = {t.name for t in tools}
    assert tool_names == EXPECTED_STANDARD_TOOLS


def test_standard_tools_and_advanced_tools_no_overlap() -> None:
    standard = {t.name for t in _standard_tools()}
    assert standard & EXPECTED_ADVANCED_TOOLS == set()


def test_release_tool_schema_includes_outcome_and_moved_to() -> None:
    tools = {t.name: t for t in _standard_tools()}
    release_tool = tools["agenthold_release"]
    schema = release_tool.inputSchema
    assert "outcome" in schema["properties"]
    assert "moved_to" in schema["properties"]
    # outcome lists the agent-allowed enum values
    assert set(schema["properties"]["outcome"]["enum"]) == {
        "released",
        "modified",
        "created",
        "deleted",
        "moved",
    }


# ---------------------------------------------------------------------------
# Dispatch — register
# ---------------------------------------------------------------------------


def test_dispatch_register(coordinator: Coordinator) -> None:
    result = _dispatch_standard(
        coordinator, "agenthold_register", {"name": "test-agent"}
    )
    assert result["status"] == "registered"
    assert result["agent_id"].startswith("agent-")


def test_dispatch_register_with_model(coordinator: Coordinator) -> None:
    result = _dispatch_standard(
        coordinator,
        "agenthold_register",
        {"name": "test-agent", "model": "claude-sonnet-4-6"},
    )
    assert result["status"] == "registered"


def test_dispatch_register_empty_name(coordinator: Coordinator) -> None:
    result = _dispatch_standard(coordinator, "agenthold_register", {"name": ""})
    assert result["status"] == "error"
    assert "name" in result["message"]


def test_dispatch_register_json_serialisable(
    coordinator: Coordinator,
) -> None:
    result = _dispatch_standard(
        coordinator, "agenthold_register", {"name": "test-agent"}
    )
    json.dumps(result)


# ---------------------------------------------------------------------------
# Dispatch — claim
# ---------------------------------------------------------------------------


def test_dispatch_claim_unclaimed(coordinator: Coordinator) -> None:
    agent_id = _register(coordinator)
    result = _dispatch_standard(
        coordinator,
        "agenthold_claim",
        {"resource": "intro.md", "agent_id": agent_id},
    )
    assert result["status"] == "claimed"
    assert result["resource"] == "file://default/intro.md"


def test_dispatch_claim_busy(coordinator: Coordinator) -> None:
    a = _register(coordinator)
    b = _register(coordinator)
    _dispatch_standard(
        coordinator,
        "agenthold_claim",
        {"resource": "intro.md", "agent_id": a},
    )
    result = _dispatch_standard(
        coordinator,
        "agenthold_claim",
        {"resource": "intro.md", "agent_id": b},
    )
    assert result["status"] == "busy"
    assert "hint" in result


def test_dispatch_claim_already_claimed(
    coordinator: Coordinator,
) -> None:
    agent_id = _register(coordinator)
    _dispatch_standard(
        coordinator,
        "agenthold_claim",
        {"resource": "intro.md", "agent_id": agent_id},
    )
    result = _dispatch_standard(
        coordinator,
        "agenthold_claim",
        {"resource": "intro.md", "agent_id": agent_id},
    )
    assert result["status"] == "already_claimed"


def test_dispatch_claim_empty_resource_returns_error(
    coordinator: Coordinator,
) -> None:
    agent_id = _register(coordinator)
    result = _dispatch_standard(
        coordinator,
        "agenthold_claim",
        {"resource": "", "agent_id": agent_id},
    )
    assert result["status"] == "error"
    assert "empty" in result["message"]


def test_dispatch_claim_dot_dot_returns_error(
    coordinator: Coordinator,
) -> None:
    """Path traversal must be rejected by the dispatcher."""
    agent_id = _register(coordinator)
    result = _dispatch_standard(
        coordinator,
        "agenthold_claim",
        {"resource": "../etc/passwd", "agent_id": agent_id},
    )
    assert result["status"] == "error"
    assert ".." in result["message"]


def test_dispatch_claim_unregistered_returns_error(
    coordinator: Coordinator,
) -> None:
    result = _dispatch_standard(
        coordinator,
        "agenthold_claim",
        {"resource": "f.md", "agent_id": "agent-unknown"},
    )
    assert result["status"] == "error"
    assert "agenthold_register" in result["message"]


def test_dispatch_claim_output_is_json_serialisable(
    coordinator: Coordinator,
) -> None:
    agent_id = _register(coordinator)
    result = _dispatch_standard(
        coordinator,
        "agenthold_claim",
        {"resource": "f.md", "agent_id": agent_id},
    )
    text = json.dumps(result, indent=2)
    parsed = json.loads(text)
    assert parsed["status"] == "claimed"


def test_dispatch_claim_uri_form(coordinator: Coordinator) -> None:
    agent_id = _register(coordinator)
    result = _dispatch_standard(
        coordinator,
        "agenthold_claim",
        {"resource": "file://default/x.py", "agent_id": agent_id},
    )
    assert result["status"] == "claimed"


def test_dispatch_claim_custom_uri(coordinator: Coordinator) -> None:
    agent_id = _register(coordinator)
    result = _dispatch_standard(
        coordinator,
        "agenthold_claim",
        {"resource": "custom://task-42", "agent_id": agent_id},
    )
    assert result["status"] == "claimed"
    assert result["resource"] == "custom://task-42"


# ---------------------------------------------------------------------------
# Dispatch — release
# ---------------------------------------------------------------------------


def test_dispatch_release_default_outcome(coordinator: Coordinator) -> None:
    agent_id = _register(coordinator)
    _dispatch_standard(
        coordinator,
        "agenthold_claim",
        {"resource": "f.md", "agent_id": agent_id},
    )
    result = _dispatch_standard(
        coordinator,
        "agenthold_release",
        {"resource": "f.md", "agent_id": agent_id},
    )
    assert result["status"] == "released"
    assert result["outcome"] == "released"


def test_dispatch_release_with_outcome(coordinator: Coordinator) -> None:
    agent_id = _register(coordinator)
    _dispatch_standard(
        coordinator,
        "agenthold_claim",
        {"resource": "temp.txt", "agent_id": agent_id},
    )
    result = _dispatch_standard(
        coordinator,
        "agenthold_release",
        {
            "resource": "temp.txt",
            "agent_id": agent_id,
            "outcome": "deleted",
        },
    )
    assert result["outcome"] == "deleted"


def test_dispatch_release_with_moved(coordinator: Coordinator) -> None:
    agent_id = _register(coordinator)
    _dispatch_standard(
        coordinator,
        "agenthold_claim",
        {"resource": "old.py", "agent_id": agent_id},
    )
    result = _dispatch_standard(
        coordinator,
        "agenthold_release",
        {
            "resource": "old.py",
            "agent_id": agent_id,
            "outcome": "moved",
            "moved_to": "new.py",
        },
    )
    assert result["outcome"] == "moved"
    assert result["moved_to"] == "file://default/new.py"


def test_dispatch_release_invalid_outcome_returns_error(
    coordinator: Coordinator,
) -> None:
    agent_id = _register(coordinator)
    _dispatch_standard(
        coordinator,
        "agenthold_claim",
        {"resource": "f.md", "agent_id": agent_id},
    )
    result = _dispatch_standard(
        coordinator,
        "agenthold_release",
        {
            "resource": "f.md",
            "agent_id": agent_id,
            "outcome": "bogus",
        },
    )
    assert result["status"] == "error"
    assert "Invalid outcome" in result["message"]


def test_dispatch_release_moved_without_target_returns_error(
    coordinator: Coordinator,
) -> None:
    agent_id = _register(coordinator)
    _dispatch_standard(
        coordinator,
        "agenthold_claim",
        {"resource": "old.py", "agent_id": agent_id},
    )
    result = _dispatch_standard(
        coordinator,
        "agenthold_release",
        {
            "resource": "old.py",
            "agent_id": agent_id,
            "outcome": "moved",
        },
    )
    assert result["status"] == "error"
    assert "moved_to" in result["message"]


def test_dispatch_release_other_agent(coordinator: Coordinator) -> None:
    a = _register(coordinator)
    b = _register(coordinator)
    _dispatch_standard(
        coordinator,
        "agenthold_claim",
        {"resource": "f.md", "agent_id": a},
    )
    result = _dispatch_standard(
        coordinator,
        "agenthold_release",
        {"resource": "f.md", "agent_id": b},
    )
    assert result["status"] == "error"


def test_dispatch_release_not_found(coordinator: Coordinator) -> None:
    agent_id = _register(coordinator)
    result = _dispatch_standard(
        coordinator,
        "agenthold_release",
        {"resource": "f.md", "agent_id": agent_id},
    )
    assert result["status"] == "not_found"


def test_dispatch_release_unregistered_returns_error(
    coordinator: Coordinator,
) -> None:
    result = _dispatch_standard(
        coordinator,
        "agenthold_release",
        {"resource": "f.md", "agent_id": "agent-unknown"},
    )
    assert result["status"] == "error"
    assert "agenthold_register" in result["message"]


def test_dispatch_release_output_is_json_serialisable(
    coordinator: Coordinator,
) -> None:
    agent_id = _register(coordinator)
    _dispatch_standard(
        coordinator,
        "agenthold_claim",
        {"resource": "f.md", "agent_id": agent_id},
    )
    result = _dispatch_standard(
        coordinator,
        "agenthold_release",
        {
            "resource": "f.md",
            "agent_id": agent_id,
            "outcome": "moved",
            "moved_to": "g.md",
        },
    )
    json.dumps(result)


# ---------------------------------------------------------------------------
# Dispatch — status
# ---------------------------------------------------------------------------


def test_dispatch_status_available(coordinator: Coordinator) -> None:
    result = _dispatch_standard(coordinator, "agenthold_status", {"resource": "f.md"})
    assert result["status"] == "available"


def test_dispatch_status_claimed(coordinator: Coordinator) -> None:
    agent_id = _register(coordinator)
    _dispatch_standard(
        coordinator, "agenthold_claim", {"resource": "f.md", "agent_id": agent_id}
    )
    result = _dispatch_standard(coordinator, "agenthold_status", {"resource": "f.md"})
    assert result["status"] == "claimed"
    assert result["held_by"] == agent_id


def test_dispatch_status_after_deleted_outcome(
    coordinator: Coordinator,
) -> None:
    agent_id = _register(coordinator)
    _dispatch_standard(
        coordinator,
        "agenthold_claim",
        {"resource": "temp.txt", "agent_id": agent_id},
    )
    _dispatch_standard(
        coordinator,
        "agenthold_release",
        {
            "resource": "temp.txt",
            "agent_id": agent_id,
            "outcome": "deleted",
        },
    )
    result = _dispatch_standard(
        coordinator, "agenthold_status", {"resource": "temp.txt"}
    )
    assert result["status"] == "available"
    assert result["previous_outcome"] == "deleted"
    assert "hint" in result


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


async def test_dispatch_wait_fires_on_release_with_outcome(
    coordinator: Coordinator,
) -> None:
    coordinator.claim("f.md", "a")
    coordinator.release("f.md", "a", outcome="modified")
    result = await _wait_standard(coordinator, resource="f.md", timeout_seconds=1.0)
    assert result["status"] == "available"
    assert result["previous_outcome"] == "modified"


async def test_dispatch_wait_fires_on_release_with_moved(
    coordinator: Coordinator,
) -> None:
    coordinator.claim("old.py", "a")
    coordinator.release("old.py", "a", outcome="moved", moved_to="new.py")
    result = await _wait_standard(coordinator, resource="old.py", timeout_seconds=1.0)
    assert result["status"] == "available"
    assert result["previous_outcome"] == "moved"
    assert result["moved_to"] == "file://default/new.py"
    assert "hint" in result


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


async def test_dispatch_wait_dot_slash_only_returns_error(
    coordinator: Coordinator,
) -> None:
    result = await _wait_standard(coordinator, resource="./", timeout_seconds=0)
    assert result["status"] == "error"


# ---------------------------------------------------------------------------
# ConflictError safety net
# ---------------------------------------------------------------------------


def test_dispatch_standard_catches_conflict_error(
    coordinator: Coordinator,
) -> None:
    detail = ConflictDetail(
        namespace="_agents",
        key="agent-deadbeef",
        expected_version=0,
        actual_version=1,
        actual_value={"name": "existing"},
        updated_by="agent-deadbeef",
        updated_at=datetime.now(UTC),
    )
    with patch.object(
        coordinator,
        "register",
        side_effect=ConflictError(detail),
    ):
        result = _dispatch_standard(coordinator, "agenthold_register", {"name": "test"})
    assert result["status"] == "error"
    assert "conflict" in result["message"].lower()
    assert "hint" in result
    json.dumps(result)


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


def test_all_standard_tools_are_handled(
    registry: WorkspaceRegistry,
) -> None:
    """Every standard tool must have a working handler."""
    store = StateStore(":memory:")
    coord = Coordinator(store, registry)

    reg = _dispatch_standard(coord, "agenthold_register", {"name": "drift-test"})
    agent_id = reg["agent_id"]

    valid_calls: dict[str, dict[str, object]] = {
        "agenthold_register": {"name": "drift-test-2"},
        "agenthold_claim": {"resource": "f.md", "agent_id": agent_id},
        "agenthold_release": {
            "resource": "f.md",
            "agent_id": agent_id,
        },
        "agenthold_status": {"resource": "f.md"},
    }

    sync_tools = EXPECTED_STANDARD_TOOLS - {"agenthold_wait"}
    assert valid_calls.keys() == sync_tools

    for tool_name, args in valid_calls.items():
        result = _dispatch_standard(coord, tool_name, args)
        assert result.get("status") != "error", (
            f"Tool '{tool_name}' returned status='error': {result}"
        )


# ---------------------------------------------------------------------------
# Workspace argument parsing
# ---------------------------------------------------------------------------


class TestWorkspaceArgParsing:
    def test_name_equals_path(self) -> None:
        ws = _parse_workspace_arg("myproj=/abs/path")
        assert ws.name == "myproj"
        assert ws.root == "/abs/path"

    def test_path_only_derives_name(self) -> None:
        ws = _parse_workspace_arg("/home/user/myproj")
        assert ws.name == "myproj"
        assert ws.root == "/home/user/myproj"

    def test_path_only_with_trailing_slash(self) -> None:
        ws = _parse_workspace_arg("/home/user/myproj/")
        assert ws.name == "myproj"

    def test_windows_path_only(self) -> None:
        ws = _parse_workspace_arg("C:\\projects\\myproj")
        assert ws.name == "myproj"

    def test_relative_rejected(self) -> None:
        with pytest.raises(ValueError, match="absolute"):
            _parse_workspace_arg("relpath")

    def test_empty_name_rejected(self) -> None:
        with pytest.raises(ValueError, match="name=path"):
            _parse_workspace_arg("=/abs")

    def test_empty_path_rejected(self) -> None:
        with pytest.raises(ValueError, match="name=path"):
            _parse_workspace_arg("name=")


class TestBuildWorkspaces:
    def test_no_args_creates_default(self) -> None:
        workspaces = _build_workspaces(None)
        assert len(workspaces) == 1
        assert workspaces[0].name == "default"

    def test_no_args_empty_list_creates_default(self) -> None:
        workspaces = _build_workspaces([])
        assert len(workspaces) == 1
        assert workspaces[0].name == "default"

    def test_multiple_args(self) -> None:
        workspaces = _build_workspaces(["a=/x", "b=/y"])
        names = [w.name for w in workspaces]
        assert names == ["a", "b"]
