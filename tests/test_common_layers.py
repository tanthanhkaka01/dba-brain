"""``common`` is two tiers, and the smaller one is listed here by name.

``docs/13_common.md`` used to state one rule — *"No data here, and none read here. Not
``data/*.json``, not ``config.json``, not the secret store"* — and 15 of the 77 modules broke it.
That is not a rule anybody was keeping; it was a description of the tier the author had in mind,
applied to a package that had since grown a second one.

So the package is described as what it is:

* **the library** — input in, result out, nothing looked up: ``restore/``, ``db_connect``,
  ``response``, ``time_window``, ``policy_engine``, ``health_model``, and 66 others. This is the
  part that could be packaged and dropped anywhere, and it is the default a new module belongs to.
* **the resolver tier** — the 14 modules in :data:`READS_LOCAL_CONFIG`, which answer "which host
  is ``ACME-192-0-2-248``" or "what is that credential's password" and therefore have to read the
  data folder. They are `common` rather than app code because every app asks the same questions.

What this test defends is the **boundary between them**, because the way it was breached was not a
module announcing that it needed config — it was a **default argument**. ``ssh.py``,
``sql_run.py`` and six others take the fact as a parameter and fall back to ``data_sources`` when
the caller passes nothing. A caller who passes everything sees a pure function; a caller who
passes nothing silently gets this repo's ``data/`` folder. Both are the same code, and only one of
them works when the module is packaged somewhere else.

Adding an entry below is therefore a visible diff that says "this module cannot answer without
reading the machine it is installed on". The default answer for a new module is the library tier:
take the fact as a parameter and let the app look it up (rule 3 of ``docs/13_common.md``).

Import direction is a separate rule with its own file — see ``tests/test_import_boundaries.py``.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest


COMMON_ROOT = Path(__file__).resolve().parents[1] / "db_ops" / "common"

#: The resolver tier: modules that read `config.json`, `data/*.json`, or the runtime store.
#: Each maps to why it cannot answer its question without them.
READS_LOCAL_CONFIG: dict[str, str] = {
    "cli.py": "the layer's CLI entry point — a composition root, and the only caller that is "
              "supposed to resolve config before handing a JSON object to the library below it.",
    "remote_credential_admin.py": "registers one host's OS login: it reads users.json and "
              "db_instances.json to write them, and takes the host's ip off the inventory record "
              "so it is not retyped. The same argument as instance_admin.py beside it — a "
              "registration command whose whole job is those files cannot take them as a "
              "parameter, and `data_dir` is already the parameter that makes it testable.",
    "config_admin.py": "writes data/*.json (add-sql, metric-toggle). Editing the config IS the "
                       "operation, so it cannot be handed the config as a value.",
    "app_command_admin.py": "edits data/app_commands.json (app-command-set), for the same reason "
                       "config_admin.py beside it does: editing the config IS the operation. It "
                       "reads one more file than it writes - the app_command entry in "
                       "shared_config_objects.json, which is where the field list and every "
                       "field's constraint live - because a second list here would be a second "
                       "opinion about the shape of a record. `data_dir` is the parameter that "
                       "keeps it testable.",
    "sql_task_admin.py": "writes data/sql_commands.json and data/sql_targets.json (sql-command-add, "
                         "sql-target-add). The same argument as config_admin.py above it: editing "
                         "the config IS the operation. `data_dir` and `tool_root` are both "
                         "parameters, which is what keeps it testable against a temporary root.",
    # data_sources became a package on 2026-08-15: `metric_targets_config` and `target_resolve`
    # were doing the same job (open a file under data/, answer what is configured) and, under
    # "an app does not import common", had nowhere else to live. One exemption, one package.
    "data_sources/__init__.py": "is the data-folder loader itself; everything else in this list "
                                "reaches the folder through it.",
    "data_sources/metric_targets.py": "enumerates the configured metric targets — the question is "
                                      "literally 'what is in the data folder'.",
    "data_sources/ssh_auth.py": "answers where an SSH key file is (data/ssh_keys/) and what a "
                                "password_ref decrypts to — both are the data folder, by "
                                "definition. Split out of common/ssh.py on 2026-08-15 so an app "
                                "needing a key path no longer imports the paramiko transport.",
    "data_sources/target_resolve.py": "is the target resolver — db_instances.json is its input.",
    "host_ops.py": "resolves a host's OS credential before running anything on it.",
    # `showcase.py` was listed here for one commit on 2026-09-10 and taken out again by this
    # file's own second guard: it reaches the inventory only *through* `identifier_scan`, which is
    # already listed, so it reads no local state of its own. The allowance was the reflex and the
    # test was right — one module owns the question, and everything else inherits its answer.
    "instance_admin.py": "writes db_instances.json, users.json and the encrypted secret store. "
                        "Editing the config IS the operation, the same reason config_admin.py is "
                        "listed - a version taking the folder as a value would still have to be "
                        "told which folder, and that is the one thing a tool root already knows.",
    "identifier_scan.py": "searches for the estate's own names, so the inventory is not a "
                          "dependency it happens to have — it is the question. A version taking "
                          "the terms as an argument would need a maintained map beside the "
                          "inventory, and the two disagree the first time somebody adds a server.",
    # metric_store.py, sla_store.py, backup_restore_history.py and telegram_queue.py were here
    # until 2026-08-15. They read store_config.json because they *are* stores — which is what
    # finally moved them out of `common` entirely, into db_ops/db/ where ORD 01 owns the runtime
    # store. `common` writes to no database now, so it needs no entry for one.
    # "notify.py" left this list on 2026-08-15 when it moved to db_ops/lib/: apps parse notify
    # blocks in-process, so it could not be a CLI call. Its one config read (the notify-level
    # vocabulary) is lazy and fails open, and db_ops.config is a root module, not a component.
    "password_rotation.py": "changes a password on the server AND in the secret store; the store "
                            "is half the operation.",
    "remote_exec.py": "resolves the credential for the host it is about to reach.",
    # "report_archive.py" left on 2026-08-15: the only part of it that read the data folder
    # was `report_base_url`, which is now data_sources' own; the stamping and copying are
    # pure and moved to db_ops/lib/report_archive.py.
    "secret_check.py": "audits the secret store — the store is its subject.",
    "sql_run.py": "resolves a target spec (server_id or db_type/ip/port) to a db instance.",
    "sqlserver_instance.py": "resolves the instance and its credential for export/replay.",
    "ssh.py": "resolves the SSH credential and key path for a target.",
}

#: What reading local state looks like in the AST. `DEFAULT_DATA_DIR` is in here because that is
#: the form the boundary was actually crossed in: a module-level constant pointing at this repo's
#: data folder, used as a default argument.
_MARKERS = {
    "db_ops.config": "imports db_ops.config",
    "db_ops.db": "imports the runtime store (db_ops.db)",
    "data_sources": "imports the data-folder loader",
}


def _module_files() -> list[Path]:
    return sorted(p for p in COMMON_ROOT.rglob("*.py") if "__pycache__" not in p.parts)


def _relative(path: Path) -> str:
    return path.relative_to(COMMON_ROOT).as_posix()


def _reads_local_state(path: Path) -> list[str]:
    """Every way this file reaches config, the data folder or the store."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            for prefix, label in _MARKERS.items():
                if node.module == prefix or node.module.startswith(prefix + "."):
                    found.add(label)
            # `from db_ops.common.data_sources import _resolve_data_dir` — the loader named as
            # the module rather than imported from its package. Same dependency, and the form
            # report_archive.py used, so matching only the package prefix missed it.
            if node.module.split(".")[-1] == "data_sources":
                found.add(_MARKERS["data_sources"])
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            for alias in node.names:
                if alias.name.split(".")[-1] == "data_sources":
                    found.add(_MARKERS["data_sources"])
        # The default-argument form: a constant pointing at this repo's data folder.
        if isinstance(node, ast.Name) and node.id == "DEFAULT_DATA_DIR":
            found.add("defaults an argument to this repo's data/ folder (DEFAULT_DATA_DIR)")
    return sorted(found)


@pytest.mark.parametrize("path", _module_files(), ids=_relative)
def test_a_common_module_reads_no_local_state_unless_it_is_listed(path: Path) -> None:
    relative = _relative(path)
    if relative in READS_LOCAL_CONFIG:
        pytest.skip(f"resolver tier: {READS_LOCAL_CONFIG[relative]}")
    offenders = _reads_local_state(path)
    assert not offenders, (
        f"common/{relative} {' and '.join(offenders)}, but is not in READS_LOCAL_CONFIG. "
        "The library tier takes every fact as a parameter and the app looks it up "
        "(docs/13_common.md, rule 3) — check that a default argument has not quietly made this "
        "module depend on this repo's layout. If it genuinely cannot answer without reading the "
        "machine it runs on, add it to READS_LOCAL_CONFIG with that reason."
    )


def test_every_listed_module_still_reads_something() -> None:
    """An entry that no longer reads config is a module that quietly became pure — and an
    exception nobody is checking. Drop it, so the list keeps meaning what it says."""
    stale = [
        name for name in READS_LOCAL_CONFIG
        if (COMMON_ROOT / name).exists() and not _reads_local_state(COMMON_ROOT / name)
    ]
    assert not stale, (
        f"These modules no longer read local state and should leave READS_LOCAL_CONFIG: {stale}")


def test_every_listed_module_still_exists() -> None:
    missing = [name for name in READS_LOCAL_CONFIG if not (COMMON_ROOT / name).exists()]
    assert not missing, f"READS_LOCAL_CONFIG names files that no longer exist: {missing}"


def test_the_library_tier_is_still_the_large_majority() -> None:
    """A ratchet, not a target. The split is only meaningful while the resolver tier stays the
    exception; if half of `common` reads the data folder, "packaged and dropped elsewhere" has
    stopped being true of the layer and the split is a story rather than a fact."""
    total = len(_module_files())
    resolvers = len(READS_LOCAL_CONFIG)

    assert resolvers <= total // 4, (
        f"{resolvers} of {total} common modules read local state. The resolver tier has stopped "
        "being the exception — either the facts belong in the callers, or common has become two "
        "packages that should be named as such."
    )


# --------------------------------------------------------------------------- #
# The operator's rule for common.cli, held BY NAME where it is already true (0.23.0)
# --------------------------------------------------------------------------- #
# "common.cli imports nothing but lib, runs no other CLI, calls no app, reads no config JSON -
# input in, work, JSON out" (the operator, 2026-09-24). Measured that day, it held for backup,
# restore and the lab-docker commands once create-db-docker moved in - and did not hold for the
# 18 resolver-tier modules above, which are the next version's work (0.24). So it is enforced two
# ways: without exception for the modules below, which are named so none of them can quietly
# become a resolver; and as a baseline for the rest, which may only shrink.

#: Every module behind backup-database, list/prune-backup-files, restore-full/diff/log/key/
#: metadata, verify-restore, restore-database, run-sqlcmd (the SMB restore's statements),
#: pack-backup/pull-file/push-file, create/move-db-docker and a restore's copy (backup-chain,
#: copy-backup-dir, prune-staged-backups). A trailing slash names a package.
CONFIG_FREE_BY_NAME: tuple[str, ...] = (
    "cli_backup.py", "backup/",
    "cli_backup_files.py", "backupfiles/", "deletefiles.py",
    "cli_restorestep.py", "restorestep/", "restorekey.py", "restoremetadata.py", "verifyrestore.py",
    "cli_restore.py", "restore/",
    "cli_filetransfer.py", "filetransfer.py",
    "cli_docker_db.py", "docker_db/",
    "cli_sqlcmd.py", "sqlcmd_run.py",
    # 0.23.0 (1.48): a restore's copy and staging cleanup, moved out of the app.
    "cli_backup_copy.py", "backup_copy.py",
    "hostcmd.py", "ssh_relay.py", "db_connect.py",
)

#: The two transports a library module may reach a host through - with the credentials it was
#: handed. Both *can* resolve (a `password_ref`, a bare key name under data/ssh_keys/), which is
#: why they are resolver tier; what makes a call site library tier is that it never asks them to.
TRANSPORTS = {"ssh", "remote_exec"}
#: Handing a transport any of these asks it to look something up.
_RESOLVING_KEYWORDS = {"data_dir", "secrets", "credential"}


def _config_free_files() -> list[Path]:
    files = []
    for name in CONFIG_FREE_BY_NAME:
        if name.endswith("/"):
            files += sorted(p for p in (COMMON_ROOT / name).rglob("*.py") if "__pycache__" not in p.parts)
        else:
            files.append(COMMON_ROOT / name)
    return files


def _resolver_modules() -> set[str]:
    """`db_ops.common.x` names of the resolver tier, packages included."""
    names = set()
    for relative in READS_LOCAL_CONFIG:
        dotted = relative[:-3].replace("/", ".")
        names.add(dotted[: -len(".__init__")] if dotted.endswith(".__init__") else dotted)
    return names


def test_every_named_module_exists() -> None:
    missing = [name for name in CONFIG_FREE_BY_NAME if not (COMMON_ROOT / name.rstrip("/")).exists()]
    assert not missing, f"CONFIG_FREE_BY_NAME names what is not there: {missing}"


@pytest.mark.parametrize("path", _config_free_files(), ids=_relative)
def test_a_backup_restore_or_docker_module_reads_no_config_at_all(path: Path) -> None:
    relative = _relative(path)
    assert relative not in READS_LOCAL_CONFIG, (
        f"common/{relative} is named as config-free, and cannot be moved to the resolver tier: "
        "backup, restore and create-db-docker take every fact in the request (the operator's rule).")
    assert not _reads_local_state(path), f"common/{relative}: {_reads_local_state(path)}"


@pytest.mark.parametrize("path", _config_free_files(), ids=_relative)
def test_a_backup_restore_or_docker_module_reaches_no_resolver(path: Path) -> None:
    """Not even one hop away. A library module importing `host_ops` or `sql_run` reads config the
    moment it calls them, and the per-module scan above would not see it."""
    resolvers = _resolver_modules() - TRANSPORTS
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    reached = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            parts = node.module.split(".")
            if parts[:2] == ["db_ops", "common"]:
                inner = ".".join(parts[2:])
                reached |= {inner} if inner else {alias.name for alias in node.names}
                reached |= {f"{inner}.{alias.name}" for alias in node.names} if inner else set()
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.startswith("db_ops.common."):
                    reached.add(alias.name[len("db_ops.common."):])
    offenders = sorted(name for name in reached if name in resolvers
                       or any(name.startswith(r + ".") for r in resolvers))
    assert not offenders, f"common/{_relative(path)} imports the resolver tier: {offenders}"


@pytest.mark.parametrize("path", _config_free_files(), ids=_relative)
def test_a_transport_is_handed_values_never_asked_to_look_them_up(path: Path) -> None:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    offenders = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        # A transport's own function - imported by name, or `remote_exec.x` / `ssh.x`. Not any
        # method that happens to share a name: paramiko's `transport.open_session()` opens a
        # channel on a connection already made, and asks nothing of anyone.
        func = node.func
        if isinstance(func, ast.Name):
            name = func.id
        elif (isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name)
              and func.value.id in TRANSPORTS):
            name = func.attr
        else:
            continue
        if name not in {"open_session", "run_command", "run_script", "open_ssh_client"}:
            continue
        keywords = {kw.arg: kw.value for kw in node.keywords}
        asked = sorted(set(keywords) & _RESOLVING_KEYWORDS)
        if asked:
            offenders.append(f"line {node.lineno}: {name}(... {', '.join(asked)} ...)")
        # A key NAME resolves under data/ssh_keys/ unless the caller says it already did.
        if name == "open_session" and not (
                isinstance(keywords.get("resolve_key"), ast.Constant)
                and keywords["resolve_key"].value is False):
            offenders.append(f"line {node.lineno}: open_session without resolve_key=False")
    assert not offenders, f"common/{_relative(path)}: {offenders}"


#: READS_LOCAL_CONFIG as it stood at 0.23.0 - 18 modules. It may only shrink: moving one of them
#: to the library tier means deleting it here AND above, in the same commit. Adding one - or
#: swapping one for another, which a count alone would not catch - fails.
RESOLVER_TIER_AT_0_23_0 = frozenset({
    "cli.py", "remote_credential_admin.py", "config_admin.py", "app_command_admin.py",
    "sql_task_admin.py", "data_sources/__init__.py", "data_sources/metric_targets.py",
    "data_sources/ssh_auth.py", "data_sources/target_resolve.py", "host_ops.py",
    "instance_admin.py", "identifier_scan.py", "password_rotation.py", "remote_exec.py",
    "secret_check.py", "sql_run.py", "sqlserver_instance.py", "ssh.py",
})


def test_the_resolver_tier_only_shrinks() -> None:
    grown = sorted(set(READS_LOCAL_CONFIG) - RESOLVER_TIER_AT_0_23_0)
    assert not grown, (
        f"READS_LOCAL_CONFIG gained {grown}. The resolver tier is a baseline since 0.23.0: a new "
        "module takes its facts as parameters and the app looks them up.")


def test_the_baseline_names_nothing_that_has_left_the_tier() -> None:
    stale = sorted(RESOLVER_TIER_AT_0_23_0 - set(READS_LOCAL_CONFIG))
    assert not stale, f"These left the resolver tier - delete them from the baseline too: {stale}"

