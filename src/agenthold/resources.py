"""
Resource identity for agenthold.

A resource is identified by:
  - scope: "file" or "custom"
  - For "file": a workspace name + workspace-relative path
  - For "custom": an opaque name

Canonical URI form (the value stored as a key in the claims namespace):
  - file://<workspace>/<path>
  - custom://<name>

Agents pass a single string to the coordination tools. Two forms are accepted:
  - URI: explicit, e.g. "file://myproj/src/main.py" or "custom://task-42"
  - Bare path (no "://"): treated as file scope; resolved against the
    "default" workspace if it exists, else the only workspace if exactly
    one is configured. Absolute bare paths are matched against configured
    workspace roots (longest prefix wins) and stripped to relative form.

The WorkspaceRegistry is built from the server's --workspace flags.
"""

from __future__ import annotations

import re
from typing import Any

from pydantic import BaseModel

DEFAULT_WORKSPACE_NAME = "default"
_WORKSPACE_NAME_RE = re.compile(r"^[a-zA-Z0-9_-]+$")
_MAX_WORKSPACE_NAME_LEN = 64
_MAX_RESOURCE_LEN = 400  # input length cap; URI fits comfortably under store's 512


# ---------------------------------------------------------------------------
# Public types
# ---------------------------------------------------------------------------


class Workspace(BaseModel):
    """A configured workspace (logical name + absolute filesystem root)."""

    name: str
    root: str  # absolute path; backslashes accepted but normalized internally


class ResourceId(BaseModel):
    """Canonical resource identifier."""

    scope: str  # "file" or "custom"
    workspace: str | None  # workspace name for file scope; None for custom
    path: str  # file: workspace-relative path; custom: opaque name

    def to_uri(self) -> str:
        if self.scope == "file":
            return f"file://{self.workspace}/{self.path}"
        if self.scope == "custom":
            return f"custom://{self.path}"
        raise ValueError(f"Unknown scope: {self.scope!r}")

    def __str__(self) -> str:
        return self.to_uri()


class WorkspaceRegistry:
    """Holds the configured workspaces and provides lookup + path resolution."""

    def __init__(self, workspaces: list[Workspace]) -> None:
        if not workspaces:
            raise ValueError("At least one workspace is required")
        names = [ws.name for ws in workspaces]
        if len(set(names)) != len(names):
            raise ValueError(f"Workspace names must be unique; got {names}")
        for ws in workspaces:
            self._validate(ws)
        self._workspaces: list[Workspace] = list(workspaces)
        self._by_name: dict[str, Workspace] = {ws.name: ws for ws in workspaces}
        # Sorted longest root first for prefix-stripping
        self._by_root_length: list[Workspace] = sorted(
            workspaces,
            key=lambda w: len(_normalize_root(w.root)),
            reverse=True,
        )

    @staticmethod
    def _validate(ws: Workspace) -> None:
        if not ws.name:
            raise ValueError("Workspace name must not be empty")
        if not _WORKSPACE_NAME_RE.match(ws.name):
            raise ValueError(
                f"Invalid workspace name {ws.name!r}: must match [a-zA-Z0-9_-]+"
            )
        if len(ws.name) > _MAX_WORKSPACE_NAME_LEN:
            raise ValueError(
                f"Workspace name {ws.name!r} exceeds "
                f"{_MAX_WORKSPACE_NAME_LEN} characters"
            )
        if not ws.root:
            raise ValueError(f"Workspace {ws.name!r}: root must not be empty")
        if not _is_absolute_root(ws.root):
            raise ValueError(
                f"Workspace {ws.name!r}: root {ws.root!r} must be an absolute path"
            )

    @property
    def workspaces(self) -> list[Workspace]:
        return list(self._workspaces)

    def get(self, name: str) -> Workspace | None:
        return self._by_name.get(name)

    def require(self, name: str) -> Workspace:
        ws = self.get(name)
        if ws is None:
            available = sorted(self._by_name.keys())
            raise ValueError(f"Unknown workspace {name!r}. Available: {available}")
        return ws

    def default_for_bare_paths(self) -> Workspace | None:
        """Return the workspace used to resolve bare relative paths.

        Rules (in order):
          1. The workspace named "default", if configured.
          2. The only workspace, if exactly one is configured.
          3. None — bare paths must be rejected.
        """
        explicit = self._by_name.get(DEFAULT_WORKSPACE_NAME)
        if explicit is not None:
            return explicit
        if len(self._workspaces) == 1:
            return self._workspaces[0]
        return None

    def longest_match(self, abs_path: str) -> tuple[Workspace, str] | None:
        """Find the workspace whose root is the longest prefix of abs_path.

        Returns (workspace, remainder) where remainder is the portion after
        the matched root with leading slashes stripped. Returns None if no
        workspace root is a prefix.
        """
        norm = abs_path.replace("\\", "/")
        for ws in self._by_root_length:
            root = _normalize_root(ws.root)
            if norm == root:
                return ws, ""
            if norm.startswith(root + "/"):
                remainder = norm[len(root) + 1 :]
                return ws, remainder
        return None


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------


def parse_resource_input(raw: Any, registry: WorkspaceRegistry) -> ResourceId:
    """Parse a raw resource input into a canonical ResourceId.

    Accepts a string in URI form (file://workspace/path or custom://name)
    or bare-path form (resolved against the default workspace).

    Raises ValueError on any validation failure with a message suitable
    for surfacing to the caller.
    """
    _validate_resource_string(raw)
    assert isinstance(raw, str)

    if "://" in raw:
        return _parse_uri(raw, registry)
    return _parse_bare_path(raw, registry)


def _parse_uri(uri: str, registry: WorkspaceRegistry) -> ResourceId:
    scheme, _, rest = uri.partition("://")
    if scheme == "file":
        return _parse_file_uri(rest, registry)
    if scheme == "custom":
        return _parse_custom_uri(rest)
    raise ValueError(f"Unknown resource scheme {scheme!r}. Allowed: 'file', 'custom'.")


def _parse_file_uri(rest: str, registry: WorkspaceRegistry) -> ResourceId:
    workspace_name, _, path_str = rest.partition("/")
    if not workspace_name:
        raise ValueError(
            "file URI must include a workspace name, e.g. 'file://default/src/main.py'"
        )
    workspace = registry.require(workspace_name)
    if not path_str:
        raise ValueError(
            f"file URI {f'file://{workspace_name}/'!r} must include a non-empty path"
        )
    canonical_path = _normalize_relative_path(path_str)
    return ResourceId(scope="file", workspace=workspace.name, path=canonical_path)


def _parse_custom_uri(rest: str) -> ResourceId:
    if not rest:
        raise ValueError("custom URI must include a name, e.g. 'custom://task-42'")
    if "\x00" in rest:
        raise ValueError("custom name must not contain null bytes")
    if not rest.strip():
        raise ValueError("custom name must not be whitespace-only")
    if len(rest) > _MAX_RESOURCE_LEN:
        raise ValueError(f"custom name exceeds {_MAX_RESOURCE_LEN} characters")
    return ResourceId(scope="custom", workspace=None, path=rest)


def _parse_bare_path(raw: str, registry: WorkspaceRegistry) -> ResourceId:
    normalized_sep = raw.replace("\\", "/")
    if _is_absolute(normalized_sep):
        match = registry.longest_match(normalized_sep)
        if match is None:
            available = sorted(w.name for w in registry.workspaces)
            raise ValueError(
                f"Absolute path {raw!r} is not inside any configured workspace. "
                f"Available workspaces: {available}. "
                "Pass a workspace-relative path or a 'file://<workspace>/<path>' URI."
            )
        ws, remainder = match
        if not remainder:
            raise ValueError(
                f"Absolute path {raw!r} resolves to the workspace root with no "
                "trailing path component; agenthold coordinates resources, not "
                "the workspace itself."
            )
        canonical_path = _normalize_relative_path(remainder)
        return ResourceId(scope="file", workspace=ws.name, path=canonical_path)

    # Relative bare path — needs a default workspace
    default = registry.default_for_bare_paths()
    if default is None:
        available = sorted(w.name for w in registry.workspaces)
        raise ValueError(
            f"Bare relative path {raw!r} requires a 'default' workspace, but "
            f"none is configured. Available workspaces: {available}. "
            "Pass a 'file://<workspace>/<path>' URI to be explicit."
        )
    canonical_path = _normalize_relative_path(raw)
    return ResourceId(scope="file", workspace=default.name, path=canonical_path)


# ---------------------------------------------------------------------------
# Normalization helpers
# ---------------------------------------------------------------------------


def _validate_resource_string(raw: Any) -> None:
    if not isinstance(raw, str):
        raise ValueError("resource must be a string")
    if not raw:
        raise ValueError("resource must not be empty")
    if "\x00" in raw:
        raise ValueError("resource must not contain null bytes")
    if len(raw) > _MAX_RESOURCE_LEN:
        raise ValueError(f"resource exceeds {_MAX_RESOURCE_LEN} characters")


def _normalize_relative_path(path: str) -> str:
    """Normalize a relative-only path into canonical form.

    - Convert backslashes to forward slashes
    - Strip leading ``./`` segments
    - Collapse repeated ``/``
    - Strip leading ``/``
    - Reject ``.``, ``..`` and empty segments

    Raises ValueError on rejection.
    """
    if not path:
        raise ValueError("path must not be empty")
    normalized = path.replace("\\", "/")

    # Strip leading "./" repeatedly
    while normalized.startswith("./"):
        normalized = normalized[2:]
    if normalized == ".":
        raise ValueError("path is empty after stripping './' segments")

    # Collapse repeated slashes
    normalized = re.sub(r"/+", "/", normalized)
    # Strip leading slashes (paths inside a URI may have them)
    normalized = normalized.lstrip("/")

    if not normalized:
        raise ValueError("path is empty after normalization")

    segments = normalized.split("/")
    for seg in segments:
        if seg == "":
            raise ValueError(f"path {path!r} contains empty segments")
        if seg == ".":
            raise ValueError(f"path {path!r} contains '.' segments")
        if seg == "..":
            raise ValueError(
                f"path {path!r} contains '..' segments (path traversal not allowed)"
            )

    return normalized


def _normalize_root(root: str) -> str:
    """Normalize a workspace root for prefix matching.

    - Backslash → forward slash
    - Remove trailing slashes (so root + '/' + remainder always works)
    """
    norm = root.replace("\\", "/").rstrip("/")
    return norm or "/"


def _is_absolute(path_with_forward_slashes: str) -> bool:
    """Detect POSIX or Windows-style absolute paths (with forward slashes)."""
    if path_with_forward_slashes.startswith("/"):
        return True
    # Windows-style: "C:/..." after backslash normalization
    if (
        len(path_with_forward_slashes) >= 3
        and path_with_forward_slashes[1] == ":"
        and path_with_forward_slashes[2] == "/"
    ):
        return True
    return False


def _is_absolute_root(root: str) -> bool:
    """Validate that a workspace root is absolute."""
    return _is_absolute(root.replace("\\", "/"))
