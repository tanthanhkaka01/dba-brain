"""One shape for every backup_restore event, checked at the call sites rather than trusted.

The rule: a **run** is bracketed by `START` and one terminal event (`END`, `ERROR`, `TIMEOUT`)
under the command that names the operation, and everything inside it is a **phase** of that same
command — `<STEP>_START` / `<STEP>_DONE` / `<STEP>_ERROR`. A step is never a command of its own.

Four conventions used to coexist, and the reporting was incoherent because of it:

* `command="restore-latest.certificate"` with `phase="START"` — a sub-step wearing the run's
  symbols, so a certificate import looked exactly like a restore starting;
* `command="backup.timeout"` — a phase encoded as a command suffix;
* `prune` emitting only the terminal event, so "how many prunes ran" had no answer;
* `restore-by-id` emitting nothing at all, so the same restore reported four events through the
  scheduler and zero from an operator's shell.

None of those were visible as bugs; each looked locally reasonable. This test makes the shape
checkable in one place so the next producer inherits it instead of inventing a fifth convention.

Why the type map needs no entry per step: `START`/`END` are registered in
`telegram_queue._PHASE_TYPES` and render `started`/`success`, while `COPY_START` deliberately is
not — it falls through to the level and renders plain. Steps are quiet *because* they are
unregistered, which is why adding one is a one-line change.
"""

from __future__ import annotations

import ast
import pathlib

import pytest

APP = pathlib.Path(__file__).resolve().parents[1] / "db_ops" / "backup_restore"

#: Phases that bracket a run. Anything else must be a step phase.
RUN_PHASES = {"START", "END", "ERROR", "TIMEOUT"}
#: The outcomes a step phase may end with.
STEP_OUTCOMES = ("_START", "_DONE", "_ERROR", "_SKIP")


def _strings(node: ast.AST) -> list[str]:
    """The string values an expression can evaluate to, ignoring anything dynamic.

    A conditional (``"END" if ok else "ERROR"``) contributes both branches: both are phases a
    producer really can emit, and both have to satisfy the shape.
    """
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return [node.value]
    if isinstance(node, ast.IfExp):
        return _strings(node.body) + _strings(node.orelse)
    return []


def _wrapper_names(tree: ast.AST) -> set[str]:
    """Local helpers that emit on the caller's behalf (``_rbid_event``, ``announce``, ``_say``).

    Without following one level of indirection this check is blind exactly where it matters:
    ``restore-by-id`` emits through a closure, which is *why* nobody noticed it emitted nothing.
    """
    names: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for inner in ast.walk(node):
            if isinstance(inner, ast.Call) and (
                getattr(inner.func, "id", None) == "emit_backup_restore_event"
                or getattr(inner.func, "attr", None) == "emit_backup_restore_event"
            ):
                names.add(node.name)
                break
    return names


def _emissions() -> list[tuple[str, int, dict[str, str]]]:
    out: list[tuple[str, int, dict[str, str]]] = []
    for path in sorted(APP.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        wrappers = _wrapper_names(tree)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            name = getattr(node.func, "id", None) or getattr(node.func, "attr", None)
            if name == "emit_backup_restore_event":
                found: dict[str, str] = {}
                for keyword in node.keywords:
                    if keyword.arg in {"command", "phase"}:
                        values = _strings(keyword.value)
                        for value in values:
                            out.append((path.name, node.lineno, {**found, keyword.arg: value}))
                        if values:
                            found[keyword.arg] = values[0]
                out.append((path.name, node.lineno, found))
            elif name in wrappers and node.args:
                # A wrapper call: its first positional argument is the phase it reports.
                for phase in _strings(node.args[0]):
                    out.append((path.name, node.lineno, {"phase": phase, "via": name}))
    return out


def test_there_are_emissions_to_check():
    """A silent failure mode of this test: a rename makes it scan nothing and pass."""
    assert len(_emissions()) >= 10


def test_no_step_is_encoded_as_a_command_suffix():
    """`backup.timeout` and `restore-latest.certificate` were steps pretending to be runs.

    A dotted command also splits the id resolution: `run_id_key` reads the head before the dot, so
    the suffix was carried purely to be read by a human, in the one field an operator filters on.
    """
    offenders = [
        f"{file}:{line} command={kwargs['command']!r}"
        for file, line, kwargs in _emissions()
        if "." in kwargs.get("command", "")
    ]
    assert offenders == [], "a step is a phase, never a command suffix: " + "; ".join(offenders)


def test_every_phase_is_either_a_run_bracket_or_a_step():
    """`COPY_START` is fine, `COPY` and `copy-start` are not - the suffix is what decides how the
    message renders, so an unrecognised shape would silently take the level's symbol."""
    offenders = []
    for file, line, kwargs in _emissions():
        phase = kwargs.get("phase")
        if phase is None:
            continue
        if phase in RUN_PHASES or phase.endswith(STEP_OUTCOMES):
            continue
        offenders.append(f"{file}:{line} phase={phase!r}")
    assert offenders == [], "phase must be START/END/ERROR/TIMEOUT or <STEP>_<OUTCOME>: " \
                            + "; ".join(offenders)


@pytest.mark.parametrize("module", ["backup.py", "prune.py", "cli.py"])
def test_every_module_that_runs_something_brackets_it(module):
    """Each of these reported half a run or none of it.

    `prune.py` emitted only its terminal event, so "how many prunes ran" had no answer. `cli.py`'s
    `restore-by-id` branch emitted nothing at all — it returns before the block that brackets every
    other subcommand — so a restore an operator ran by hand was completely silent while the same
    restore through the scheduler reported four events. Both are the paths a human drives, which is
    when the reporting matters most.

    Checked per module rather than per command because the phase and the command do not always
    reach `emit_backup_restore_event` in the same call: a closure forwards the phase and binds the
    command once.
    """
    phases = {kwargs.get("phase") for file, _l, kwargs in _emissions() if file == module}
    assert "START" in phases, f"{module} does not announce that a run started"
    assert phases & {"END", "ERROR"}, f"{module} does not announce how a run ended"
