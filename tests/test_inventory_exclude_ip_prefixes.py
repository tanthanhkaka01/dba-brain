"""Which servers an inventory page leaves out is the operator's decision, not the library's.

`db_ops/lib/inventory_render.py` carried `EXCLUDE_IP_PREFIXES = ("198.51.100.",)` — one estate's
management subnet, written into the rendering code. Every inventory page rendered by anyone,
anywhere, silently dropped every server in that range, and nothing on the page said so. A reader
counting servers would have got the wrong number and had no way to notice.

Hiding a machine is a statement about *an* estate, so it is configuration by definition. The
library's default is now to hide nothing, and the list arrives as an argument that the apps read
from `reports_config.json`.

These tests pin both halves: the library shows everything unless told, and the reader returns
nothing at all when the key is absent — because an unconfigured filter must not become an
accidental one.
"""

from __future__ import annotations

import json
from pathlib import Path

from db_ops.common.data_sources import inventory_exclude_ip_prefixes
from db_ops.lib.inventory_render import EXCLUDE_IP_PREFIXES, _render_markdown
from db_ops.reports.inventory_report import build_models

INVENTORY = {
    "servers": [
        {"server_id": "A-10-0-0-1", "ip": "10.0.0.1", "databases": [{"db_type": "sqlserver"}]},
        {"server_id": "B-10-0-0-2", "ip": "10.0.0.2", "databases": [{"db_type": "oracle"}]},
        {"server_id": "C-198-51-100-9", "ip": "198.51.100.9", "databases": [{"db_type": "sqlserver"}]},
    ]
}


def _write_reports_config(tmp_path: Path, payload: dict) -> Path:
    data_dir = tmp_path / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    (data_dir / "reports_config.json").write_text(json.dumps(payload), encoding="utf-8")
    return data_dir


def test_the_library_hides_nothing_by_default() -> None:
    assert EXCLUDE_IP_PREFIXES == ()


def test_an_unconfigured_filter_does_not_become_an_accidental_one(tmp_path: Path) -> None:
    data_dir = _write_reports_config(tmp_path, {"report_base_url": "http://example.invalid/"})

    assert inventory_exclude_ip_prefixes(data_dir) == ()


def test_a_missing_reports_config_hides_nothing(tmp_path: Path) -> None:
    """A first run has no config yet, and must still render every server it was one estate management subnetgiven."""
    assert inventory_exclude_ip_prefixes(tmp_path / "does-not-exist") == ()


def test_the_configured_prefixes_are_what_the_operator_wrote(tmp_path: Path) -> None:
    data_dir = _write_reports_config(
        tmp_path, {"inventory_exclude_ip_prefixes": ["10.0.0.", " 172.31. ", ""]}
    )

    assert inventory_exclude_ip_prefixes(data_dir) == ("10.0.0.", "172.31.")


def test_a_single_prefix_written_as_a_string_still_works(tmp_path: Path) -> None:
    """Config is edited by hand, and a one-element list is exactly what gets typed as a string."""
    data_dir = _write_reports_config(tmp_path, {"inventory_exclude_ip_prefixes": "10.0.0."})

    assert inventory_exclude_ip_prefixes(data_dir) == ("10.0.0.",)


def test_the_summary_page_shows_every_server_when_nothing_is_excluded() -> None:
    rendered = _render_markdown(INVENTORY, "2026-08-21")

    assert "198.51.100.9" in rendered or "C-198-51-100-9" in rendered


def test_the_summary_page_leaves_out_what_the_operator_excluded() -> None:
    rendered = _render_markdown(INVENTORY, "2026-08-21", ("198.51.100.",))

    assert "198.51.100.9" not in rendered
    assert "C-198-51-100-9" not in rendered


def test_the_report_models_carry_every_server_when_nothing_is_excluded() -> None:
    _scope, models = build_models(INVENTORY)

    assert len(models) == 3


def test_the_report_models_drop_the_excluded_range() -> None:
    _scope, models = build_models(INVENTORY, exclude_ip_prefixes=("198.51.100.",))

    assert len(models) == 2
    assert all("198.51.100." not in str(model) for model in models)
