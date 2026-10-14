"""Every prompt a web job can reach goes through the bridge salmon web installs, and none is bound where it misses."""

import ast
from collections.abc import Iterator
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent / "src" / "salmon"
# Replaced on the asyncclick module itself: reachable only as click.<name>.
_CLICK_NAMES = {"prompt", "confirm", "edit", "echo", "secho"}
# salmon's own, replaced on these modules only: install_interaction_patches lists them.
_BOUND_WHERE_REPLACED = {
    "view_spectrals": {"uploader/spectrals.py"},
    "flush_stdin": {"uploader/spectrals.py"},
    "prompt_async": {"uploader/spectrals.py"},
    "handle_spectrals_upload_and_deletion": {"uploader/spectrals.py", "uploader/__init__.py", "commands.py"},
}
# The one module whose input() salmon web shadows with a module global.
_INPUT_MODULES = {"tagger/pre_data.py"}


def _modules() -> Iterator[tuple[str, ast.Module]]:
    for path in sorted(SRC.rglob("*.py")):
        name = path.relative_to(SRC).as_posix()
        if not name.startswith("webui/"):
            yield name, ast.parse(path.read_text(encoding="utf-8"))


def _imports(tree: ast.Module) -> Iterator[ast.ImportFrom]:
    return (node for node in ast.walk(tree) if isinstance(node, ast.ImportFrom))


def test_no_module_binds_a_click_prompt_function_by_name() -> None:
    found = [
        (name, alias.name)
        for name, tree in _modules()
        for node in _imports(tree)
        if node.module in ("asyncclick", "click")
        for alias in node.names
        if alias.name in _CLICK_NAMES
    ]

    assert found == []


def test_salmons_own_prompting_functions_are_bound_only_where_salmon_web_replaces_them() -> None:
    found = [
        (name, alias.name)
        for name, tree in _modules()
        for node in _imports(tree)
        for alias in node.names
        if alias.name in _BOUND_WHERE_REPLACED and name not in _BOUND_WHERE_REPLACED[alias.name]
    ]

    assert found == []


def test_input_is_called_only_where_salmon_web_shadows_it() -> None:
    found = [
        name
        for name, tree in _modules()
        if name not in _INPUT_MODULES
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "input"
    ]

    assert found == []
