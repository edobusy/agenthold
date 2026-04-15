"""Unit tests for the pure functions in scripts/release.py."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import release  # noqa: E402, I001


# --- parse_version ---


@pytest.mark.parametrize("s", ["0.0.1", "1.2.3", "10.20.30"])
def test_parse_version_accepts_semver(s: str) -> None:
    release.parse_version(s)


@pytest.mark.parametrize(
    "s", ["", "1", "1.2", "1.2.3.4", "v1.2.3", "1.2.3-rc1", "a.b.c"]
)
def test_parse_version_rejects_bad(s: str) -> None:
    with pytest.raises(ValueError):
        release.parse_version(s)


# --- bump_version ---


@pytest.mark.parametrize(
    ("current", "kind", "expected"),
    [
        ("0.4.3", "patch", "0.4.4"),
        ("0.4.3", "minor", "0.5.0"),
        ("0.4.3", "major", "1.0.0"),
        ("1.9.9", "patch", "1.9.10"),
        ("1.9.9", "minor", "1.10.0"),
    ],
)
def test_bump_version(current: str, kind: str, expected: str) -> None:
    assert release.bump_version(current, kind) == expected


def test_bump_version_unknown_kind() -> None:
    with pytest.raises(ValueError):
        release.bump_version("1.0.0", "bogus")


# --- extract_current_version + patch_init_py ---


def test_extract_current_version() -> None:
    src = '"""docstring"""\n\n__version__ = "1.2.3"\n'
    assert release.extract_current_version(src) == "1.2.3"


def test_extract_current_version_missing() -> None:
    with pytest.raises(ValueError):
        release.extract_current_version("no version here")


def test_patch_init_py_replaces_only_version_line() -> None:
    src = '"""d"""\n__version__ = "0.1.0"\n\nx = "__version__ = \\"spoof\\""\n'
    patched = release.patch_init_py(src, "0.2.0")
    assert '__version__ = "0.2.0"' in patched
    assert '__version__ = "0.1.0"' not in patched
    # sanity: the string literal that contained a version-like substring is untouched
    assert 'x = "__version__' in patched


def test_patch_init_py_missing_raises() -> None:
    with pytest.raises(ValueError):
        release.patch_init_py("no version", "1.0.0")


# --- patch_pyproject_toml ---


def test_patch_pyproject_toml_replaces_project_version() -> None:
    src = '[project]\nname = "x"\nversion = "0.1.0"\n'
    assert 'version = "0.2.0"' in release.patch_pyproject_toml(src, "0.2.0")


def test_patch_pyproject_toml_missing_raises() -> None:
    with pytest.raises(ValueError):
        release.patch_pyproject_toml('[project]\nname = "x"\n', "0.2.0")


# --- patch_server_json ---


def test_patch_server_json_updates_both_version_fields() -> None:
    src = json.dumps(
        {
            "name": "io.github.x/y",
            "version": "0.1.0",
            "packages": [{"identifier": "y", "version": "0.1.0"}],
        }
    )
    out = release.patch_server_json(src, "0.2.0")
    parsed = json.loads(out)
    assert parsed["version"] == "0.2.0"
    assert parsed["packages"][0]["version"] == "0.2.0"
    assert out.endswith("\n")


# --- CHANGELOG helpers ---


CHANGELOG_SAMPLE = """# Changelog

Preamble.

---

## [0.4.3] - 2026-04-15

### Added
- Thing.

---

## [0.4.2] - 2026-03-20

### Fixed
- Bug.
"""


def test_insert_changelog_stub_before_latest_entry() -> None:
    out = release.insert_changelog_stub(CHANGELOG_SAMPLE, "0.4.4", "2026-04-20")
    assert "## [0.4.4] - 2026-04-20" in out
    idx_new = out.index("## [0.4.4]")
    idx_old = out.index("## [0.4.3]")
    assert idx_new < idx_old
    assert "TODO: describe changes" in out


def test_insert_changelog_stub_no_entries_raises() -> None:
    with pytest.raises(ValueError):
        release.insert_changelog_stub("# Changelog\n\nEmpty.", "0.1.0", "2026-04-20")


def test_extract_changelog_section_reads_correct_block() -> None:
    section = release.extract_changelog_section(CHANGELOG_SAMPLE, "0.4.3")
    assert "### Added" in section
    assert "Thing." in section
    # Should not bleed into the 0.4.2 section.
    assert "0.4.2" not in section
    assert "Bug." not in section


def test_extract_changelog_section_last_entry() -> None:
    section = release.extract_changelog_section(CHANGELOG_SAMPLE, "0.4.2")
    assert "### Fixed" in section
    assert "Bug." in section


def test_extract_changelog_section_missing() -> None:
    with pytest.raises(ValueError):
        release.extract_changelog_section(CHANGELOG_SAMPLE, "9.9.9")


def test_changelog_has_todo_detects_stub() -> None:
    content = release.insert_changelog_stub(CHANGELOG_SAMPLE, "0.4.4", "2026-04-20")
    assert release.changelog_has_todo(content, "0.4.4") is True


def test_changelog_has_todo_false_after_edit() -> None:
    content = release.insert_changelog_stub(CHANGELOG_SAMPLE, "0.4.4", "2026-04-20")
    content = content.replace("TODO: describe changes", "Real entry")
    assert release.changelog_has_todo(content, "0.4.4") is False
