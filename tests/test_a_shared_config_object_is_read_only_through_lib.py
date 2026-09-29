"""A shared config object's fields are read by `lib`'s parser, and nowhere else (R20).

`time_window` is parsed once, in `lib/time_window.py`: its legacy field names, a blank that means
unset, the manual-only `-1`, the weekday set and every refusal are decided there. A module that
reads `repeat_interval` straight out of the JSON gets none of that - a legacy name is invisible, and
a window the scheduler refuses is shown as if it ran. The only guard R20 had was the
duplicate-name check, which cannot see a copy that is not a function: 24 such reads in 7 files when
this was written (2026-09-28), all moved behind `lib.time_window.window_of` the same day, and the
registrars' own validator of the same rules (`config_admin.normalize_time_window`) now asks the
parser too.

So this reads every module outside `lib` for a read of a `time_window` field **on a window** - a
`window.get("from_hour")`, a `window["repeat_interval"]`, a `(x.get("time_window") or {})
.get(...)`. A dict the module built itself under the same key name (an answer's
`item["repeat_interval"]`) is not a window and is not counted; neither is a write.
"""

from __future__ import annotations

import ast
from pathlib import Path

DB_OPS = Path(__file__).resolve().parents[1] / "db_ops"

#: The fields only `lib.time_window` interprets.
TIME_WINDOW_FIELDS = frozenset({
    "from_year", "to_year", "from_month", "to_month", "from_day", "to_day", "from_hour", "to_hour",
    "from_minute", "to_minute", "repeat_interval", "retry_interval", "timeout", "weekdays"})

#: Empty since 2026-09-28 (24 when measured). A read here is a parse outside `lib`.
READS_LEFT: dict[str, int] = {}


def _is_a_window(node: ast.expr) -> bool:
    """The object read from is a time_window: named so, or fetched by that key."""
    source = ast.unparse(node)
    return "window" in source.lower()


def _reads(path: Path) -> list[str]:
    found = []
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"), filename=str(path))):
        if (isinstance(node, ast.Subscript) and isinstance(node.ctx, ast.Load)
                and isinstance(node.slice, ast.Constant) and node.slice.value in TIME_WINDOW_FIELDS
                and _is_a_window(node.value)):
            found.append(f"{node.lineno}: {ast.unparse(node)}")
        elif (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
              and node.func.attr == "get" and node.args and isinstance(node.args[0], ast.Constant)
              and node.args[0].value in TIME_WINDOW_FIELDS and _is_a_window(node.func.value)):
            found.append(f"{node.lineno}: {ast.unparse(node)}")
    return found


def _measured() -> dict[str, list[str]]:
    found = {}
    for path in sorted(DB_OPS.rglob("*.py")):
        relative = path.relative_to(DB_OPS)
        if "__pycache__" in relative.parts or relative.parts[0] == "lib":
            continue
        reads = _reads(path)
        if reads:
            found[relative.as_posix()] = reads
    return found


def test_no_module_outside_lib_reads_a_time_window_field_it_did_not_already():
    grown = {name: reads for name, reads in _measured().items() if len(reads) > READS_LEFT.get(name, 0)}
    assert not grown, (
        f"these read time_window fields straight from the JSON: {grown}. Read the window with "
        "lib.time_window.window_of (or parse_time_window_config) and use its attributes (rules R20).")


def test_the_reads_left_only_shrink():
    measured = _measured()
    stale = {name: (count, len(measured.get(name, []))) for name, count in READS_LEFT.items()
             if len(measured.get(name, [])) < count}
    assert not stale, f"fewer reads now - lower READS_LEFT (listed, measured): {stale}"


def test_the_guard_sees_what_it_is_for(tmp_path):
    """A guard that finds nothing must be shown able to find something."""
    sample = tmp_path / "sample.py"
    sample.write_text(
        "window = command.get('time_window') or {}\n"
        "a = window.get('repeat_interval')\n"
        "b = (item.get('time_window') or {}).get('from_hour')\n"
        "c = window['weekdays']\n"
        "d = item['repeat_interval']\n"          # an answer the module built - not a window
        "window['timeout'] = 5\n",               # a write - not a read
        encoding="utf-8")
    assert len(_reads(sample)) == 3
