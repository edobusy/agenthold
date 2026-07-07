"""Tests for the resources module: workspaces, registry, parsing, canonicalization."""

import pytest

from agenthold.resources import (
    DEFAULT_WORKSPACE_NAME,
    ResourceId,
    Workspace,
    WorkspaceRegistry,
    parse_resource_input,
)

# ---------------------------------------------------------------------------
# Workspace and WorkspaceRegistry construction
# ---------------------------------------------------------------------------


class TestWorkspaceValidation:
    def test_accepts_posix_absolute_root(self) -> None:
        WorkspaceRegistry([Workspace(name="proj", root="/abs/path")])

    def test_accepts_windows_absolute_root(self) -> None:
        WorkspaceRegistry([Workspace(name="proj", root="C:\\path")])

    def test_accepts_windows_forward_slash_root(self) -> None:
        WorkspaceRegistry([Workspace(name="proj", root="C:/path")])

    def test_rejects_relative_root(self) -> None:
        with pytest.raises(ValueError, match="absolute"):
            WorkspaceRegistry([Workspace(name="proj", root="relative/path")])

    def test_rejects_empty_root(self) -> None:
        with pytest.raises(ValueError, match="root"):
            WorkspaceRegistry([Workspace(name="proj", root="")])

    def test_rejects_invalid_name_with_slash(self) -> None:
        with pytest.raises(ValueError, match="name"):
            WorkspaceRegistry([Workspace(name="bad/name", root="/abs")])

    def test_rejects_invalid_name_with_space(self) -> None:
        with pytest.raises(ValueError, match="name"):
            WorkspaceRegistry([Workspace(name="bad name", root="/abs")])

    def test_rejects_empty_name(self) -> None:
        with pytest.raises(ValueError, match="name"):
            WorkspaceRegistry([Workspace(name="", root="/abs")])

    def test_rejects_overlong_name(self) -> None:
        with pytest.raises(ValueError, match="64"):
            WorkspaceRegistry([Workspace(name="x" * 65, root="/abs")])

    def test_rejects_empty_workspace_list(self) -> None:
        with pytest.raises(ValueError, match="At least one workspace"):
            WorkspaceRegistry([])

    def test_rejects_duplicate_names(self) -> None:
        with pytest.raises(ValueError, match="unique"):
            WorkspaceRegistry(
                [
                    Workspace(name="dup", root="/a"),
                    Workspace(name="dup", root="/b"),
                ]
            )

    def test_accepts_alphanumeric_name(self) -> None:
        WorkspaceRegistry([Workspace(name="abc123_-XYZ", root="/abs")])


class TestRegistryLookup:
    def test_get_returns_workspace(self) -> None:
        ws = Workspace(name="proj", root="/abs")
        registry = WorkspaceRegistry([ws])
        assert registry.get("proj") == ws

    def test_get_unknown_returns_none(self) -> None:
        registry = WorkspaceRegistry([Workspace(name="proj", root="/abs")])
        assert registry.get("unknown") is None

    def test_require_raises_for_unknown(self) -> None:
        registry = WorkspaceRegistry([Workspace(name="proj", root="/abs")])
        with pytest.raises(ValueError, match="Unknown workspace"):
            registry.require("unknown")


class TestDefaultForBarePaths:
    def test_default_workspace_wins(self) -> None:
        registry = WorkspaceRegistry(
            [
                Workspace(name="default", root="/d"),
                Workspace(name="other", root="/o"),
            ]
        )
        assert registry.default_for_bare_paths().name == "default"

    def test_single_workspace_is_default(self) -> None:
        registry = WorkspaceRegistry([Workspace(name="myproj", root="/abs")])
        assert registry.default_for_bare_paths().name == "myproj"

    def test_multiple_no_default_returns_none(self) -> None:
        registry = WorkspaceRegistry(
            [
                Workspace(name="a", root="/a"),
                Workspace(name="b", root="/b"),
            ]
        )
        assert registry.default_for_bare_paths() is None


class TestLongestMatch:
    def test_longest_root_wins(self) -> None:
        registry = WorkspaceRegistry(
            [
                Workspace(name="outer", root="/foo"),
                Workspace(name="inner", root="/foo/bar"),
            ]
        )
        match = registry.longest_match("/foo/bar/baz.py")
        assert match is not None
        ws, rem = match
        assert ws.name == "inner"
        assert rem == "baz.py"

    def test_match_outside_returns_none(self) -> None:
        registry = WorkspaceRegistry([Workspace(name="proj", root="/foo")])
        assert registry.longest_match("/bar/baz") is None

    def test_match_at_root_returns_empty_remainder(self) -> None:
        registry = WorkspaceRegistry([Workspace(name="proj", root="/foo")])
        match = registry.longest_match("/foo")
        assert match is not None
        _, rem = match
        assert rem == ""

    def test_match_with_trailing_slash_in_root(self) -> None:
        registry = WorkspaceRegistry([Workspace(name="proj", root="/foo/")])
        match = registry.longest_match("/foo/x.py")
        assert match is not None
        _, rem = match
        assert rem == "x.py"

    def test_match_with_backslash_root(self) -> None:
        registry = WorkspaceRegistry([Workspace(name="proj", root="C:\\foo")])
        match = registry.longest_match("C:/foo/x.py")
        assert match is not None
        _, rem = match
        assert rem == "x.py"

    def test_match_does_not_collide_on_prefix(self) -> None:
        """A workspace at /foo must not match /foobar/x."""
        registry = WorkspaceRegistry([Workspace(name="proj", root="/foo")])
        assert registry.longest_match("/foobar/x") is None


# ---------------------------------------------------------------------------
# parse_resource_input — file scope, bare relative paths
# ---------------------------------------------------------------------------


@pytest.fixture
def default_registry() -> WorkspaceRegistry:
    return WorkspaceRegistry([Workspace(name="default", root="/proj")])


@pytest.fixture
def multi_registry() -> WorkspaceRegistry:
    return WorkspaceRegistry(
        [
            Workspace(name="a", root="/projects/a"),
            Workspace(name="b", root="/projects/b"),
        ]
    )


class TestBareRelativePath:
    def test_simple_relative(self, default_registry: WorkspaceRegistry) -> None:
        rid = parse_resource_input("src/main.py", default_registry)
        assert rid.scope == "file"
        assert rid.workspace == "default"
        assert rid.path == "src/main.py"
        assert rid.to_uri() == "file://default/src/main.py"

    def test_strips_dot_slash(self, default_registry: WorkspaceRegistry) -> None:
        rid = parse_resource_input("./src/main.py", default_registry)
        assert rid.path == "src/main.py"

    def test_strips_repeated_dot_slash(
        self, default_registry: WorkspaceRegistry
    ) -> None:
        rid = parse_resource_input("././src/main.py", default_registry)
        assert rid.path == "src/main.py"

    def test_collapses_slashes(self, default_registry: WorkspaceRegistry) -> None:
        rid = parse_resource_input("src//main.py", default_registry)
        assert rid.path == "src/main.py"

    def test_backslash_to_slash(self, default_registry: WorkspaceRegistry) -> None:
        rid = parse_resource_input("src\\main.py", default_registry)
        assert rid.path == "src/main.py"

    def test_combined_normalization(self, default_registry: WorkspaceRegistry) -> None:
        rid = parse_resource_input(".\\src\\\\main.py", default_registry)
        assert rid.path == "src/main.py"

    def test_rejects_dot_dot(self, default_registry: WorkspaceRegistry) -> None:
        with pytest.raises(ValueError, match=r"\.\."):
            parse_resource_input("../etc/passwd", default_registry)

    def test_rejects_inner_dot_dot(self, default_registry: WorkspaceRegistry) -> None:
        with pytest.raises(ValueError, match=r"\.\."):
            parse_resource_input("src/../etc/passwd", default_registry)

    def test_rejects_dot_segment(self, default_registry: WorkspaceRegistry) -> None:
        with pytest.raises(ValueError, match=r"'\.'"):
            parse_resource_input("src/./main.py", default_registry)

    def test_rejects_empty(self, default_registry: WorkspaceRegistry) -> None:
        with pytest.raises(ValueError, match="empty"):
            parse_resource_input("", default_registry)

    def test_rejects_dot_only(self, default_registry: WorkspaceRegistry) -> None:
        with pytest.raises(ValueError, match="empty"):
            parse_resource_input(".", default_registry)

    def test_rejects_dot_slash_only(self, default_registry: WorkspaceRegistry) -> None:
        with pytest.raises(ValueError, match="empty"):
            parse_resource_input("./", default_registry)

    def test_rejects_null_byte(self, default_registry: WorkspaceRegistry) -> None:
        with pytest.raises(ValueError, match="null"):
            parse_resource_input("src/\x00main.py", default_registry)

    def test_rejects_overlength(self, default_registry: WorkspaceRegistry) -> None:
        with pytest.raises(ValueError, match="exceeds"):
            parse_resource_input("x" * 1000, default_registry)

    def test_rejects_non_string(self, default_registry: WorkspaceRegistry) -> None:
        with pytest.raises(ValueError, match="string"):
            parse_resource_input(123, default_registry)  # type: ignore[arg-type]

    def test_case_sensitive_workspace_distinguishes_case(self) -> None:
        reg = WorkspaceRegistry(
            [Workspace(name="default", root="/proj", case_sensitive=True)]
        )
        a = parse_resource_input("src/Main.py", reg)
        b = parse_resource_input("src/main.py", reg)
        assert a.path != b.path

    def test_case_insensitive_workspace_folds_case(self) -> None:
        # On a case-insensitive FS, 'Main.py' and 'main.py' are the same file
        # and must not be independently claimable.
        reg = WorkspaceRegistry(
            [Workspace(name="default", root="/proj", case_sensitive=False)]
        )
        keys = {
            parse_resource_input(p, reg).to_uri()
            for p in ("src/Main.py", "src/main.py", "src/MAIN.PY")
        }
        assert keys == {"file://default/src/main.py"}

    def test_case_insensitive_does_not_fold_custom(self) -> None:
        reg = WorkspaceRegistry(
            [Workspace(name="default", root="/proj", case_sensitive=False)]
        )
        # custom names are opaque identifiers, not filesystem paths.
        assert parse_resource_input("custom://Foo", reg).to_uri() == "custom://Foo"

    def test_case_insensitive_absolute_root_case_mismatch(self) -> None:
        reg = WorkspaceRegistry(
            [Workspace(name="default", root="/proj", case_sensitive=False)]
        )
        # Differing case in the absolute path/root still resolves + folds.
        rid = parse_resource_input("/PROJ/Src/App.py", reg)
        assert rid.to_uri() == "file://default/src/app.py"


class TestBareRelativeNoDefault:
    def test_rejects_when_no_default(self, multi_registry: WorkspaceRegistry) -> None:
        with pytest.raises(ValueError, match="default"):
            parse_resource_input("src/main.py", multi_registry)


class TestBareAbsolutePath:
    def test_strips_workspace_root(self) -> None:
        registry = WorkspaceRegistry([Workspace(name="proj", root="/work")])
        rid = parse_resource_input("/work/src/main.py", registry)
        assert rid.workspace == "proj"
        assert rid.path == "src/main.py"

    def test_picks_longest_match(self) -> None:
        registry = WorkspaceRegistry(
            [
                Workspace(name="outer", root="/work"),
                Workspace(name="inner", root="/work/sub"),
            ]
        )
        rid = parse_resource_input("/work/sub/src/main.py", registry)
        assert rid.workspace == "inner"
        assert rid.path == "src/main.py"

    def test_rejects_outside_workspaces(self) -> None:
        registry = WorkspaceRegistry([Workspace(name="proj", root="/work")])
        with pytest.raises(ValueError, match="not inside any configured workspace"):
            parse_resource_input("/elsewhere/file.py", registry)

    def test_rejects_root_only(self) -> None:
        registry = WorkspaceRegistry([Workspace(name="proj", root="/work")])
        with pytest.raises(ValueError, match="workspace root"):
            parse_resource_input("/work", registry)

    def test_windows_absolute(self) -> None:
        registry = WorkspaceRegistry([Workspace(name="proj", root="C:\\work")])
        rid = parse_resource_input("C:\\work\\src\\main.py", registry)
        assert rid.workspace == "proj"
        assert rid.path == "src/main.py"


# ---------------------------------------------------------------------------
# parse_resource_input — file URI form
# ---------------------------------------------------------------------------


class TestFileUri:
    def test_simple(self, default_registry: WorkspaceRegistry) -> None:
        rid = parse_resource_input("file://default/src/main.py", default_registry)
        assert rid.scope == "file"
        assert rid.workspace == "default"
        assert rid.path == "src/main.py"

    def test_unknown_workspace_rejected(
        self, default_registry: WorkspaceRegistry
    ) -> None:
        with pytest.raises(ValueError, match="Unknown workspace"):
            parse_resource_input("file://nonexistent/src/main.py", default_registry)

    def test_missing_path_rejected(self, default_registry: WorkspaceRegistry) -> None:
        with pytest.raises(ValueError, match="non-empty path"):
            parse_resource_input("file://default/", default_registry)

    def test_missing_workspace_rejected(
        self, default_registry: WorkspaceRegistry
    ) -> None:
        with pytest.raises(ValueError, match="workspace name"):
            parse_resource_input("file:///src/main.py", default_registry)

    def test_uri_normalizes_path(self, default_registry: WorkspaceRegistry) -> None:
        rid = parse_resource_input("file://default/.\\src//main.py", default_registry)
        assert rid.path == "src/main.py"

    def test_uri_rejects_dot_dot(self, default_registry: WorkspaceRegistry) -> None:
        with pytest.raises(ValueError, match=r"\.\."):
            parse_resource_input("file://default/../etc/passwd", default_registry)

    def test_uri_with_multiple_workspaces(
        self, multi_registry: WorkspaceRegistry
    ) -> None:
        rid = parse_resource_input("file://b/x/y.py", multi_registry)
        assert rid.workspace == "b"
        assert rid.path == "x/y.py"


# ---------------------------------------------------------------------------
# parse_resource_input — custom URI form
# ---------------------------------------------------------------------------


class TestCustomUri:
    def test_simple(self, default_registry: WorkspaceRegistry) -> None:
        rid = parse_resource_input("custom://task-42", default_registry)
        assert rid.scope == "custom"
        assert rid.workspace is None
        assert rid.path == "task-42"
        assert rid.to_uri() == "custom://task-42"

    def test_arbitrary_chars(self, default_registry: WorkspaceRegistry) -> None:
        rid = parse_resource_input("custom://arb !@# stuff", default_registry)
        assert rid.path == "arb !@# stuff"

    def test_empty_name_rejected(self, default_registry: WorkspaceRegistry) -> None:
        with pytest.raises(ValueError, match="name"):
            parse_resource_input("custom://", default_registry)

    def test_whitespace_only_name_rejected(
        self, default_registry: WorkspaceRegistry
    ) -> None:
        with pytest.raises(ValueError, match="whitespace"):
            parse_resource_input("custom://   ", default_registry)

    def test_unknown_scheme_rejected(self, default_registry: WorkspaceRegistry) -> None:
        with pytest.raises(ValueError, match="scheme"):
            parse_resource_input("http://example.com/x", default_registry)


# ---------------------------------------------------------------------------
# ResourceId
# ---------------------------------------------------------------------------


class TestResourceId:
    def test_file_to_uri(self) -> None:
        rid = ResourceId(scope="file", workspace="proj", path="src/main.py")
        assert rid.to_uri() == "file://proj/src/main.py"

    def test_custom_to_uri(self) -> None:
        rid = ResourceId(scope="custom", workspace=None, path="task-42")
        assert rid.to_uri() == "custom://task-42"

    def test_str_returns_uri(self) -> None:
        rid = ResourceId(scope="file", workspace="proj", path="src/main.py")
        assert str(rid) == "file://proj/src/main.py"

    def test_unknown_scope_to_uri_raises(self) -> None:
        rid = ResourceId(scope="bogus", workspace=None, path="x")
        with pytest.raises(ValueError):
            rid.to_uri()


# ---------------------------------------------------------------------------
# Canonical equivalence — different inputs → same URI
# ---------------------------------------------------------------------------


class TestCanonicalEquivalence:
    """The whole point of the design: equivalent inputs collapse to one URI."""

    def test_dot_slash_and_bare_equivalent(self) -> None:
        registry = WorkspaceRegistry([Workspace(name="default", root="/proj")])
        a = parse_resource_input("./src/main.py", registry).to_uri()
        b = parse_resource_input("src/main.py", registry).to_uri()
        assert a == b

    def test_backslash_and_slash_equivalent(self) -> None:
        registry = WorkspaceRegistry([Workspace(name="default", root="/proj")])
        a = parse_resource_input("src\\main.py", registry).to_uri()
        b = parse_resource_input("src/main.py", registry).to_uri()
        assert a == b

    def test_absolute_and_relative_equivalent_in_workspace(self) -> None:
        registry = WorkspaceRegistry([Workspace(name="proj", root="/work")])
        a = parse_resource_input("/work/src/main.py", registry).to_uri()
        b = parse_resource_input("src/main.py", registry).to_uri()
        assert a == b

    def test_uri_and_bare_equivalent(self) -> None:
        registry = WorkspaceRegistry([Workspace(name="default", root="/proj")])
        a = parse_resource_input("file://default/src/main.py", registry).to_uri()
        b = parse_resource_input("src/main.py", registry).to_uri()
        assert a == b

    def test_double_slash_collapses(self) -> None:
        registry = WorkspaceRegistry([Workspace(name="default", root="/proj")])
        a = parse_resource_input("src//main.py", registry).to_uri()
        b = parse_resource_input("src/main.py", registry).to_uri()
        assert a == b


# ---------------------------------------------------------------------------
# Default workspace name
# ---------------------------------------------------------------------------


class TestDefaultName:
    def test_default_constant(self) -> None:
        assert DEFAULT_WORKSPACE_NAME == "default"
