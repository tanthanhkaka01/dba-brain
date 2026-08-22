"""Shared fixtures. Chiefly: a test's own estate, so no test reads the operator's.

Roughly fifty places in this suite load the repository's real `config.json`, and through it the
real `data/db_instances.json`. That made the suite *offline* — it touches no network — without
making it *self-contained*, and the two are not the same claim. It surfaced on 2026-08-21 while
identifiers were being replaced with documentation placeholders: rename a host in a test that
resolves against the live inventory and the two disagree, so seventeen tests failed and announced
a coupling nobody had chosen.

It also fails the thing the public repository needs, independently of any scrub: a clean checkout
has no `config.json` and no `data/`, so a suite that reads them cannot pass there.

`estate` gives a test the smallest inventory it needs, in a temporary directory, and points the
one reader of the data folder at it. Everything else follows, because
`db_ops.common.data_sources` is that single reader — `_resolve_data_dir` reads the module global
at call time, so redirecting it redirects every loader that funnels through it.

    def test_something(estate):
        estate.db_instances({"server_id": "ACME-192-0-2-10", "ip": "192.0.2.10", ...})
        config_path = estate.config()

Write only what the test needs. A fixture that mirrors the whole estate is the coupling again,
wearing a different hat.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest


class Estate:
    """A temporary `data/` directory, and a `config.json` pointing at it."""

    def __init__(self, root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        self.root = root
        self.data_dir = root / "data"
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self._monkeypatch = monkeypatch

        # `data_sources` is the one reader of the data folder (see
        # tests/test_app_common_imports.py), so this is the only redirection needed for anything
        # that goes through it. Modules that imported the constant into their own namespace get
        # redirected on request, via `also_redirect`.
        from db_ops.common import data_sources

        monkeypatch.setattr(data_sources, "DEFAULT_DATA_DIR", self.data_dir)

    # -- writing --------------------------------------------------------------------------- #
    def write(self, name: str, payload: Any) -> Path:
        """Write one `data/<name>` file and return its path."""
        path = self.data_dir / name
        path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        return path

    def db_instances(self, *instances: dict[str, Any]) -> Path:
        """The inventory, in the shape `load_db_instances` expects."""
        return self.write("db_instances.json", {"db_instances": list(instances)})

    def instance(self, **overrides: Any) -> dict[str, Any]:
        """One plausible SQL Server instance, so a test states only what it cares about."""
        record = {
            "server_id": "ACME-192-0-2-10",
            "db_instance_name": "sqlserver_192.0.2.10",
            "ip": "192.0.2.10",
            "port": 1433,
            "db_type": "sqlserver",
            "instance_name": "MSSQLSERVER",
            "service_name": "SALESDB",
            "site": "ACME",
            "env": "test",
            "enabled": True,
        }
        record.update(overrides)
        return record

    def config(self, **overrides: Any) -> str:
        """Write a `config.json` beside the data directory and return its path as a string.

        Paths are absolute so the file works whatever directory the test runs from — a relative
        `data/` in a config is resolved against the *working* directory, which is how a test can
        appear to pass while reading the repository's real folder.
        """
        payload = {
            "app_name": "db_ops-test",
            "log_dir": str(self.root / "logs"),
            "runtime_dir": str(self.root / "runtime"),
            "console_level": "ERROR",
            "file_level": "ERROR",
            "store_config_file": str(self.data_dir / "store_config.json"),
            "telegram_config_file": str(self.data_dir / "telegram_config.json"),
        }
        payload.update(overrides)
        path = self.root / "config.json"
        path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        return str(path)

    # -- redirection ----------------------------------------------------------------------- #
    def also_redirect(self, *targets: str) -> None:
        """Point a module that imported `DEFAULT_DATA_DIR` into its own namespace at this estate.

        `from ... import DEFAULT_DATA_DIR` binds the value once, so patching the definition does
        not reach the copy. Name the copies here rather than leaving a test to fail obscurely:

            estate.also_redirect("db_ops.reports.metrics_reports.DEFAULT_DATA_DIR")
        """
        for target in targets:
            self._monkeypatch.setattr(target, self.data_dir)


@pytest.fixture
def estate(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Estate:
    """A self-contained estate for one test. See this module's docstring."""
    return Estate(tmp_path, monkeypatch)


def write_catalogued_data(data: Path) -> Path:
    """The smallest catalogued data folder a console or drift test needs.

    It used to be `shutil.copytree(REPO_ROOT / "data", data)` — the operator's own estate, mirrored
    into every run. That one line tied 87 tests to one company's inventory and made them
    uncollectable on any checkout without it, which is every public one. What the console tests
    actually assert on is the *shape*: catalogued files, records with keys and labels, an app that
    has one config file and an app that has several.

    `config_sync` reports a catalogued file that is absent rather than failing on it, so the
    catalog may name more than exists — but everything named here is written, because a test that
    depends on a "missing" outcome should say so itself.
    """
    data.mkdir(parents=True, exist_ok=True)
    write = lambda name, payload: (data / name).write_text(
        json.dumps(payload, indent=2), encoding="utf-8")

    write("config_catalog.json", {"schema_version": 1, "config_sources": [
        {"file": "app_commands.json", "app_code": "jobs",
         "display_name": "Scheduled app commands",
         "description": "What the daemon runs, how often, and in which node role.",
         "collections": [{"collection": "app_commands", "key_fields": ["app_command_id"],
                          "label_field": "display_name"}]},
        {"file": "db_instances.json", "app_code": "metrics", "display_name": "Database instances",
         "description": "Every instance the estate monitors.",
         "collections": [{"collection": "db_instances", "key_fields": ["db_instance_name"],
                          "label_field": "db_instance_name"}]},
        {"file": "metric_definitions.json", "app_code": "metrics",
         "display_name": "Metric definitions", "description": "What each metric collects.",
         "collections": [{"collection": "metric_definitions", "key_fields": ["metric_code"],
                          "label_field": "metric_code"}]},
        {"file": "reports_config.json", "app_code": "reports", "display_name": "Reports",
         "description": "Which reports are produced and where they are published.",
         "collections": [{"collection": "reports", "key_fields": ["report_id"],
                          "label_field": "report_id"}]},
        {"file": "telegram_users.json", "app_code": "telegram", "display_name": "Telegram users",
         "description": "People the bot has seen, and the permission level each holds.",
         "collections": [{"collection": "telegram_users", "key_fields": ["user_id"],
                          "label_field": "username"}]},
        # The dashboard draws its app blocks from the *store*, not from the folder — they arrive
        # through this catalog entry. Leaving it out renders a console that knows no apps at all.
        {"file": "webhost_config.json", "app_code": "webhost", "display_name": "Web console",
         "description": "Login/session rules and the app blocks the dashboard draws.",
         "collections": [{"collection": "apps", "key_fields": ["app_code"],
                          "label_field": "display_name"}]},
    ]})

    write("app_commands.json", {"app_commands": [
        {
                "app_command_id": "APP-BACKUP-RESTORE",
                "app_code": "APP-BACKUP-RESTORE",
                "app_name": "db_ops_backup",
                "display_name": "Run backup then restore workflow",
                "log_scope": "backup",
                "working_dir": "tools/db_ops",
                "active": True,
                "node_role": "worker",
                "command_text": "python -m db_ops.backup_restore.cli workflow --config config.json",
                "time_window": {
                        "from_hour": 0,
                        "to_hour": 23,
                        "repeat_interval": 300,
                        "retry_interval": 60,
                        "timeout": 7200
                }
        },
        {
                "app_command_id": "APP-CONTROL",
                "app_code": "APP-CONTROL",
                "app_name": "db_ops_control",
                "display_name": "Watch db_ops itself: alert on an app that starts failing, summarise every app hourly",
                "log_scope": "control",
                "working_dir": "tools/db_ops",
                "active": True,
                "node_role": "worker",
                "command_text": "python -m db_ops.db.cli ops-status '{\"mode\": \"auto\", \"telegram_chat\": \"control\", \"summary_from_hour\": 8, \"summary_to_hour\": 20, \"summary_interval_seconds\": 3600, \"window_hours\": 24}'",
                "time_window": {
                        "from_hour": 0,
                        "to_hour": 23,
                        "repeat_interval": 60,
                        "retry_interval": 60,
                        "timeout": 120
                }
        },
        {
                "app_command_id": "APP-METRICS",
                "app_code": "APP-METRICS",
                "app_name": "db_ops_metrics",
                "display_name": "Collect DB metrics",
                "log_scope": "metrics",
                "working_dir": "tools/db_ops",
                "active": True,
                "node_role": "worker",
                "command_text": "python -m db_ops.metrics.cli --config config.json collect",
                "time_window": {
                        "from_hour": 0,
                        "to_hour": 23,
                        "repeat_interval": 120,
                        "retry_interval": 60,
                        "timeout": 2400
                }
        },
        {
                "app_command_id": "APP-REPORTS-CREATE",
                "app_code": "APP-REPORTS-CREATE",
                "app_name": "db_ops_reports",
                "display_name": "Run scheduled reports",
                "log_scope": "reports",
                "working_dir": "tools/db_ops",
                "active": True,
                "node_role": "worker",
                "command_text": "python -m db_ops.reports.cli --config config.json run-scheduled --summary-limit 150 --backup-days 7",
                "time_window": {
                        "from_hour": 0,
                        "to_hour": 23,
                        "repeat_interval": 120,
                        "retry_interval": 60,
                        "timeout": 600
                }
        },
        {
                "app_command_id": "APP-REPORTS-INVENTORY-WORKFLOW",
                "app_code": "APP-REPORTS-INVENTORY-WORKFLOW",
                "app_name": "db_ops_reports",
                "display_name": "Build inventory health + summary (local, reads the store directly)",
                "log_scope": "reports",
                "working_dir": "tools/db_ops",
                "active": True,
                "node_role": "worker",
                "command_text": "python -m db_ops.reports.cli inventory-workflow --days 7 --beauty 1",
                "time_window": {
                        "from_hour": 0,
                        "to_hour": 23,
                        "repeat_interval": 3600,
                        "retry_interval": 60,
                        "timeout": 1800
                }
        },
        {
                "app_command_id": "APP-SLA-VALIDATE",
                "app_code": "APP-SLA-VALIDATE",
                "app_name": "db_ops_sla",
                "display_name": "Validate SLA/SLO compliance",
                "log_scope": "sla",
                "working_dir": "tools/db_ops",
                "active": True,
                "node_role": "worker",
                "command_text": "python -m db_ops.sla.cli --config config.json validate --format text --notify --publish-web --allow-fail",
                "time_window": {
                        "from_hour": 0,
                        "to_hour": 23,
                        "repeat_interval": 3600,
                        "retry_interval": 60,
                        "timeout": 600
                }
        },
        {
                "app_command_id": "APP-SQL_TASKS",
                "app_code": "APP-SQL_TASKS",
                "app_name": "db_ops_sql_tasks",
                "display_name": "Run SQL task scheduler",
                "log_scope": "sql_tasks",
                "working_dir": "tools/db_ops",
                "active": True,
                "node_role": "worker",
                "command_text": "python -m db_ops.sql_tasks.runner --config config.json",
                "time_window": {
                        "from_hour": 0,
                        "to_hour": 23,
                        "repeat_interval": 60,
                        "retry_interval": 60,
                        "timeout": 1800
                }
        },
        {
                "app_command_id": "APP-TELEGRAM",
                "app_code": "APP-TELEGRAM",
                "app_name": "db_ops_telegram",
                "display_name": "Run Telegram workflow",
                "log_scope": "telegram",
                "working_dir": "tools/db_ops",
                "active": True,
                "node_role": "worker",
                "command_text": "python -m db_ops.telegram.cli --config config.json run-workflow",
                "time_window": {
                        "from_hour": 0,
                        "to_hour": 23,
                        "repeat_interval": 1,
                        "retry_interval": 60,
                        "timeout": 300
                }
        },
        {
                "app_command_id": "APP-WEBHOST",
                "app_code": "APP-WEBHOST",
                "app_name": "db_ops_webhost",
                "display_name": "Serve inventory report over HTTP (web host)",
                "log_scope": "webhost",
                "working_dir": "tools/db_ops",
                "active": True,
                "node_role": "worker",
                "command_text": "python -m db_ops.webhost.cli --config config.json serve --root runtime/reports --mount report_dba --port 8080",
                "time_window": {
                        "from_hour": 0,
                        "to_hour": 23,
                        "repeat_interval": 0,
                        "retry_interval": 60,
                        "timeout": 0
                }
        }
]})
    write("db_instances.json", {"db_instances": [
        {"db_instance_name": "sqlserver_192.0.2.10", "server_id": "ACME-192-0-2-10",
         "ip": "192.0.2.10", "port": 1433, "db_type": "sqlserver", "enabled": True},
    ]})
    write("metric_definitions.json", {"metric_definitions": [
        {"metric_code": "DATABASE_STATUS", "db_type": "sqlserver", "active": True},
        {"metric_code": "BACKUP_LAST_RESULT", "db_type": "sqlserver", "active": True},
    ]})
    # `report_base_url` sits beside `reports` at the top level, which is what produces the
    # `__document__` row the record count has to exclude. `time_window` is the nested block the
    # record page draws as its own section.
    write("reports_config.json", {"report_base_url": "", "reports": [
        {"report_id": "rp_metric_daily_logging", "report_code": "METRIC_DAILY", "active": True,
         "description": "Daily metrics summary.",
         "time_window": {"repeat_interval": 3600, "from_hour": 1, "to_hour": 6}},
        {"report_id": "rp_backup_health", "report_code": "BACKUP_HEALTH", "active": True,
         "description": "Backup health.",
         "time_window": {"repeat_interval": 86400, "from_hour": 0, "to_hour": 23}},
    ]})
    write("telegram_users.json", {"telegram_users": [
        {"user_id": "100000001", "username": "operator", "display_name": "Operator",
         "user_type": 100, "active": True},
        # A second row on purpose: deleting the only record would leave the collection empty, and
        # the test that retires one still expects a file with rows in it.
        {"user_id": "100000002", "username": "viewer", "display_name": "Viewer",
         "user_type": 1, "active": True},
    ]})
    write("webhost_config.json", {"schema_version": 1, "web": {
        "mount": "db_ops", "cookie_name": "db_ops_session", "session_days": 7,
        "cookie_secure": False, "cookie_samesite": "Lax", "max_failed_logins": 5,
        "lockout_minutes": 10, "min_level_view": 1, "min_level_edit": 50, "min_level_run": 50,
    }, "apps": [
        {
                "app_code": "db",
                "ord": 1,
                "display_name": "Runtime Store",
                "doc": "docs/01_runtime_store.md",
                "summary": "The database db_ops keeps its own data in: job runs, metric results, reports, the Telegram queue, and the config mirror.",
                "app_command_ids": []
        },
        {
                "app_code": "logging_ops",
                "ord": 2,
                "display_name": "Logging Engine",
                "doc": "docs/02_logging_engine.md",
                "summary": "One logger per app: files under logs/, rows in job_runs, and the notify level that decides which Telegram chat hears about it.",
                "app_command_ids": []
        },
        {
                "app_code": "jobs",
                "ord": 3,
                "display_name": "App Command Daemon",
                "doc": "docs/03_app_command_daemon.md",
                "summary": "The scheduler. Reads app_commands.json and runs every other app on its own interval, forwarding the secret key to each child process.",
                "app_command_ids": []
        },
        {
                "app_code": "metrics",
                "ord": 4,
                "display_name": "Metrics Engine",
                "doc": "docs/04_metrics_engine.md",
                "summary": "Collects every metric from every enabled target and writes metric_results. Reports and SLA both read what it produces.",
                "app_command_ids": [
                        "APP-METRICS"
                ]
        },
        {
                "app_code": "sql_tasks",
                "ord": 5,
                "display_name": "SQL Task Runner",
                "doc": "docs/05_sql_task_runner.md",
                "summary": "Runs the scheduled SQL scripts against their configured targets and delivers the output as text or a spreadsheet.",
                "app_command_ids": [
                        "APP-SQL_TASKS"
                ]
        },
        {
                "app_code": "reports",
                "ord": 6,
                "display_name": "Reports App",
                "doc": "docs/06_reports_app.md",
                "summary": "Turns collected metrics into the scheduled reports and the inventory pages the web host publishes.",
                "app_command_ids": [
                        "APP-REPORTS-CREATE",
                        "APP-REPORTS-INVENTORY-WORKFLOW"
                ]
        },
        {
                "app_code": "telegram",
                "ord": 7,
                "display_name": "Telegram App",
                "doc": "docs/07_telegram_app.md",
                "summary": "The bot: delivers the outgoing queue one message at a time, and executes the commands people send back.",
                "app_command_ids": [
                        "APP-TELEGRAM"
                ]
        },
        {
                "app_code": "backup_restore",
                "ord": 8,
                "display_name": "Backup / Restore App",
                "doc": "docs/08_backup_restore_app.md",
                "summary": "Runs backups, proves them by restoring, and records what was verified and when.",
                "app_command_ids": [
                        "APP-BACKUP-RESTORE"
                ]
        },
        {
                "app_code": "sla",
                "ord": 9,
                "display_name": "SLA / SLO Compliance",
                "doc": "docs/09_sla_slo_compliance_app.md",
                "summary": "Validates the objectives against collected metrics and reports the error budget left on each.",
                "app_command_ids": [
                        "APP-SLA-VALIDATE"
                ]
        },
        {
                "app_code": "sre",
                "ord": 10,
                "display_name": "SRE App",
                "doc": "docs/10_sre_app.md",
                "summary": "Provisions and moves the lab databases in Docker that drills and tests run against.",
                "app_command_ids": []
        },
        {
                "app_code": "control",
                "ord": 11,
                "display_name": "Control App",
                "doc": "docs/11_control_app.md",
                "summary": "Builds and deploys db_ops to the worker, and watches db_ops itself - the only app that reports on the others.",
                "app_command_ids": [
                        "APP-CONTROL"
                ]
        },
        {
                "app_code": "webhost",
                "ord": 12,
                "display_name": "Web Host",
                "doc": "docs/12_webhost_app.md",
                "summary": "Serves the reports over HTTP and hosts this console. Runs once and stays up rather than repeating on an interval.",
                "app_command_ids": [
                        "APP-WEBHOST"
                ]
        },
        {
                "app_code": "common",
                "ord": 13,
                "display_name": "Common Operations",
                "doc": "docs/13_common.md",
                "summary": "The shared operations layer: run SQL, reach a host, move a file, rotate a password. Apps call it through its CLI, never by import.",
                "app_command_ids": []
        },
        {
                "app_code": "lib",
                "ord": 14,
                "display_name": "Shared Rules",
                "doc": "docs/14_lib.md",
                "summary": "Pure values and rules - time windows, notify routing, severity, formatting. Imported everywhere, runs nothing.",
                "app_command_ids": []
        }
]})

    return data


#: The SQL Server instance-portability policy, as the shipped file states it. Verified to contain
#: no host, account or database name — it is a set of decisions about *what is portable between
#: two instances*, which is why the tests can carry it verbatim. `notes` is dropped: the prose is
#: documentation for the operator editing the real file, not something a test asserts on.
SQLSERVER_INSTANCE_POLICY = {
    "schema_version": 1,
    "artifacts": {
        "note": "Each artifact is one file under server/ in the bundle, applied in this order. 'phase' says whether it must run BEFORE the user databases are restored or AFTER: logins must exist before, or every restored database's users are orphaned; Agent jobs must come after, because their steps name databases that have to exist. 'min_major_version' gates replay onto an older target.",
        "sp_configure": {
            "order": 1,
            "phase": "pre-database",
            "min_major_version": 10
        },
        "credentials": {
            "order": 2,
            "phase": "pre-database",
            "min_major_version": 10
        },
        "logins": {
            "order": 3,
            "phase": "pre-database",
            "min_major_version": 10
        },
        "server_roles": {
            "order": 4,
            "phase": "pre-database",
            "min_major_version": 10,
            "user_defined_min_major_version": 11
        },
        "permissions": {
            "order": 5,
            "phase": "pre-database",
            "min_major_version": 10
        },
        "endpoints": {
            "order": 6,
            "phase": "pre-database",
            "min_major_version": 10
        },
        "linked_servers": {
            "order": 7,
            "phase": "pre-database",
            "min_major_version": 10
        },
        "db_mail": {
            "order": 8,
            "phase": "post-database",
            "min_major_version": 10,
            "requires_agent": True
        },
        "operators": {
            "order": 9,
            "phase": "post-database",
            "min_major_version": 10,
            "requires_agent": True
        },
        "proxies": {
            "order": 10,
            "phase": "post-database",
            "min_major_version": 10,
            "requires_agent": True
        },
        "agent_schedules": {
            "order": 11,
            "phase": "post-database",
            "min_major_version": 10,
            "requires_agent": True
        },
        "agent_jobs": {
            "order": 12,
            "phase": "post-database",
            "min_major_version": 10,
            "requires_agent": True
        },
        "alerts": {
            "order": 13,
            "phase": "post-database",
            "min_major_version": 10,
            "requires_agent": True
        },
        "model_options": {
            "order": 14,
            "phase": "post-database",
            "min_major_version": 10
        }
    },
    "sp_configure": {
        "note": "'portable' is replayed as-is. 'host_specific' describes the MACHINE, not the workload, so it is exported COMMENTED OUT with the source value visible and reported as skipped - replaying a 256 GB source's memory ceiling onto a 32 GB target is worse than not replaying it. Anything not listed here is exported commented out too, because an unclassified option is an option nobody has decided about.",
        "portable": [
            "backup compression default",
            "blocked process threshold (s)",
            "clr enabled",
            "contained database authentication",
            "cost threshold for parallelism",
            "database mail XPs",
            "default trace enabled",
            "optimize for ad hoc workloads",
            "remote admin connections",
            "remote login timeout (s)",
            "remote query timeout (s)",
            "xp_cmdshell",
            "Agent XPs",
            "Ole Automation Procedures",
            "backup checksum default",
            "default full-text language",
            "default language",
            "nested triggers",
            "two digit year cutoff"
        ],
        "host_specific": [
            "max server memory (MB)",
            "min server memory (MB)",
            "max degree of parallelism",
            "affinity mask",
            "affinity I/O mask",
            "affinity64 mask",
            "affinity64 I/O mask",
            "max worker threads",
            "index create memory (KB)",
            "min memory per query (KB)",
            "fill factor (%)",
            "recovery interval (min)",
            "lightweight pooling",
            "priority boost"
        ]
    },
    "model_options": {
        "note": "The portable subset of ALTER DATABASE model SET. Sizing (file size/growth) and anything naming a path is deliberately absent: a new instance's disks are its own. is_trustworthy_on is excluded: SQL Server exposes the column but refuses ALTER DATABASE model SET TRUSTWORTHY (error 15309).",
        "portable": [
            "recovery_model_desc",
            "page_verify_option_desc",
            "is_auto_create_stats_on",
            "is_auto_update_stats_on",
            "is_auto_update_stats_async_on",
            "is_auto_shrink_on",
            "is_auto_close_on",
            "is_parameterization_forced",
            "is_date_correlation_on",
            "is_read_committed_snapshot_on",
            "snapshot_isolation_state_desc"
        ]
    },
    "logins": {
        "note": "SQL logins are replayed WITH their SID and password hash, which is what stops restored user databases having orphaned users and lets applications keep their connection strings. Windows logins carry no hash and their SID comes from the domain, so they are created FROM WINDOWS with no SID clause. 'skip_name_prefixes' are the built-ins every instance already has.",
        "preserve_sid": True,
        "preserve_password_hash": True,
        "skip_name_prefixes": [
            "##",
            "NT AUTHORITY\\",
            "NT SERVICE\\",
            "BUILTIN\\"
        ],
        "skip_names": [
            "sa",
            "distributor_admin"
        ],
        "on_unmapped_windows_login": "skip"
    },
    "agent_jobs": {
        "note": "preserve_job_id keeps job history correlatable across a rebuild and makes replay idempotent; a collision with a job of the same id but a different name is gated rather than overwritten. default_owner is used when the source owner is a login that was skipped (a domain account that does not exist on the target).",
        "preserve_job_id": True,
        "default_owner": "sa",
        "skip_categories": [
            "REPL-*",
            "Database Maintenance Plan"
        ]
    },
    "secrets": {
        "note": "SQL Server encrypts these with the service master key and offers no read path, so an export CANNOT contain them. Each is emitted as a placeholder naming a key in data/encrypted_secret_text.json, resolved only at replay. Replay fails closed and lists every unresolved reference BEFORE executing anything, rather than creating a credential with a placeholder string that fails at first use.",
        "placeholder_format": "{{secret:%s}}",
        "kinds": [
            "credential",
            "linked_server_login",
            "proxy",
            "db_mail_account",
            "endpoint_certificate"
        ]
    }
}


def write_sqlserver_instance_policy(data: Path) -> Path:
    """Write the policy the instance-export tests read, into *data*.

    They used to read `data/sqlserver_instance_policy.json` through `DEFAULT_DATA_DIR`, so they
    could not run without the operator's folder — and the loader refuses to invent a default,
    deliberately: "db_ops has no built-in answer on purpose".
    """
    data.mkdir(parents=True, exist_ok=True)
    path = data / "sqlserver_instance_policy.json"
    path.write_text(json.dumps(SQLSERVER_INSTANCE_POLICY, indent=2), encoding="utf-8")
    return path


def shipped_config(name: str) -> Path:
    """A configuration file as *this installation* has it: the real one, else the shipped example.

    Some checks are about the definitions the project ships rather than about code — that the
    `/spbot_report_metric_history` command is wired to the right CLI, say. They have to read a
    file, and which file depends on where they are running: an operator's checkout has
    `data/telegram_support_commands.json`, a fresh clone has only
    `data/telegram_support_commands.example.json`.

    Asking for the real one first keeps an operator's suite checking what that operator actually
    runs. Falling back to the example is what lets the same test run at all in a public checkout —
    and it puts a real obligation on the example: it must contain the definitions the tests name,
    or those tests fail there. That is the right pressure. An example nobody exercises drifts.
    """
    from db_ops.lib.paths import DEFAULT_DATA_DIR

    real = DEFAULT_DATA_DIR / name
    if real.exists():
        return real
    stem, _, suffix = name.rpartition(".")
    return DEFAULT_DATA_DIR / f"{stem}.example.{suffix}"


#: Materialised once per session by :func:`shipped_data_dir`, because it is read at *collection*
#: time — a parametrize argument cannot ask for a fixture, and building it per call would copy the
#: same files for every test that asks.
_SHIPPED_DATA_DIR: Path | None = None


def shipped_data_dir() -> Path:
    """A whole ``data/`` directory as this installation has it, file by file.

    The directory-level counterpart of :func:`shipped_config`, for the handful of checks whose
    subject is the *set* of configuration files rather than one of them — that the config catalog
    names every file, that every catalogued file splits into rows and rebuilds unchanged.

    On an operator's checkout this is the real ``data/`` and nothing is copied, so those checks go
    on measuring what that operator actually runs. On a public checkout the real files are absent
    and the examples stand in for them, **renamed to the name they are an example of** — which is
    the only way a catalog keyed by ``telegram_groups.json`` can be checked against a tree that
    ships ``telegram_groups.example.json``.

    That renaming is the point, not a workaround. `test_config_sync.py` used to fail at
    *collection* on a clean checkout — one missing catalog took the entire suite down before a
    single test ran — and the examples it now reads are held to the same losslessness the real
    files are. An example that drifts out of the shape the catalog declares fails here rather than
    in a stranger's first hour.
    """
    global _SHIPPED_DATA_DIR
    if _SHIPPED_DATA_DIR is not None:
        return _SHIPPED_DATA_DIR

    import shutil
    import tempfile

    from db_ops.lib.paths import DEFAULT_DATA_DIR

    real = [path for path in DEFAULT_DATA_DIR.glob("*.json") if ".example." not in path.name]
    if real:
        _SHIPPED_DATA_DIR = DEFAULT_DATA_DIR
        return _SHIPPED_DATA_DIR

    target = Path(tempfile.mkdtemp(prefix="db_ops_shipped_data_"))
    for example in DEFAULT_DATA_DIR.glob("*.example.json"):
        shutil.copy(example, target / example.name.replace(".example.json", ".json"))
    _SHIPPED_DATA_DIR = target
    return _SHIPPED_DATA_DIR


#: Materialised once per session, for the same reason as :data:`_SHIPPED_DATA_DIR`.
_SHIPPED_TOOL_ROOT: Path | None = None


def shipped_tool_root() -> Path:
    """A tool root as this installation has it — ``config.json`` plus a ``data/`` beside it.

    The few checks whose subject is *the configuration this project ships* need both halves at
    once: `config.json` names `data/store_config.json`, which then has to be there. On an
    operator's checkout that is the repository root and nothing is copied. On a public checkout it
    is a directory built from `config.example.json` and the examples that
    :func:`shipped_data_dir` renames — which is precisely the layout a stranger has after
    following the quickstart, and therefore the one worth checking.
    """
    global _SHIPPED_TOOL_ROOT
    if _SHIPPED_TOOL_ROOT is not None:
        return _SHIPPED_TOOL_ROOT

    import shutil
    import tempfile

    from db_ops.lib.paths import TOOL_ROOT

    if (TOOL_ROOT / "config.json").exists():
        _SHIPPED_TOOL_ROOT = TOOL_ROOT
        return _SHIPPED_TOOL_ROOT

    target = Path(tempfile.mkdtemp(prefix="db_ops_shipped_root_"))
    shutil.copy(TOOL_ROOT / "config.example.json", target / "config.json")
    shutil.copytree(shipped_data_dir(), target / "data")
    _SHIPPED_TOOL_ROOT = target
    return _SHIPPED_TOOL_ROOT


@pytest.fixture
def shipped_metric_catalog(monkeypatch: pytest.MonkeyPatch) -> list[dict]:
    """The metric catalog as this installation ships it, with the report readers pointed at it.

    `server_report.metric_catalog()` and `metric_intervals()` read `data/metric_definitions.json`
    from the operator's data directory and **cache the result in a module global**. Both are
    best-effort by design — a missing catalog yields an empty one rather than raising — so on a
    checkout without that file the coverage section quietly reports no expectation at all, and a
    test asserting on a gap sees an empty set instead of an error. That is the failure this
    fixture removes: it names the file, so an absent one is loud.

    The caches are cleared on the way in *and* out. Left set, they would leak this catalog into
    every later test in the session, which is the kind of order-dependent pass that is only found
    weeks later by running one file on its own.
    """
    from db_ops.reports import server_report

    catalog_path = shipped_config("metric_definitions.json")
    assert catalog_path.exists(), f"no shipped metric catalog at {catalog_path}"

    monkeypatch.setattr(server_report, "METRIC_DEFINITIONS", catalog_path)
    monkeypatch.setattr(server_report, "_METRIC_CATALOG", None)
    monkeypatch.setattr(server_report, "_METRIC_INTERVALS", None)
    catalog = server_report.metric_catalog(catalog_path)
    yield catalog
    server_report._METRIC_CATALOG = None
    server_report._METRIC_INTERVALS = None
