"""The static types of every ``common.cli`` request and answer are rendered from the reference, not written.

The 2026-10-02 assessment (``audits/20261002_audit_typed_requests_and_errors.md`` section 2) chose
one description of a request: the reference (rules R16). ``lib/cli_types.py`` (a ``TypedDict`` per
``input_*`` / ``output_*`` entry) and ``transport/common_cli.pyi`` (an overload of ``run`` per
command, so ``run("restore-full", ...)`` returns ``RestoreStepAnswer``) are rendered from it by
``lib.request_types``. A field added to the reference and not to the types - or the other way round -
is two descriptions disagreeing, which is what this holds down.
"""

from __future__ import annotations

from pathlib import Path

from db_ops.control import cli as control_cli
from db_ops.lib import request_types, shared_objects

PACKAGE = Path(__file__).resolve().parents[1] / "db_ops"
REFERENCE = shared_objects.load(PACKAGE / "common" / "catalogue")


def test_the_request_and_answer_types_are_the_reference_rendered():
    committed = (PACKAGE / "lib" / "cli_types.py").read_text(encoding="utf-8")
    assert committed == request_types.render(REFERENCE), (
        "lib/cli_types.py differs from the reference: python -m db_ops.control.cli request-types --write")


def test_the_transport_stub_is_the_reference_rendered():
    committed = (PACKAGE / "transport" / "common_cli.pyi").read_text(encoding="utf-8")
    assert committed == request_types.render_transport_stub(REFERENCE), (
        "transport/common_cli.pyi differs from the reference: python -m db_ops.control.cli request-types --write")


def test_every_entry_becomes_a_type_and_every_command_with_both_is_typed():
    from db_ops.lib import cli_types

    entries = [e for e in REFERENCE if e.get("kind") in ("input", "output")
               and str(e.get("object", "")).startswith(("input_", "output_"))]
    assert len(cli_types.__all__) == len(entries)
    stub = (PACKAGE / "transport" / "common_cli.pyi").read_text(encoding="utf-8")
    assert "def run(command: Literal['restore-full'], request: t.RestoreStepRequest" in stub
    assert "-> t.RestoreStepAnswer: ..." in stub


def test_a_name_is_the_entry_s_and_its_kind():
    assert request_types.type_name("input_run_sql") == "RunSqlRequest"
    assert request_types.type_name("output_run_sql") == "RunSqlAnswer"


def test_the_command_that_writes_them_says_they_are_current(capsys):
    assert control_cli.main(["request-types"]) == 0
    assert "current" in capsys.readouterr().out
