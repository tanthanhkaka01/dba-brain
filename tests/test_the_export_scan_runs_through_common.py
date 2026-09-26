"""The export's identifier scan runs `common.cli check-identifiers`, and keeps its three outcomes apart.

`control` imported `identifier_scan` to scan the exported tree (rules R03). Through the CLI every
failure is a failed answer, and two of them mean opposite things: a scan that *refused* - nothing to
search for, or nothing to read - leaves the copy written and unverified, which the export says as
SKIPPED; a scan that *broke* must stop the export, exactly as the exception did in-process. The
answer's `refused` is what tells them apart.
"""

from __future__ import annotations

import pytest

from db_ops.control import export_public
from db_ops.transport.common_cli import CommonCliError


def _answer(monkeypatch, success, data, error=""):
    calls = []

    def run_allowing_failure(command, request, **_kwargs):
        calls.append((command, request))
        return success, data, error

    monkeypatch.setattr("db_ops.transport.common_cli.run_allowing_failure", run_allowing_failure)
    return calls


def test_a_clean_scan_returns_its_report_for_the_whole_copied_tree(monkeypatch, tmp_path):
    (tmp_path / "db_ops").mkdir()
    (tmp_path / ".git").mkdir()
    calls = _answer(monkeypatch, True, {"hits": 0, "files_with_findings": 0})
    assert export_public.scan_exported_tree(tmp_path)["hits"] == 0
    assert calls == [("check-identifiers", {"root": str(tmp_path), "paths": ["db_ops"]})]


def test_a_refused_scan_is_skipped_not_passed(monkeypatch, tmp_path):
    _answer(monkeypatch, False, {"refused": True}, "no identifiers to search for")
    with pytest.raises(export_public.ScanRefused, match="no identifiers"):
        export_public.scan_exported_tree(tmp_path)


def test_a_scan_that_broke_stops_the_export(monkeypatch, tmp_path):
    _answer(monkeypatch, False, {}, "KeyError: 'files'")
    with pytest.raises(CommonCliError, match="KeyError"):
        export_public.scan_exported_tree(tmp_path)
