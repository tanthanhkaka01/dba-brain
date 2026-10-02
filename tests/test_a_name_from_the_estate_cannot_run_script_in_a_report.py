"""A value collected from a monitored server cannot close the report's `<script>` block.

Review 0.25.0, B7.1: the reports embedded estate data - database, login, job and linked-server
names, error text - with a plain `json.dumps`, which escapes nothing an HTML parser cares about. A
database named `</script><script>...` on any monitored instance ran script in every browser that
opened the report, on the console's own origin.
"""

from __future__ import annotations

import json

from db_ops.lib.html_json import json_for_html
from db_ops.reports import inventory_report

EVIL = "</script><script>fetch('/db_ops/api/config')</script><!--"


def test_the_helper_escapes_what_can_start_a_tag_and_stays_json():
    text = json_for_html({"name": EVIL, "sep": "a b"})

    assert "<" not in text and ">" not in text and "&" not in text and " " not in text
    assert json.loads(text) == {"name": EVIL, "sep": "a b"}


def test_the_inventory_page_carries_no_closing_script_tag_from_the_data():
    page = inventory_report.render_html(
        {"name": EVIL}, [{"name": EVIL, "server": EVIL}], [{"title": EVIL}], "2026-10-01",
        linked_servers=[{"name": EVIL}])

    assert EVIL not in page
    assert "</script><script>fetch" not in page


def test_the_console_uses_the_same_helper():
    from db_ops.webhost.pages import json_script

    assert json_script({"v": EVIL}) == json_for_html({"v": EVIL}, default=str)
