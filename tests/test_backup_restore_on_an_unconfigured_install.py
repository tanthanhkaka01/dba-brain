""""Not configured" is a state, not a failure — and the backup/restore app was the exception.

Reported on 2026-09-11 from a fresh install where the operator had just switched the backup/restore
command on:

    APP-BACKUP-RESTORE: error at 2026-09-11T03:58:20Z, still failing (1/1 failed in 24h)
        ERROR: 'prod_backup_share'

That string is the whole of the error text, and it is a dict key. It names neither the file that is
missing, nor the app that wanted it, nor what to put in it. `load_restore_configs` fell through to
parsing `RESTORE_PARSER_DEFAULTS` as if it were a restore entry, and the first required field it
read raised `KeyError`.

Two things were wrong and they compound:

* **The loader treated "nothing declared" as "one entry, malformed".** An install with no restore
  configured has no restore entries — an empty list, which every caller already handles.
* **`init` wrote no `restore_config.json` at all**, while `data/data_files.json` listed it as one
  of the toolkit's files. So an operator reading the manifest went looking for a file that was
  never created, and the only signal was a `KeyError` once a day.
"""

from __future__ import annotations

import json

import pytest

from db_ops import scaffold
from db_ops.backup_restore import backup as backup_module
from db_ops.backup_restore import config as config_module
from db_ops.backup_restore import restore_script as script_module
from db_ops.backup_restore.backup import load_backup_jobs
from db_ops.backup_restore.config import load_restore_configs, parse_restore_config
from db_ops.backup_restore.restore_script import load_script_restores


@pytest.fixture
def empty_root(tmp_path, monkeypatch):
    """A tool root with a `data/` folder and nothing in it.

    `DEFAULT_RESTORE_CONFIG_PATH` is resolved once at import from the tool root, so `chdir` alone
    leaves the loader reading THIS repository's estate config — which is how a test for "nothing
    configured" quietly ran against fourteen real restore entries.
    """
    data = tmp_path / "data"
    data.mkdir()
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(config_module, "DEFAULT_RESTORE_CONFIG_PATH", data / "restore_config.json")
    monkeypatch.setattr(backup_module, "DEFAULT_RESTORE_CONFIG_PATH", data / "restore_config.json",
                        raising=False)
    # `restore_script` imported the constant by value, so patching the config module alone left
    # the script-driven loader reading this repository's own fourteen entries.
    monkeypatch.setattr(script_module, "DEFAULT_RESTORE_CONFIG_PATH", data / "restore_config.json",
                        raising=False)
    return tmp_path


def test_an_incomplete_entry_names_its_missing_fields_instead_of_quoting_a_dict_key():
    """The other half of the same defect, closed 2026-09-14.

    The 2026-09-11 fix covered "nothing is configured": that now returns an empty list. An entry
    that *exists* and is incomplete still went straight into the constructor, where six fields are
    read by subscript, and the first one missing was again the entire error text - measured through
    the new `restore-add` as `'vm_import_unc'`, a key that entry had never mentioned under that
    name. It is `source.backup_share` and `target.vm_import_linux_path` an operator writes.
    """
    with pytest.raises(ValueError) as caught:
        parse_restore_config({"restore_id": "ACME_DRILL",
                              "target": {"restore_data_dir": "/var/opt/mssql/data"}})

    message = str(caught.value)
    assert "ACME_DRILL" in message, "the entry has to name itself; a config has many"
    assert "missing required field(s)" in message
    # Both spellings: the internal name the code reads, and the field somebody has to type.
    assert "prod_backup_share" in message and "source.backup_share" in message
    assert "vm_import_unc" in message and "vm_import_linux_path" in message
    assert "restore_config.example.json" in message


def test_every_field_read_by_subscript_is_declared_required():
    """The check and the constructor must not drift: a field added to one and not the other is a
    bare KeyError again, and the whole point is that there is no second list to forget."""
    import inspect
    import re

    from db_ops.backup_restore import config as config_module

    source = inspect.getsource(config_module.parse_restore_config)
    subscripted = set(re.findall(r'values\["([a-z_]+)"\]', source))

    assert subscripted, "the pattern this guard reads for has changed; check it still applies"
    undeclared = sorted(subscripted - set(config_module.REQUIRED_RESTORE_FIELDS))
    assert not undeclared, (
        f"{undeclared} are read by subscript but not in REQUIRED_RESTORE_FIELDS, so a missing one "
        "raises a bare KeyError again. Add each with the field an operator actually writes.")


def test_all_the_missing_fields_are_named_at_once_not_one_per_run():
    """A restore entry is hand-written and half these fields arrive together. A parser that stops
    at the first turns one incomplete entry into six edit-and-rerun cycles."""
    with pytest.raises(ValueError) as caught:
        parse_restore_config({"restore_id": "R"})

    message = str(caught.value)
    assert message.count(";") >= 3, message


def test_a_complete_linux_entry_still_parses():
    """The counterweight: the fallbacks that fill four of those six from one Linux path must keep
    working, or the check has turned a good config into a refusal."""
    parsed = parse_restore_config({
        "restore_id": "R", "cleanup_retention": 86400,
        "source": {"backup_share": "//192.0.2.10/SQLBK"},
        "target": {"vm_platform": "linux", "vm_import_linux_path": "/opt/restore/import",
                   "restore_data_dir": "/var/opt/mssql/data"},
    })

    assert str(parsed.vm_import_unc) == str(parsed.vm_import_local)
    assert parsed.restore_id == "R"


def test_no_restore_config_means_no_entries_rather_than_a_key_error(empty_root):
    """The exact reproduction. It raised `KeyError: 'prod_backup_share'` before this."""
    assert load_restore_configs("config.json") == []


def test_every_loader_is_quiet_on_an_install_with_nothing_configured(empty_root):
    """All three, because the app calls all three and one raising is enough to fail the cycle."""
    assert load_restore_configs("config.json") == []
    assert load_backup_jobs("config.json") == []
    assert load_script_restores("config.json") == []


def test_a_flat_single_source_block_is_still_parsed(empty_root):
    """The fall-through that had to keep working. A `backup_restore` object holding the fields
    directly — no `restores`, no `sources` — is the oldest shape and is still a real entry."""
    (empty_root / "config.json").write_text(json.dumps({"backup_restore": {
        "prod_backup_share": "//source/SQLBK",
        "vm_import_unc": "//target/import",
        "vm_import_local": "E:/import",
        "vm_log_unc": "//target/logs",
        "vm_log_local": "E:/logs",
        "cleanup_retention": 691200,
    }}), encoding="utf-8")

    configs = load_restore_configs("config.json")

    assert len(configs) == 1
    assert configs[0].cleanup_retention == 691200


@pytest.mark.parametrize("command", ["copy-backup", "restore-latest", "verify-restore"])
def test_a_manual_command_with_nothing_configured_says_so_rather_than_index_error(
        empty_root, monkeypatch, capsys, command):
    """The loader's `[]` is right for the scheduled `workflow`: nothing is due. A command asked to
    act on one restore has nothing to act on, and it reached `restore_configs[0]` - so the fix for
    `KeyError: 'prod_backup_share'` would have turned it into `IndexError: list index out of
    range`, equally silent about what is missing. It must fail, and name the file."""
    from db_ops.backup_restore import cli
    from db_ops.config import DbOpsConfig

    monkeypatch.setattr(cli, "load_config", lambda _path: DbOpsConfig(
        log_dir=empty_root / "logs", runtime_dir=empty_root / "runtime",
        sqlite_path=empty_root / "runtime" / "db_ops.sqlite"))
    monkeypatch.setattr(cli, "patch_stdout", lambda *a, **k: None)
    monkeypatch.setattr(cli, "setup_app_logger", lambda *a, **k: None)
    monkeypatch.setattr(cli, "emit_backup_restore_event", lambda **_k: None)

    assert cli.main([command, "--config", "config.json"]) == 1

    error = capsys.readouterr().err
    assert "IndexError" not in error and "list index out of range" not in error
    assert "data/restore_config.json" in error and command in error


def test_a_first_run_writes_the_file_the_manifest_promises(tmp_path):
    """`data_files.json` has listed `restore_config.json` all along; `init` did not write it.

    Empty is the content, not a placeholder: a restore target is an estate fact and there is no
    sensible default for somebody else's share, credential or instance. What the file carries is
    the shape and the word "empty", so an operator finds a file to edit rather than an absence to
    infer.
    """
    scaffold.initialise(tmp_path, app_name="probe")
    written = json.loads((tmp_path / "data" / "restore_config.json").read_text(encoding="utf-8"))

    assert written["backup_restore"] == {"backups": [], "restores": []}
    assert written["notes"], "the shape has to be written down somewhere the operator will look"

    manifest = json.loads((tmp_path / "data" / "data_files.json").read_text(encoding="utf-8"))
    listed = {item["file"] for item in (manifest.get("data_files") or manifest)}
    assert "restore_config.json" in listed


def test_the_shipped_file_loads_as_nothing_configured(tmp_path, monkeypatch):
    """The file `init` writes must itself parse to an empty estate, or shipping it would only move
    the failure from "no file" to "a file that cannot be read"."""
    scaffold.initialise(tmp_path, app_name="probe")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(config_module, "DEFAULT_RESTORE_CONFIG_PATH",
                        tmp_path / "data" / "restore_config.json")

    assert load_restore_configs("config.json") == []
    assert load_backup_jobs(tmp_path / "data" / "restore_config.json") == []


def test_the_workflow_signature_matches_what_the_cli_passes():
    """`run_workflow` and the CLI disagreed on a parameter name after the retention rename, which
    is a `TypeError` on the one path the daemon actually runs and no test exercised."""
    import inspect

    from db_ops.backup_restore import cli, workflow

    accepted = set(inspect.signature(workflow.run_workflow).parameters)
    source = inspect.getsource(cli.main)
    call = source[source.index("summary = run_workflow("):]
    call = call[:call.index(")\n")]
    passed = {line.split("=")[0].strip() for line in call.splitlines()[1:] if "=" in line}

    unknown = {name for name in passed if name and not name.startswith("#")} - accepted
    assert not unknown, f"cli.main passes arguments run_workflow does not accept: {sorted(unknown)}"


def test_run_workflow_passes_each_step_only_what_that_step_accepts(monkeypatch):
    """The hop after the CLI. Every existing test stubs `run_backup` and `run_scheduled_restores`
    with `lambda **_`, which accepts any keyword at all - so a parameter renamed on one side of
    this call and not the other passes the whole suite, exactly as §2.4 of the 2026-09-11 handover
    did. These stubs bind against the real signatures instead, and every parameter is set to a
    non-default so none of them is skipped by being left out."""
    import inspect

    from db_ops.backup_restore import workflow

    def binding(real):
        signature = inspect.signature(real)

        def stub(**kwargs):
            signature.bind(**kwargs)  # the TypeError the real call would raise, raised here
            return {"failed": 0}
        return stub

    monkeypatch.setattr(workflow, "run_backup", binding(workflow.run_backup))
    monkeypatch.setattr(workflow, "run_scheduled_restores", binding(workflow.run_scheduled_restores))

    result = workflow.run_workflow(
        app_config=object(), config_path="c.json", logger=object(), backup_id="B", job_name="full",
        restore_id="R", key="k", key_base64="a2V5", dry_run=True, force=True,
        env_overrides={"X": "1"}, backup_type="full", copy_hours=6, delete_retention=3600,
    )

    assert result["failed"] == 0
