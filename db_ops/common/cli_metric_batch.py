"""``metric-batch`` - plumbing only: :mod:`db_ops.common.metric_batch` does the work."""

from __future__ import annotations

import sys
import time
from typing import Any

from db_ops.lib import response

USAGE = """\
Usage: python -m db_ops.common.cli metric-batch -

Run one target's metric items, one after another, and answer for each. The request arrives on
stdin (-): it carries the target's resolved password and the secret refs it names. Reads no
config - the metrics app decides what is due, builds the items and grades the answers.

  {"target": {"target_id": "ACME-192-0-2-10-MSSQL", "db_type": "sqlserver",
              "host": "192.0.2.10", "port": 1433,
              "database": "",                    // "" = the engine's default (master)
              "service_name": "", "sqlserver_driver": "",
              "username": "monitor", "password": "...",
              "sql_access": {"method": "direct"},  // or the legacy bridge block
              "credential": {...}},              // the bridge builds its connect string from it
   "secrets": {"<ref>": "<value>"},              // only the refs this target names
   "items": [
     {"id": "INSTANCE_STATUS", "kind": "sql", "sql": "SELECT ...", "timeout_seconds": 5,
      "max_rows": 0, "per_database": false, "max_databases": 50},
     {"id": "OS_CPU", "kind": "script", "script": "...", "shell": "bash",
      "access": {"method": "ssh", "host": "...", ...}, "credential": {...},
      "env": {"NAME": "value"}, "timeout_seconds": 10},
     {"id": "DOCKER_STATE", "kind": "local", "path": "/app/.../docker_stats.sh",
      "env": {...}, "timeout_seconds": 10, "require_local_host": false, "host": ""}]}

  data: {"target_id", "items": [{"id", "kind", "started_at",
          "rows" | "databases"+"database_count"+"skipped" | "exit_code"+"stdout"+"stderr"+"duration_seconds",
          "truncated", "error": {"message", "failure_phase", "kind", "stdout", "stderr",
                                 "exit_code", "duration_seconds"}}]}

An item that fails answers with "error"; the batch still succeeds. Only a request that cannot be
read at all fails the command.
"""


def run(argv: list[str], *, read_request: Any) -> int:
    operation = "metric-batch"
    if argv and argv[0] in {"-h", "--help"}:
        print(USAGE)
        return 0
    if not argv or argv[0] != "-":
        print(USAGE, file=sys.stderr)
        return response.emit(response.fail(
            operation, "the request must arrive on stdin (-): it carries a target's password and "
                       "secret values, which inline are visible on the command line"))
    request, code = read_request("-", USAGE)
    if request is None:
        return code

    from db_ops.common import metric_batch
    from db_ops.common.cli_docker_db import _stdout_is_stderr

    started = time.monotonic()
    try:
        # A driver, the bridge or a library that prints must not land inside the JSON answer.
        with _stdout_is_stderr():
            data = metric_batch.run(request)
    except ValueError as exc:
        return response.emit(response.fail(operation, str(exc)))
    except Exception as exc:  # noqa: BLE001 - the caller parses an answer; a traceback is none
        return response.emit(response.fail(operation, f"{type(exc).__name__}: {exc}"))
    failed = sum(1 for item in data["items"] if item.get("error"))
    return response.emit(response.ok(
        operation, message=f"{data['target_id']}: {len(data['items'])} item(s), {failed} failed",
        data=data, metrics={"duration_ms": int((time.monotonic() - started) * 1000),
                            "items": len(data["items"]), "failed": failed}))
