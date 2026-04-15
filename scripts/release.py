#!/usr/bin/env python3
"""Interactive release helper for agenthold.

Usage:
    python scripts/release.py                    # fully interactive
    python scripts/release.py 0.4.4              # version fixed, still confirms
    python scripts/release.py --bump patch       # non-interactive bump
    python scripts/release.py --dry-run          # preview, no writes
    python scripts/release.py --no-push          # stop after tag creation
    python scripts/release.py --branch my-branch # allow release from non-main
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from datetime import date
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
INIT_PY = REPO_ROOT / "src" / "agenthold" / "__init__.py"
PYPROJECT = REPO_ROOT / "pyproject.toml"
SERVER_JSON = REPO_ROOT / "server.json"
CHANGELOG = REPO_ROOT / "CHANGELOG.md"
SCHEMA_URL = (
    "https://static.modelcontextprotocol.io/schemas/2025-12-11/server.schema.json"
)

VERSION_RE = re.compile(r"^\d+\.\d+\.\d+$")


# --- Pure functions (unit-tested) ---


def parse_version(s: str) -> tuple[int, int, int]:
    if not VERSION_RE.match(s):
        raise ValueError(f"Invalid version: {s!r}")
    major, minor, patch = s.split(".")
    return int(major), int(minor), int(patch)


def bump_version(current: str, kind: str) -> str:
    major, minor, patch = parse_version(current)
    if kind == "patch":
        patch += 1
    elif kind == "minor":
        minor += 1
        patch = 0
    elif kind == "major":
        major += 1
        minor = 0
        patch = 0
    else:
        raise ValueError(f"Unknown bump kind: {kind!r}")
    return f"{major}.{minor}.{patch}"


def extract_current_version(init_py_content: str) -> str:
    m = re.search(r'^__version__\s*=\s*"([^"]+)"', init_py_content, re.MULTILINE)
    if not m:
        raise ValueError("__version__ not found in __init__.py")
    return m.group(1)


def patch_init_py(content: str, new_version: str) -> str:
    pattern = re.compile(r'^__version__\s*=\s*"[^"]+"', re.MULTILINE)
    if not pattern.search(content):
        raise ValueError("__version__ line not found")
    return pattern.sub(f'__version__ = "{new_version}"', content, count=1)


def patch_pyproject_toml(content: str, new_version: str) -> str:
    pattern = re.compile(r'^version\s*=\s*"[^"]+"', re.MULTILINE)
    if not pattern.search(content):
        raise ValueError("version field not found in pyproject.toml")
    return pattern.sub(f'version = "{new_version}"', content, count=1)


def patch_server_json(content: str, new_version: str) -> str:
    data = json.loads(content)
    data["version"] = new_version
    data["packages"][0]["version"] = new_version
    return json.dumps(data, indent=2) + "\n"


def insert_changelog_stub(content: str, new_version: str, today: str) -> str:
    stub = (
        f"## [{new_version}] - {today}\n\n"
        "### Changed\n"
        "- TODO: describe changes\n\n"
        "---\n\n"
    )
    m = re.search(r"^## \[", content, re.MULTILINE)
    if not m:
        raise ValueError("No existing changelog entries found")
    return content[: m.start()] + stub + content[m.start() :]


def extract_changelog_section(content: str, version: str) -> str:
    pattern = re.compile(
        rf"^## \[{re.escape(version)}\][^\n]*\n(.*?)(?=^## \[|\Z)",
        re.MULTILINE | re.DOTALL,
    )
    m = pattern.search(content)
    if not m:
        raise ValueError(f"No CHANGELOG section for {version}")
    body = m.group(1).strip()
    body = re.sub(r"\n?---\s*$", "", body).strip()
    return body


def changelog_has_todo(content: str, version: str) -> bool:
    return "TODO" in extract_changelog_section(content, version)


# --- Orchestration (not unit-tested; exercise via --dry-run) ---


def _run(
    cmd: list[str], *, check: bool = True, capture: bool = False
) -> subprocess.CompletedProcess[str]:  # pragma: no cover
    return subprocess.run(
        cmd,
        check=check,
        capture_output=capture,
        text=True,
        cwd=REPO_ROOT,
    )


def _prompt(question: str, default: str = "") -> str:  # pragma: no cover
    suffix = f" [{default}]" if default else ""
    answer = input(f"{question}{suffix} ").strip()
    return answer or default


def _confirm(question: str, default: bool = False) -> bool:  # pragma: no cover
    hint = "Y/n" if default else "y/N"
    answer = input(f"{question} [{hint}] ").strip().lower()
    if not answer:
        return default
    return answer in ("y", "yes")


def _die(msg: str) -> None:  # pragma: no cover
    print(f"ERROR: {msg}", file=sys.stderr)
    sys.exit(1)


def _step(n: int, total: int, title: str) -> None:  # pragma: no cover
    print(f"\n=== {n}/{total} — {title} ===")


def preflight(branch: str) -> None:  # pragma: no cover
    if not (REPO_ROOT / ".git").exists():
        _die("Not a git repo")
    current = _run(
        ["git", "rev-parse", "--abbrev-ref", "HEAD"], capture=True
    ).stdout.strip()
    if current != branch:
        _die(f"On branch {current!r}, expected {branch!r}. Override with --branch.")
    dirty = _run(["git", "status", "--porcelain"], capture=True).stdout.strip()
    if dirty:
        _die("Working tree is not clean. Commit or stash first.")
    _run(["git", "fetch", "origin", branch])
    ahead_behind = _run(
        ["git", "rev-list", "--left-right", "--count", f"origin/{branch}...HEAD"],
        capture=True,
    ).stdout.strip()
    behind, ahead = ahead_behind.split()
    if int(behind) > 0:
        _die(f"Local {branch} is {behind} commit(s) behind origin. Pull first.")


def tag_exists(version: str) -> bool:  # pragma: no cover
    tag = f"v{version}"
    local = _run(["git", "tag", "-l", tag], capture=True).stdout.strip()
    if local:
        return True
    remote = _run(
        ["git", "ls-remote", "--tags", "origin", tag], capture=True
    ).stdout.strip()
    return bool(remote)


def validate_server_json() -> None:  # pragma: no cover
    if _has_tool("mcp-publisher"):
        _run(["mcp-publisher", "validate"])
        return
    if _has_tool("check-jsonschema"):
        _run(
            [
                "check-jsonschema",
                "--schemafile",
                SCHEMA_URL,
                str(SERVER_JSON),
            ]
        )
        return
    print("WARNING: neither mcp-publisher nor check-jsonschema found; skipping.")


def _has_tool(name: str) -> bool:  # pragma: no cover
    from shutil import which

    return which(name) is not None


def launch_editor(path: Path) -> None:  # pragma: no cover
    editor = os.environ.get("EDITOR") or ("notepad" if os.name == "nt" else "nano")
    # Editor may include args, e.g. "code --wait".
    parts = editor.split()
    subprocess.run([*parts, str(path)], check=True)


def run_quality_gates() -> None:  # pragma: no cover
    gates = [
        ["uv", "run", "ruff", "check", "src/", "tests/"],
        ["uv", "run", "ruff", "format", "--check", "src/", "tests/"],
        ["uv", "run", "mypy", "src/"],
        ["uv", "run", "pytest", "tests/", "-q", "--tb=short"],
        [
            "uv",
            "run",
            "pytest",
            "tests/",
            "--cov=agenthold",
            "--cov-report=term-missing",
            "--cov-fail-under=80",
            "-q",
        ],
    ]
    for cmd in gates:
        print(f"  $ {' '.join(cmd)}")
        _run(cmd)


def main() -> int:  # pragma: no cover
    parser = argparse.ArgumentParser(description="Cut a new agenthold release")
    parser.add_argument("version", nargs="?", help="Explicit version (X.Y.Z)")
    parser.add_argument(
        "--bump", choices=["patch", "minor", "major"], help="Non-interactive bump"
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="Preview without mutating"
    )
    parser.add_argument(
        "--no-push", action="store_true", help="Stop after tag creation"
    )
    parser.add_argument(
        "--branch", default="main", help="Required branch (default: main)"
    )
    args = parser.parse_args()

    total = 8

    _step(1, total, "Preconditions")
    if args.dry_run:
        print("(dry-run) skipping git preconditions")
    else:
        preflight(args.branch)
    current = extract_current_version(INIT_PY.read_text(encoding="utf-8"))
    print(f"Current version: {current}")

    _step(2, total, "Pick new version")
    if args.version:
        new_version = args.version
        parse_version(new_version)
    else:
        kind = args.bump or _prompt(
            "Bump [p]atch / [m]inor / [M]ajor / [c]ustom?", default="p"
        )
        kind_map = {"p": "patch", "m": "minor", "M": "major", "c": "custom"}
        kind_full = kind_map.get(kind, kind)
        if kind_full == "custom":
            new_version = _prompt("Enter version (X.Y.Z):")
            parse_version(new_version)
        else:
            new_version = bump_version(current, kind_full)
        if not args.bump and not _confirm(
            f"New version will be {new_version}. Proceed?", default=True
        ):
            return 1
    if not args.dry_run and tag_exists(new_version):
        _die(f"Tag v{new_version} already exists (local or origin).")

    _step(3, total, "Rewrite version files")
    init_text = INIT_PY.read_text(encoding="utf-8")
    py_text = PYPROJECT.read_text(encoding="utf-8")
    sj_text = SERVER_JSON.read_text(encoding="utf-8")
    new_init = patch_init_py(init_text, new_version)
    new_py = patch_pyproject_toml(py_text, new_version)
    new_sj = patch_server_json(sj_text, new_version)
    if args.dry_run:
        print(f"(dry-run) would write {new_version} to 3 files")
    else:
        INIT_PY.write_text(new_init, encoding="utf-8")
        PYPROJECT.write_text(new_py, encoding="utf-8")
        SERVER_JSON.write_text(new_sj, encoding="utf-8")
        print(f"  wrote {INIT_PY.relative_to(REPO_ROOT)}")
        print(f"  wrote {PYPROJECT.relative_to(REPO_ROOT)}")
        print(f"  wrote {SERVER_JSON.relative_to(REPO_ROOT)}")

    _step(4, total, "Validate server.json")
    if args.dry_run:
        print("(dry-run) would validate")
    else:
        validate_server_json()

    _step(5, total, "CHANGELOG entry")
    cl_text = CHANGELOG.read_text(encoding="utf-8")
    if f"## [{new_version}]" in cl_text:
        print(f"CHANGELOG already has a section for {new_version}; skipping stub.")
    else:
        new_cl = insert_changelog_stub(cl_text, new_version, date.today().isoformat())
        if args.dry_run:
            print("(dry-run) would insert stub and launch editor")
        else:
            CHANGELOG.write_text(new_cl, encoding="utf-8")
            print("Inserted stub. Launching editor...")
            launch_editor(CHANGELOG)
            updated = CHANGELOG.read_text(encoding="utf-8")
            if changelog_has_todo(updated, new_version):
                _die("CHANGELOG still contains 'TODO:' — please fill in the entry.")

    _step(6, total, "Quality gates")
    if args.dry_run:
        print("(dry-run) skipping gates")
    else:
        try:
            run_quality_gates()
        except subprocess.CalledProcessError:
            if _confirm("Gates failed. Revert file changes?", default=True):
                INIT_PY.write_text(init_text, encoding="utf-8")
                PYPROJECT.write_text(py_text, encoding="utf-8")
                SERVER_JSON.write_text(sj_text, encoding="utf-8")
                CHANGELOG.write_text(cl_text, encoding="utf-8")
                print("Reverted.")
            return 1

    _step(7, total, "Commit and tag")
    files = [
        "src/agenthold/__init__.py",
        "pyproject.toml",
        "server.json",
        "CHANGELOG.md",
        "uv.lock",
    ]
    if args.dry_run:
        print(f"(dry-run) would commit: {files}")
        print(f"(dry-run) would create annotated tag v{new_version}")
    else:
        _run(["git", "diff", "--stat", "--", *files])
        if not _confirm(f"Commit as 'release: v{new_version}'?", default=True):
            return 1
        _run(["git", "add", "--", *files])
        _run(["git", "commit", "-m", f"release: v{new_version}"])
        tag_msg = extract_changelog_section(
            CHANGELOG.read_text(encoding="utf-8"), new_version
        )
        _run(
            ["git", "tag", "-a", f"v{new_version}", "-m", tag_msg],
        )
        print(f"Created tag v{new_version}")

    _step(8, total, "Push")
    push_hint = f"git push origin {args.branch} && git push origin v{new_version}"
    if args.no_push or args.dry_run:
        print("Skipped push.")
        print(f"To publish: {push_hint}")
        return 0
    if not _confirm(f"Push {args.branch} and v{new_version} to origin?", default=False):
        print(f"Skipped push. Run: {push_hint}")
        return 0
    _run(["git", "push", "origin", args.branch])
    _run(["git", "push", "origin", f"v{new_version}"])
    print("\nDone. Watch: https://github.com/edobusy/agenthold/actions")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
