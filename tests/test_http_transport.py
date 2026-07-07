"""Tests for the HTTP (Streamable HTTP) transport.

Two levels:
  * Pure/unit tests — arg parsing, security-settings helper, app construction,
    and the lifespan cleanup hook. No sockets.
  * Integration tests — a real uvicorn server on an ephemeral localhost port,
    driven by the MCP Streamable HTTP client end to end.
"""

from __future__ import annotations

import asyncio
import json
import socket
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import pytest
import uvicorn
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from mcp.server.transport_security import TransportSecuritySettings
from starlette.applications import Starlette

from agenthold import server as srv
from agenthold.coordinator import Coordinator
from agenthold.resources import Workspace, WorkspaceRegistry
from agenthold.store import StateStore

STANDARD_TOOLS = {
    "agenthold_register",
    "agenthold_claim",
    "agenthold_release",
    "agenthold_status",
    "agenthold_wait",
}


@pytest.fixture
def registry() -> WorkspaceRegistry:
    return WorkspaceRegistry([Workspace(name="default", root="/work")])


def _coordinator(registry: WorkspaceRegistry) -> tuple[StateStore, Coordinator]:
    store = StateStore(":memory:")
    return store, Coordinator(store, registry)


def _payload(result: object) -> dict:
    """Extract the JSON dict a tool returned from a CallToolResult."""
    return json.loads(result.content[0].text)  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# Arg parsing
# ---------------------------------------------------------------------------


def test_transport_defaults_to_stdio() -> None:
    args = srv._build_arg_parser().parse_args([])
    assert args.transport == "stdio"
    assert args.host == "127.0.0.1"
    assert args.port == 8417
    assert args.path == "/mcp"
    assert args.json_response is False
    assert args.allowed_host is None


def test_transport_http_parsing() -> None:
    args = srv._build_arg_parser().parse_args(["--transport", "http"])
    assert args.transport == "http"


def test_http_custom_flags_parsed() -> None:
    args = srv._build_arg_parser().parse_args(
        [
            "--transport",
            "http",
            "--host",
            "0.0.0.0",
            "--port",
            "9000",
            "--path",
            "/rpc",
            "--json-response",
            "--allowed-host",
            "a.example",
            "--allowed-host",
            "b.example",
        ]
    )
    assert args.host == "0.0.0.0"
    assert args.port == 9000
    assert args.path == "/rpc"
    assert args.json_response is True
    assert args.allowed_host == ["a.example", "b.example"]


def test_invalid_transport_rejected() -> None:
    with pytest.raises(SystemExit):
        srv._build_arg_parser().parse_args(["--transport", "carrier-pigeon"])


# ---------------------------------------------------------------------------
# Security-settings helper
# ---------------------------------------------------------------------------


def test_security_settings_none_when_no_hosts() -> None:
    assert srv._security_settings_from_allowed_hosts(None) is None
    assert srv._security_settings_from_allowed_hosts([]) is None


def test_security_settings_enabled_with_hosts() -> None:
    settings = srv._security_settings_from_allowed_hosts(["example.com", "x:*"])
    assert isinstance(settings, TransportSecuritySettings)
    assert settings.enable_dns_rebinding_protection is True
    assert settings.allowed_hosts == ["example.com", "x:*"]


# ---------------------------------------------------------------------------
# build_http_app structure
# ---------------------------------------------------------------------------


def test_build_http_app_returns_starlette_with_default_route(
    registry: WorkspaceRegistry,
) -> None:
    store, coord = _coordinator(registry)
    app = srv.build_http_app(store, coord, registry, "standard")
    assert isinstance(app, Starlette)
    assert "/mcp" in [route.path for route in app.routes]  # type: ignore[attr-defined]


def test_build_http_app_custom_path(registry: WorkspaceRegistry) -> None:
    store, coord = _coordinator(registry)
    app = srv.build_http_app(store, coord, registry, "advanced", path="/rpc")
    assert "/rpc" in [route.path for route in app.routes]  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# Lifespan cleanup
# ---------------------------------------------------------------------------


async def test_lifespan_standard_releases_claims_on_shutdown(
    registry: WorkspaceRegistry,
) -> None:
    store, coord = _coordinator(registry)
    agent_id = coord.register(name="editor")["agent_id"]
    assert coord.claim("custom://res", agent_id)["status"] == "claimed"

    app = srv.build_http_app(store, coord, registry, "standard")
    async with app.router.lifespan_context(app):
        # Still held while the server is up.
        assert coord.status("custom://res")["status"] == "claimed"

    # Shutdown ran _cleanup_agents: the claim is released as 'abandoned'.
    after = coord.status("custom://res")
    assert after["status"] == "available"
    assert after["previous_outcome"] == "abandoned"


async def test_lifespan_advanced_skips_cleanup(
    registry: WorkspaceRegistry, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, coord = _coordinator(registry)
    calls: list[object] = []
    monkeypatch.setattr(srv, "_cleanup_agents", lambda c: calls.append(c))

    app = srv.build_http_app(store, coord, registry, "advanced")
    async with app.router.lifespan_context(app):
        pass

    assert calls == []


async def test_lifespan_standard_invokes_cleanup_hook(
    registry: WorkspaceRegistry, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, coord = _coordinator(registry)
    calls: list[object] = []
    monkeypatch.setattr(srv, "_cleanup_agents", lambda c: calls.append(c))

    app = srv.build_http_app(store, coord, registry, "standard")
    async with app.router.lifespan_context(app):
        pass

    assert calls == [coord]


# ---------------------------------------------------------------------------
# Integration: real uvicorn server on an ephemeral port
# ---------------------------------------------------------------------------


@asynccontextmanager
async def _serve(app: Starlette, host: str = "127.0.0.1") -> AsyncIterator[str]:
    """Run `app` under uvicorn on an ephemeral port; yield its base URL.

    Binds a socket ourselves (port 0) and hands it to uvicorn, eliminating any
    free-port race. Waits for startup with a bounded loop, and shuts down
    deterministically on exit.
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind((host, 0))
    sock.listen(128)
    port = sock.getsockname()[1]

    config = uvicorn.Config(app, log_level="warning")
    server = uvicorn.Server(config)
    # Signal handlers can only be installed in the main thread of the main
    # interpreter; disable them so serve() works inside the test event loop.
    server.install_signal_handlers = lambda: None  # type: ignore[method-assign]

    task = asyncio.create_task(server.serve(sockets=[sock]))
    try:
        for _ in range(250):  # up to ~5s, bounded
            if server.started:
                break
            await asyncio.sleep(0.02)
        else:  # pragma: no cover - only hit if startup hangs
            raise RuntimeError("uvicorn did not start in time")
        yield f"http://{host}:{port}/mcp"
    finally:
        server.should_exit = True
        await task


async def test_http_standard_end_to_end(registry: WorkspaceRegistry) -> None:
    store, coord = _coordinator(registry)
    app = srv.build_http_app(store, coord, registry, "standard")

    async with _serve(app) as url:
        async with streamable_http_client(url) as (read, write, _):
            async with ClientSession(read, write) as session:
                init = await session.initialize()
                assert init.instructions is not None
                assert "agenthold_register" in init.instructions

                tools = await session.list_tools()
                assert {t.name for t in tools.tools} == STANDARD_TOOLS

                reg = _payload(
                    await session.call_tool("agenthold_register", {"name": "editor"})
                )
                agent_id = reg["agent_id"]

                claim = _payload(
                    await session.call_tool(
                        "agenthold_claim",
                        {"resource": "custom://r", "agent_id": agent_id},
                    )
                )
                assert claim["status"] == "claimed"

                status = _payload(
                    await session.call_tool(
                        "agenthold_status", {"resource": "custom://r"}
                    )
                )
                assert status["status"] == "claimed"

                # wait on an unclaimed resource returns 'available' immediately.
                waited = _payload(
                    await session.call_tool(
                        "agenthold_wait",
                        {"resource": "custom://free", "timeout_seconds": 0},
                    )
                )
                assert waited["status"] == "available"

                released = _payload(
                    await session.call_tool(
                        "agenthold_release",
                        {
                            "resource": "custom://r",
                            "agent_id": agent_id,
                            "outcome": "modified",
                        },
                    )
                )
                assert released["status"] == "released"


async def test_http_advanced_end_to_end(registry: WorkspaceRegistry) -> None:
    store, coord = _coordinator(registry)
    app = srv.build_http_app(store, coord, registry, "advanced")

    async with _serve(app) as url:
        async with streamable_http_client(url) as (read, write, _):
            async with ClientSession(read, write) as session:
                init = await session.initialize()
                # Advanced mode sends no coordination instructions.
                assert not init.instructions

                tools = await session.list_tools()
                assert len(tools.tools) == 8

                written = _payload(
                    await session.call_tool(
                        "agenthold_set",
                        {
                            "namespace": "ns",
                            "key": "k",
                            "value": {"a": 1},
                            "updated_by": "tester",
                            "expected_version": 0,
                        },
                    )
                )
                assert written["version"] == 1

                got = _payload(
                    await session.call_tool(
                        "agenthold_get", {"namespace": "ns", "key": "k"}
                    )
                )
                assert got["value"] == {"a": 1}

                # watch with an already-current version + zero timeout: times out.
                watched = _payload(
                    await session.call_tool(
                        "agenthold_watch",
                        {
                            "namespace": "ns",
                            "key": "k",
                            "since_version": 1,
                            "timeout_seconds": 0,
                        },
                    )
                )
                assert watched["status"] == "timeout"


async def test_http_two_sessions_coordinate_and_cleanup(
    registry: WorkspaceRegistry,
) -> None:
    store, coord = _coordinator(registry)
    app = srv.build_http_app(store, coord, registry, "standard")

    async with _serve(app) as url:
        async with streamable_http_client(url) as (r1, w1, _):
            async with ClientSession(r1, w1) as s1:
                await s1.initialize()
                async with streamable_http_client(url) as (r2, w2, _):
                    async with ClientSession(r2, w2) as s2:
                        await s2.initialize()

                        a1 = _payload(
                            await s1.call_tool("agenthold_register", {"name": "one"})
                        )["agent_id"]
                        a2 = _payload(
                            await s2.call_tool("agenthold_register", {"name": "two"})
                        )["agent_id"]
                        assert a1 != a2

                        c1 = _payload(
                            await s1.call_tool(
                                "agenthold_claim",
                                {"resource": "custom://shared", "agent_id": a1},
                            )
                        )
                        assert c1["status"] == "claimed"

                        # Second, independent session sees the resource as busy.
                        c2 = _payload(
                            await s2.call_tool(
                                "agenthold_claim",
                                {"resource": "custom://shared", "agent_id": a2},
                            )
                        )
                        assert c2["status"] == "busy"

    # Server shut down → cleanup released the session agent's claim.
    assert coord.status("custom://shared")["status"] == "available"
