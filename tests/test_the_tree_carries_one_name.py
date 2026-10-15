"""The code carries one name: `dbabrain`.

0.29.0 renamed the package, the command, the environment variables, the classes, the image and the
console mount in one step, with nothing kept for the earlier name (the operator, 2026-10-12: *cut
it completely, no need to keep it*). A rename like that does not stay done by itself: the next
copied example, the next pasted command line, the next test fixture brings the old spelling back,
and a reader who meets two names concludes there are two tools.

So this file reads the tree and allows the old spelling in exactly two kinds of place:

- **history** - dated records, released notes and the changelog, the captured showcase pages, and a
  node's own configuration (`data/*.json` that is not an example), which `upgrade-config` migrates
  and which is the operator's, not the code's;
- **the operator's SQL** under `assets/`, which runs against production: a lock name or a value it
  hands a stored procedure is a contract with that server, so the rename did not touch it. A Python
  script there that *imports* the package is the code's business, and is held below;
- **a value that lives outside the code** - the store's database, schema and the rows that carry
  its schema version, the certificate on the SQL Servers, folders on a host, the store's container
  and the network it sits on, the marker inside every node's secret store. The operator's own line
  (2026-10-12): *the database and schema the store uses stay - that is configuration, not code*.
  Each such value is named below with where it lives. The same line is drawn through the one-word
  nickname (the operator, 2026-10-12: *what belongs to configuration, and the name of a backup, a
  restore or a SQL task, is not changed; what belongs to the code is*): a backup folder, a login
  or a share is on a host and is read after the run that wrote it, and an RMAN tag is the name a
  backup carries - they keep theirs; a partial file's suffix, a helper container's mount, a
  multipart boundary and the PostgreSQL restore's staging and WAL folders are the code's own
  scratch and were renamed. A default that named something read later - the backup certificate,
  the staging marker - took the new name only after `upgrade-config` had written the earlier one
  into every record that ran on it (`lib.earlier_defaults`, sheet 1.123).

Allowed whole: the rules that find the old name in a node's configuration, the `upgrade-config`
step that runs them, and the tests of both and of `deploy-code`, which moves a compose file with
the same rules - finding the old name is their job.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]

#: The old spellings - package, command, environment prefix, class prefix - and the two the first
#: pass did not look for (0.29.0 sheet 1.123): the display name written as two words, which sat in
#: help text and two page titles, and the one-word nickname in either case, which was held aside
#: whole as "a folder on a host" while six of its uses were scratch names of the code's own. The
#: two words only as the display name was capitalised: in lower case they are a command of this
#: version (`dbabrain db ops-status`), and "DB ops" is plain English for what a DBA does.
OLD = re.compile(r"db_ops|db-ops|DB_OPS|DbOps|\bD[Bb] Ops\b|\bDB OPS\b|(?i:dbops)")

#: History, and a node's own configuration.
SKIPPED_PREFIXES = ("audits/", "docs/releases/", "examples/showcase/", "CHANGELOG.md", "assets/")
LIVE_CONFIGURATION = re.compile(r"^data/(?!.*\.example\.json$)[^/]+\.json$")

#: Files whose job is the old name.
ALLOWED_FILES = {
    "dbabrain/lib/package_rename.py",
    "dbabrain/common/config_upgrade.py",
    "tests/test_upgrade_config_renames_the_package.py",
    "tests/test_deploy_code_ships_the_code_only.py",
    "tests/test_the_tree_carries_one_name.py",
}

#: Values that live outside the code, each with where it lives.
KEPT_VALUES = [
    (r"/opt/db_ops", "folders on the worker host - backups and the lab databases' compose folders"),
    (r"db_ops_backup_cert", "the certificate the SQL Servers encrypt and restore backups with"),
    (r"db_ops_store", "the PostgreSQL container that holds the store"),
    (r"db_ops_default", "the docker network the store and the lab databases sit on"),
    (r"db_ops\.secret_text\.v1", "the format marker inside every node's encrypted secret store"),
    (r"DB_OPS_ORACLE8I_BRIDGE_SECRET", "the 8i bridge's token - a ref in the master's secret store"),
    (r'SCHEMA_OWNER = "DbOpsStore"', "the store's schema-version key, a row in every store"),
    (r'SCHEMA_ROW = "db_ops"', "the store's schema-version row read by `db.cli check`"),
    (r'"(?:database|database_name|schema|dbname|db_name|search_path)"\s*:\s*"db_ops"',
     "the store's database and schema, in configuration"),
    (r"(?:schema|search_path|dbname|database)\s*=\s*['\"]?db_ops\b", "the store's database and schema"),
    (r":(?!8080)\d{2,5}/db_ops\b", "the store's database in an address"),
    (r"%3Ddb_ops\b", "the store's schema in a connection string"),
    (r"(?:schema|database)\s+`db_ops`", "the store's schema or database, in prose"),
    (r"`db_ops`\s*/\s*`db_ops`", "the store's database and schema, in prose"),
    # The one-word nickname, where it is on a host or a server and read after the run that wrote it.
    (r"(?:backup|SQLBK)[/\\]+dbops\b",
     "the backup folders on the database hosts - where the chains are and where a restore looks"),
    (r"\.dbops-staging",
     "the marker's earlier name - the file a staging folder marked before 0.29.0 carries, and what "
     "such an entry states as its staging_marker; the code's own default is the new name"),
    (r"DBOPS_(?:ARCH|CTL|SPFILE)\b|DBOPS_L(?=[0-9$])",
     "the tag RMAN records on a backup - the name a backup carries in the Oracle catalogue and in "
     "its pieces, where a DBA reads it; the name of a backup is not changed"),
    (r"SQLBK_DBOPS", "the backup share and its folder on the Windows SQL Servers"),
    (r"(?i:svc_dbops)", "a service login on a database host, and the secret ref named after it"),
    (r"dbops_monitor|dbops_(?:app|report|disabled)_user", "roles and logins on database servers"),
    (r'"username": "dbops"|ORA_136_DBOPS|remote_2\.111_dbops',
     "a login named so on a database host, and the credential named after it, in a documented example"),
    (r"DBOPS-STORE", "a service label one store was registered with, quoted from the failure it caused"),
    (r"A1A_DBOPS_STORE_PG_115|ACME_MSSQL_DBOPS_TO_MSSQL2025_DOCKER",
     "the id of a backup entry and of a restore entry, quoted in the docs"),
    (r"'db\"ops',\s*\"DBOPS\"",
     "misspellings of the store's database name, in the list of names it may not have"),
    (r"DB Ops [·-] QuotaStaging", "what a shipped showcase page was titled on 2026-09-12, quoted"),
]
_KEPT = re.compile("|".join(f"(?:{pattern})" for pattern, _ in KEPT_VALUES))

_BINARY = (".jpg", ".jpeg", ".png", ".gif", ".ico", ".svg", ".pdf", ".zip", ".gz", ".tar", ".whl",
           ".exe", ".dll", ".pyd", ".so", ".key", ".pub")


def _files() -> list[str]:
    try:
        listed = subprocess.run(["git", "ls-files", "-z"], cwd=REPO, capture_output=True, check=True)
        return [name for name in listed.stdout.decode("utf-8").split("\0") if name]
    except (OSError, subprocess.CalledProcessError):
        skip = {".git", ".venv", "runtime", "logs", "build", "dist", "__pycache__", ".pytest_tmp"}
        return [path.relative_to(REPO).as_posix() for path in REPO.rglob("*")
                if path.is_file() and not skip.intersection(path.relative_to(REPO).parts)]


def _offenders() -> list[str]:
    found = []
    for name in _files():
        if (name.startswith(SKIPPED_PREFIXES) or LIVE_CONFIGURATION.match(name)
                or name in ALLOWED_FILES or name.lower().endswith(_BINARY)):
            continue
        path = REPO / name
        if not path.is_file():
            continue
        raw = path.read_bytes()
        if b"\x00" in raw:
            continue
        for number, line in enumerate(raw.decode("utf-8", errors="replace").splitlines(), 1):
            if OLD.search(_KEPT.sub("", line)):
                found.append(f"{name}:{number}: {line.strip()[:120]}")
    return found


def test_the_old_name_appears_only_where_a_value_lives_outside_the_code() -> None:
    offenders = _offenders()
    assert not offenders, (
        f"{len(offenders)} line(s) still carry the package's earlier name; rename them, or - if the "
        "value lives outside the code - add it to KEPT_VALUES with where it lives:\n  "
        + "\n  ".join(offenders[:40]))


def test_every_kept_value_says_where_it_lives() -> None:
    assert all(where.strip() for _, where in KEPT_VALUES)


def test_the_guard_finds_what_it_is_for() -> None:
    """A guard that stopped matching would pass by reading nothing."""
    assert OLD.search("python -m db_ops.common.cli") and OLD.search("DB_OPS_SECRET_KEY")
    assert OLD.search("db-ops init") and OLD.search("class DbOpsStore")
    assert not OLD.search(_KEPT.sub("", "/opt/db_ops/backup and db_ops_backup_cert"))
    assert OLD.search(_KEPT.sub("", "http://host:8080/db_ops/")), "the console mount is the code's"
    assert OLD.search("DB Ops SRE CLI.") and OLD.search('boundary = f"dbops-{token}"')
    assert not OLD.search("python -m dbabrain.db.cli db ops-status"), "a command of this version"
    assert not OLD.search("what DB ops and SREs do"), "plain English, not the display name"
    assert OLD.search(_KEPT.sub("", 'DEFAULT_NAME = "DBOPS"')), "kept in one list, not wherever quoted"
    assert not OLD.search(_KEPT.sub("", "/opt/oracle/backup/dbops/.dbops-staging TAG DBOPS_L0"))
    assert OLD.search(_KEPT.sub("", "/var/lib/postgresql/dbops_wal")), "a restore's scratch folder is the code's"
    assert OLD.search(_KEPT.sub("", "DBOPS_SCRIPT_EOF")), "a tag is kept, a name of the code's is not"
    assert OLD.search(_KEPT.sub("", "a.bak.dbops_partial")), "a scratch name is the code's"
    assert len(_files()) > 1000, "the tree was not read"


#: Operator scripts that may still reach the package by its earlier name, and until when. The worker
#: runs what the master's `assets/` holds, so a script there serves the version the worker runs:
#: on 2026-10-12 this one reached the 0.28.0 worker importing only the new name, and SQLSERVER-030
#: failed every run for 9 minutes (0.29.0 sheet 1.122). It now tries the new name first and falls
#: back; the fallback goes when the worker runs 0.29.0.
TRANSITIONAL_SCRIPTS = {"assets/tasks/python/calculate_working_hour_queue.py": "the worker runs 0.29.0"}


def test_an_operator_script_imports_the_package_by_its_name() -> None:
    """`assets/` is skipped above, but a script there that imports the package stops on the first
    run after the upgrade if it uses the old name alone - SQLSERVER-030's input script is one."""
    stale = []
    for name in _files():
        if not (name.startswith("assets/") and name.endswith(".py") and (REPO / name).is_file()):
            continue
        text = (REPO / name).read_text(encoding="utf-8", errors="replace")
        old = [line.strip() for line in text.splitlines()
               if re.match(r"\s*(from|import)\s+db_ops(\.|\s|$)", line) or "DB_OPS_" in line]
        if not old:
            continue
        if name in TRANSITIONAL_SCRIPTS and re.search(r"(?m)^\s*from\s+dbabrain\.", text):
            continue
        stale.extend(f"{name}: {line}" for line in old)
    assert not stale, stale
