"""Replaying a SQL Server instance-metadata bundle from named ``.sql`` files.

A SQL Server backup covers user databases only — ``master``, ``msdb`` and ``model`` are excluded —
so every login, server role, permission, credential, linked server and Agent job is absent after a
restore. The bundle is those objects exported as SQL; this applies it.

Files are named by the caller, in order, because that is what makes it usable outside a scheduled
run: an operator holding a bundle directory can apply exactly the parts they want, one at a time,
and watch each land. Ordering is the caller's too — ``logins`` before the databases are restored so
their users are not orphaned, ``agent_jobs`` after, because job steps name databases that must
exist.

Each file is applied whole and its failure is reported per file rather than aborting the rest: a
linked server that cannot be recreated on this network should not cost the logins that could.
"""

from __future__ import annotations

import re
from typing import Any


class MetadataReplayError(ValueError):
    """The replay could not be attempted."""


#: A `.sql` export is a script, and scripts use GO as a batch separator, which the TDS protocol
#: knows nothing about - sending the file whole makes the server reject everything after the first
#: CREATE ... AS. Splitting on it is not optional.
_GO = re.compile(r"^\s*GO\s*(?:--.*)?$", re.IGNORECASE | re.MULTILINE)


def split_batches(script: str) -> list[str]:
    """Split a T-SQL script on its ``GO`` separators, dropping empties."""
    return [part.strip() for part in _GO.split(script) if part.strip()]


def replay(request: dict[str, Any]) -> dict[str, Any]:
    """Apply named ``.sql`` files to a target instance, in the order given."""
    target = request.get("target") or {}
    if not str(target.get("host") or "").strip():
        raise MetadataReplayError("target.host is required.")

    files = request.get("files") or []
    if isinstance(files, str):
        raise MetadataReplayError("files must be an array of paths, in the order to apply them.")
    paths = [str(p).strip() for p in files if str(p).strip()]
    if not paths:
        raise MetadataReplayError("files is required: the .sql exports to apply, in order.")

    host = request.get("host")
    dry_run = bool(request.get("dry_run"))

    scripts: list[tuple[str, str]] = []
    for path in paths:
        scripts.append((path, _read(path, host)))

    if dry_run:
        return {"applied": [], "dry_run": True,
                "files": [{"path": p, "batches": len(split_batches(s))} for p, s in scripts]}

    from db_ops.common.db_connect import connect_engine

    connection = connect_engine(
        db_type="sqlserver", host=str(target["host"]), port=int(target.get("port") or 1433),
        database=str(target.get("database") or "master"),
        username=str(target.get("username") or ""), password=str(target.get("password") or ""),
        # autocommit: sp_configure and RECONFIGURE are refused inside a user transaction (error
        # 574), and a bundle usually contains them.
        autocommit=True, statement_timeout_seconds=0,
    )
    results: list[dict[str, Any]] = []
    try:
        cursor = connection.cursor()
        for path, script in scripts:
            batches = split_batches(script)
            done, error = 0, ""
            for batch in batches:
                try:
                    cursor.execute(batch)
                    while cursor.nextset():
                        pass
                    done += 1
                except Exception as exc:  # noqa: BLE001 - reported per file, never fatal.
                    error = str(exc)[:300]
                    break
            results.append({"path": path, "batches": len(batches), "applied": done,
                            "ok": not error, "error": error or None})
    finally:
        connection.close()

    return {"files": results, "ok": all(r["ok"] for r in results),
            "applied": [r["path"] for r in results if r["ok"]]}


def _read(path: str, host: Any) -> str:
    """Read one export, from this machine or from the host that holds it."""
    if not host:
        from pathlib import Path

        file = Path(path)
        if not file.exists():
            raise MetadataReplayError(f"file not found: {path}")
        return file.read_text(encoding="utf-8-sig")

    import shlex

    from db_ops.common.hostcmd import parse_host, run

    result = run(parse_host(host), f"cat {shlex.quote(path)}")
    if result["exit_code"] != 0:
        raise MetadataReplayError(f"could not read {path} on the host: {result['stderr'][:200]}")
    return result["stdout"]
