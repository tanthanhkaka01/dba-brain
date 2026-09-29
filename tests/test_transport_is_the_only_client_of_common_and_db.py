"""`transport` is the one client of `common.cli` and `db.cli` (R38-R40, docs/15_transport.md).

`lib` builds a command and reads the answer; `transport` starts the process between the two. Until
0.24.0 `lib/common_cli.py` started it itself, which put an operation inside the layer everything
imports (R07). Moving the `subprocess.run` to another file in `lib` would only have renamed the
breach, and moving it into `common` would have made every app import `common` (R03) and `common`
start a process (R05) - so the launch has a layer of its own, and these guards keep it the only
one, and keep it small.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest


DB_OPS = Path(__file__).resolve().parents[1] / "db_ops"
TRANSPORT = DB_OPS / "transport"
#: The two CLIs every component is meant to reach, and only through `transport`.
REACHED_THROUGH_TRANSPORT = ("db_ops.common.cli", "db_ops.db.cli")
#: How a process is started, by module. `os.system` and friends count: a launch by another name is
#: still a launch.
_LAUNCHERS = {"subprocess"}
_OS_LAUNCHES = {"system", "popen", "spawnl", "spawnv", "execv", "execl"}


def _files(root: Path) -> list[Path]:
    return sorted(p for p in root.rglob("*.py") if "__pycache__" not in p.parts)


def _relative(path: Path) -> str:
    return path.relative_to(DB_OPS).as_posix()


def _tree(path: Path) -> ast.AST:
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def _starts_processes(tree: ast.AST) -> bool:
    for node in ast.walk(tree):
        if isinstance(node, ast.Import) and any(a.name.split(".")[0] in _LAUNCHERS for a in node.names):
            return True
        if isinstance(node, ast.ImportFrom) and (node.module or "").split(".")[0] in _LAUNCHERS:
            return True
        if (isinstance(node, ast.Attribute) and node.attr in _OS_LAUNCHES
                and getattr(node.value, "id", "") == "os"):
            return True
    return False


def _names_a_reached_cli(tree: ast.AST) -> bool:
    return any(isinstance(node, ast.Constant) and node.value in REACHED_THROUGH_TRANSPORT
               for node in ast.walk(tree))


def _db_ops_imports(tree: ast.AST) -> set[str]:
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found |= {alias.name for alias in node.names}
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            found.add(node.module)
            found |= {f"{node.module}.{alias.name}" for alias in node.names}
    return {name for name in found if name.split(".")[0] == "db_ops"}


def test_the_layer_exists():
    assert (TRANSPORT / "process.py").is_file() and (TRANSPORT / "common_cli.py").is_file()


@pytest.mark.parametrize("path", [p for p in _files(DB_OPS) if TRANSPORT not in p.parents], ids=_relative)
def test_nothing_outside_transport_starts_common_or_db_cli(path):
    """R38. Naming the module is allowed - `lib` builds the command - starting it is not."""
    tree = _tree(path)
    assert not (_starts_processes(tree) and _names_a_reached_cli(tree)), (
        f"{_relative(path)} starts {' / '.join(REACHED_THROUGH_TRANSPORT)} itself. Call it through "
        "`db_ops.transport.common_cli` (run / run_allowing_failure / spawn).")


@pytest.mark.parametrize("path", _files(TRANSPORT), ids=_relative)
def test_transport_imports_only_lib(path):
    """R39. Anything it imported would come along into every process that reaches `common`."""
    outside = sorted(name for name in _db_ops_imports(_tree(path))
                     if not name.startswith(("db_ops.lib", "db_ops.transport")))
    assert not outside, f"{_relative(path)} imports {outside}; transport imports only lib"


#: Every shared layer but `transport` itself. `db` joined on 2026-09-28 (the operator: *db has no
#: reason to import transport*): its Telegram writer started `db.cli` to reach the insert it then
#: called in-process anyway, whenever the process failed.
SHARED_LAYERS_BELOW_THE_APPS = ("lib", "common", "db", "logging_ops")


@pytest.mark.parametrize("path", [path for layer in SHARED_LAYERS_BELOW_THE_APPS
                                  for path in _files(DB_OPS / layer)], ids=_relative)
def test_no_shared_layer_imports_transport(path):
    """R39. `common` is what transport starts, `lib` what it builds from, `db` and `logging_ops` what
    apps import beside it - none of them reaches up into the layer that starts processes; only an
    app does."""
    reached = sorted(name for name in _db_ops_imports(_tree(path)) if name.startswith("db_ops.transport"))
    assert not reached, f"{_relative(path)} imports {reached}"


def test_one_function_in_transport_starts_a_process():
    """R40. One place to read to know how every call to `common` behaves."""
    starting = [_relative(p) for p in _files(TRANSPORT) if _starts_processes(_tree(p))]
    assert starting == ["transport/process.py"], starting
    tree = _tree(TRANSPORT / "process.py")
    calls = [node for node in ast.walk(tree) if isinstance(node, ast.Call)
             and isinstance(node.func, ast.Attribute) and getattr(node.func.value, "id", "") == "subprocess"
             and node.func.attr in {"run", "Popen", "call", "check_call", "check_output"}]
    assert len(calls) == 1, f"transport/process.py starts a process in {len(calls)} places"
    owner = next(node for node in ast.walk(tree) if isinstance(node, ast.FunctionDef)
                 and calls[0] in list(ast.walk(node)))
    assert owner.name == "execute"
