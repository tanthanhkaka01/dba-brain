"""No module works out the project root from its own file path. One does, and it is named here.

``Path(__file__).resolve().parents[2]`` answers "where is my configuration" with "where is my
code". Those are the same directory in exactly two layouts — a dev checkout and the container —
and different in every installed copy, where the answer becomes ``site-packages/data``. On
2026-08-21 a wheel built from this tree proved it: the package imported cleanly and could not
have found a single config file.

That was fixed once in ``db_ops/lib/paths.py``, which now resolves the root in a stated order
(``DB_OPS_HOME`` → a working directory carrying ``data/`` or ``config.json`` → the package's own
location). The fix is worthless if the idiom grows back somewhere else, and it had already grown
back **eleven** times before this guard existed — including in
``telegram/command_processor.py``, which imported ``TOOL_ROOT`` on one line and re-derived the
same value two lines below it.

So the rule is mechanical: the depth of this package on disk is stated in one file. Everything
else imports ``TOOL_ROOT``, ``PACKAGE_ROOT`` or ``DEFAULT_DATA_DIR`` from it.

A module that genuinely needs its own directory — a package's own data file, say — is not what
this guards: ``Path(__file__).parent`` is untouched. What is refused is climbing out of the
package to guess where the *project* is.
"""

from __future__ import annotations

import re
from pathlib import Path

PACKAGE_ROOT = Path(__file__).resolve().parents[1] / "db_ops"

#: The one file allowed to state how deep the package sits, relative to ``db_ops/``.
#: A second entry here needs the same argument this one had, made as hard.
ALLOWED: dict[str, str] = {
    "lib/paths.py": (
        "the single definition: PACKAGE_ROOT, plus the resolution order that no longer depends "
        "on it"
    ),
}

#: Climbing two or more levels out of a module's own file is how a project root gets guessed.
#: One level (``parents[0]`` / ``.parent``) is a module's own directory and is nobody's business
#: but that module's.
SELF_DERIVED_ROOT = re.compile(r"Path\(__file__\)\.resolve\(\)\.parents\[([2-9])\]")


def _offenders() -> list[tuple[str, int, str]]:
    found: list[tuple[str, int, str]] = []
    for path in sorted(PACKAGE_ROOT.rglob("*.py")):
        relative = path.relative_to(PACKAGE_ROOT).as_posix()
        if relative in ALLOWED:
            continue
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            if SELF_DERIVED_ROOT.search(line):
                found.append((relative, number, line.strip()))
    return found


def test_the_project_root_is_derived_from_the_package_location_in_one_file_only() -> None:
    offenders = _offenders()
    assert not offenders, (
        "These modules work out the project root from their own file path, which resolves to "
        "site-packages once the package is installed. Import TOOL_ROOT / PACKAGE_ROOT / "
        "DEFAULT_DATA_DIR from db_ops.lib.paths instead:\n"
        + "\n".join(f"  {name}:{number}: {line}" for name, number, line in offenders)
    )


def test_the_allowed_file_still_exists_and_still_needs_its_exception() -> None:
    """An exception that stops being used must be deleted, not left as folklore."""
    for relative, reason in ALLOWED.items():
        path = PACKAGE_ROOT / relative
        assert path.exists(), f"ALLOWED names a file that is gone: {relative}"
        text = path.read_text(encoding="utf-8")
        assert SELF_DERIVED_ROOT.search(text), (
            f"{relative} no longer derives a root from __file__, so it should lose its "
            f"exception ({reason})"
        )
