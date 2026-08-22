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
    "config_admin.py": "writes data/*.json (add-sql, metric-toggle). Editing the config IS the "
                       "operation, so it cannot be handed the config as a value.",
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
