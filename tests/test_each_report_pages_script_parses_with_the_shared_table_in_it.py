"""Each report page's script parses with the shared backup table pasted into it (2026-11-11).

Why it matters: ``backup_files.js`` is pasted into two pages - ``server-metrics.html`` and
``backup-restore.html``. On 2026-11-11 it gained a ``const day``; the server page already declared
one, the browser refused the whole script ("Identifier 'day' has already been declared") and
server-metrics.html stayed on "Loading..." for every server. The table's own tests render it alone,
where nothing clashes. So each page's script is put together here exactly as the page is - the
placeholders filled - and handed to a real JavaScript engine to parse.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

from dbabrain.reports import backup_restore_report, server_report

NODE = shutil.which("node")
TEMPLATES = Path(server_report.__file__).resolve().parent / "templates"
SHARED = TEMPLATES / "backup_files.js"


def _scripts(page: Path) -> str:
    html = page.read_text(encoding="utf-8").replace("__BACKUP_FILES_JS__", SHARED.read_text(encoding="utf-8"))
    body = "\n;\n".join(re.findall(r"<script>(.*?)</script>", html, flags=re.S))
    # Every other placeholder is a value the page is rendered with - a literal is enough to parse.
    return re.sub(r"__[A-Z][A-Z0-9_]*__", "0", body)


@pytest.mark.skipif(not NODE, reason="a JavaScript engine parses the page's script")
@pytest.mark.parametrize("page", ["server_report.html", "backup_restore_report.html"])
def test_the_pages_script_parses_with_the_shared_table_pasted_in(page, tmp_path):
    script = tmp_path / "page.js"
    script.write_text(_scripts(TEMPLATES / page), encoding="utf-8")
    done = subprocess.run([NODE, "--check", str(script)], capture_output=True, text=True, timeout=60)
    assert done.returncode == 0, done.stderr[-1500:]


def test_the_shared_table_declares_no_name_either_page_declares():
    shared = SHARED.read_text(encoding="utf-8")
    # Top level only - written at the start of a line; a name inside a function is its own.
    declared = re.compile(r"(?:^|\n)(?:const|let|var|function)\s+([A-Za-z_$][\w$]*)")
    ours = set(declared.findall(shared))
    for page in ("server_report.html", "backup_restore_report.html"):
        html = (TEMPLATES / page).read_text(encoding="utf-8")
        theirs = set(declared.findall("\n".join(re.findall(r"<script>(.*?)</script>", html, flags=re.S))))
        assert not ours & theirs, f"{page} and backup_files.js both declare {sorted(ours & theirs)}"


def test_both_pages_still_paste_the_shared_table():
    assert server_report.BACKUP_FILES_JS == SHARED and backup_restore_report.BACKUP_FILES_JS == SHARED
