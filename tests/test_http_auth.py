"""Tests for bearer-token authentication on the HTTP transport."""

from __future__ import annotations

import asyncio
import json
import socket
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx
import pytest
import uvicorn
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from starlette.applications import Starlette

from agenthold import server as srv
from agenthold.coordinator import Coordinator
from agenthold.resources import Workspace, WorkspaceRegistry
from agenthold.store import StateStore


@pytest.fixture
def registry() -> WorkspaceRegistry:
    return WorkspaceRegistry([Workspace(name="default", root="/work")])


def _app(registry: WorkspaceRegistry, tokens: frozenset[str] | None) -> Starlette:
    store = StateStore(":memory:")
    coord = Coordinator(store, registry)
    return srv.build_http_app(store, coord, registry, "standard", auth_tokens=tokens)


@asynccontextmanager
async def _serve(app: Starlette, host: str = "127.0.0.1") -> AsyncIterator[str]:
    """Run `app` under uvicorn on an ephemeral port; yield its base /mcp URL.

    Mirrors the helper in test_http_transport (kept local to keep this feature's
    tests self-contained).
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind((host, 0))
    sock.listen(128)
    port = sock.getsockname()[1]

    config = uvicorn.Config(
        app, log_level="warning", lifespan="on", timeout_graceful_shutdown=10
    )
    server = uvicorn.Server(config)
    server.install_signal_handlers = lambda: None  # type: ignore[method-assign]

    task = asyncio.create_task(server.serve(sockets=[sock]))
    try:
        for _ in range(250):
            if server.started:
                break
            await asyncio.sleep(0.02)
        else:  # pragma: no cover - only hit if startup hangs
            raise RuntimeError("uvicorn did not start in time")
        yield f"http://{host}:{port}/mcp"
    finally:
        server.should_exit = True
        try:
            await asyncio.wait_for(task, timeout=15)
        except TimeoutError:  # pragma: no cover - teardown safety net
            task.cancel()


async def _post(
    url: str, token: str | None = None, scheme: str = "Bearer"
) -> httpx.Response:
    headers = {"Accept": "application/json, text/event-stream"}
    if token is not None:
        headers["Authorization"] = f"{scheme} {token}"
    async with httpx.AsyncClient() as client:
        return await client.post(url, json={}, headers=headers)


# ---------------------------------------------------------------------------
# Token collection
# ---------------------------------------------------------------------------


def test_collect_tokens_cli_only() -> None:
    assert srv._collect_auth_tokens(["a", "b"], None) == frozenset({"a", "b"})


def test_collect_tokens_env_comma_separated() -> None:
    assert srv._collect_auth_tokens(None, "x,y,z") == frozenset({"x", "y", "z"})


def test_collect_tokens_merged_and_strips_blanks() -> None:
    # CLI + env merged; blank/whitespace-only entries dropped.
    assert srv._collect_auth_tokens(["a", "  "], "a,b, ,") == frozenset({"a", "b"})


def test_collect_tokens_empty() -> None:
    assert srv._collect_auth_tokens(None, None) == frozenset()
    assert srv._collect_auth_tokens([], "") == frozenset()
    assert srv._collect_auth_tokens([" "], " , ") == frozenset()


def test_auth_token_arg_parsing() -> None:
    args = srv._build_arg_parser().parse_args(
        ["--transport", "http", "--auth-token", "t1", "--auth-token", "t2"]
    )
    assert args.auth_token == ["t1", "t2"]


def test_auth_token_defaults_none() -> None:
    assert srv._build_arg_parser().parse_args([]).auth_token is None


# ---------------------------------------------------------------------------
# build_http_app wiring
# ---------------------------------------------------------------------------


def test_build_http_app_wraps_endpoint_with_auth(
    registry: WorkspaceRegistry,
) -> None:
    app = _app(registry, frozenset({"secret"}))
    assert isinstance(app.routes[0].endpoint, srv._BearerAuthASGIApp)  # type: ignore[attr-defined]


def test_build_http_app_no_wrapper_without_auth(
    registry: WorkspaceRegistry,
) -> None:
    for tokens in (None, frozenset()):
        app = _app(registry, tokens)
        assert isinstance(app.routes[0].endpoint, srv._StreamableHTTPASGIApp)  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# Integration: real server, auth enforced
# ---------------------------------------------------------------------------


async def test_no_auth_allows_request(registry: WorkspaceRegistry) -> None:
    async with _serve(_app(registry, None)) as url:
        resp = await _post(url, token=None)
        # Reaches the MCP handler (may be 4xx for the empty body) but NOT 401.
        assert resp.status_code != 401


async def test_missing_token_rejected(registry: WorkspaceRegistry) -> None:
    async with _serve(_app(registry, frozenset({"secret"}))) as url:
        resp = await _post(url, token=None)
        assert resp.status_code == 401
        assert resp.headers.get("www-authenticate") == "Bearer"
        assert resp.json()["error"] == "unauthorized"
        assert "secret" not in resp.text  # never leak the token


async def test_wrong_token_rejected(registry: WorkspaceRegistry) -> None:
    async with _serve(_app(registry, frozenset({"secret"}))) as url:
        resp = await _post(url, token="not-it")
        assert resp.status_code == 401


async def test_non_bearer_scheme_rejected(registry: WorkspaceRegistry) -> None:
    async with _serve(_app(registry, frozenset({"secret"}))) as url:
        resp = await _post(url, token="secret", scheme="Basic")
        assert resp.status_code == 401


async def test_lowercase_scheme_accepted(registry: WorkspaceRegistry) -> None:
    async with _serve(_app(registry, frozenset({"secret"}))) as url:
        resp = await _post(url, token="secret", scheme="bearer")
        assert resp.status_code != 401  # RFC 7235: scheme is case-insensitive


async def test_multiple_tokens_each_authorizes(
    registry: WorkspaceRegistry,
) -> None:
    async with _serve(_app(registry, frozenset({"alpha", "beta"}))) as url:
        assert (await _post(url, token="alpha")).status_code != 401
        assert (await _post(url, token="beta")).status_code != 401
        assert (await _post(url, token="gamma")).status_code == 401


async def test_valid_token_full_flow(registry: WorkspaceRegistry) -> None:
    async with _serve(_app(registry, frozenset({"s3cr3t"}))) as url:
        async with httpx.AsyncClient(
            headers={"Authorization": "Bearer s3cr3t"}
        ) as client:
            async with streamable_http_client(url, http_client=client) as (
                read,
                write,
                _,
            ):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    tools = await session.list_tools()
                    assert any(t.name == "agenthold_register" for t in tools.tools)
                    result = await session.call_tool(
                        "agenthold_register", {"name": "authed"}
                    )
                    payload = json.loads(result.content[0].text)  # type: ignore[attr-defined]
                    assert payload["status"] == "registered"
