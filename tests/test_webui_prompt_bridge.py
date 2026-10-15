"""Every prompt a web job can reach goes through the bridge salmon web installs, and none is bound where it misses."""

import ast
from collections.abc import Iterator
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent / "src" / "salmon"
# Replaced on the asyncclick module itself: reachable only as click.<name>.
_CLICK_NAMES = {"prompt", "confirm", "edit", "echo", "secho"}
# salmon's own, imported only by these modules, where install_interaction_patches replaces each but prompt_async:
# that is awaited only under view_spectrals, which it replaces.
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


def _top_level_names(tree: ast.Module) -> Iterator[str]:
    for node in tree.body:
        if isinstance(node, ast.ImportFrom):
            yield from (alias.name for alias in node.names)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            yield node.name


def _callers(tree: ast.Module, callee: str) -> Iterator[str]:
    for fn in ast.walk(tree):
        if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)) and any(
            isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == callee
            for node in ast.walk(fn)
        ):
            yield fn.name


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


def test_prompt_async_is_awaited_only_under_view_spectrals() -> None:
    modules = dict(_modules())
    awaits = {(name, fn) for name, tree in modules.items() for fn in _callers(tree, "prompt_async")}
    opens = {(name, fn) for name, tree in modules.items() for fn in _callers(tree, "_open_specs_in_web_server")}

    assert awaits == {("uploader/spectrals.py", "_open_specs_in_web_server")}
    assert opens == {("uploader/spectrals.py", "view_spectrals")}


def test_the_scan_finds_each_guarded_name_where_it_is_allowed() -> None:
    modules = dict(_modules())
    held = {(name, held_name) for name, tree in modules.items() for held_name in _top_level_names(tree)}
    calls_input = {
        name
        for name, tree in modules.items()
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "input"
    }

    assert {(module, fn) for fn, allowed in _BOUND_WHERE_REPLACED.items() for module in allowed} <= held
    assert calls_input == _INPUT_MODULES
