"""The ``common.cli`` gated operations: host facts, services and restarts, the SQL Server patch and instance steps, the emergency actions, ``authorize`` and ``ask``.

Split out of ``common/cli.py`` on 2026-10-03 (Q11,
``audits/20261002_audit_typed_requests_and_errors.md`` section 5). ``cli`` re-exports every
name, so no import changes.
"""

from __future__ import annotations
import sys
from db_ops.common.cli_request import _read_json_request
from db_ops.common.cli_usage import ASK_USAGE


def _gate_command(argv: list[str], usage: str, runner_name: str) -> int:
    """The twelve host/patch/instance commands: one JSON-object contract, one answer shape.

    They share one handler because they share one shape — a JSON request in, a
    :class:`db_ops.common.evidence.GateReport` dict out, exit 0 unless a blocking gate failed.
    Progress goes to **stderr** and only the JSON result to stdout, so an operator can watch a
    30-minute restart happen while a caller still pipes the result into `jq`.

    Sharing the handler is also why they converted to the response envelope in **one edit** on
    2026-08-16 — twelve of the twenty-one commands that were still answering in an ad-hoc
    ``{"ok": …}``. The gate report itself is unchanged and now sits under ``data``.
    """
    from db_ops.lib.secret_text import set_key_env

    source = ""
    config_path = "config.json"
    key = key_base64 = None
    rest = list(argv)
    while rest:
        token = rest.pop(0)
        if token in {"-h", "--help"}:
            print(usage)
            return 0
        if token == "--config":
            config_path = rest.pop(0) if rest else config_path
        elif token == "--key":
            key = rest.pop(0) if rest else None
        elif token in {"--key-base64", "--key_base64"}:
            key_base64 = rest.pop(0) if rest else None
        elif not source:
            source = token
        else:
            print(f"Unexpected argument: {token}\n\n{usage}", file=sys.stderr)
            return 2

    if not source:
        print(usage, file=sys.stderr)
        return 2
    try:
        # Reaching a host needs the OS credential out of the encrypted store, exactly like
        # run-sql needs the database one.
        set_key_env(key, key_base64)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    request, code = _read_json_request(source, usage)
    if request is None:
        return code

    # Nothing here reads the configuration, so there is no data folder to find (rules R09): the
    # request states the host, the login, the policy and the rules. `--config` is still accepted
    # so a command line written before 0.24.0 keeps parsing.
    _ = config_path

    from db_ops.common import (confirm as confirm_gate, host_ops, job_control,
                               sqlserver_emergency, sqlserver_instance, sqlserver_patch)

    runners = {
        "authorize": confirm_gate.authorize_request,
        "host-facts": host_ops.host_facts,
        "host-service": host_ops.service_control,
        "host-restart": host_ops.restart_host,
        "shrink-log": sqlserver_emergency.shrink_log,
        "kill-spid": sqlserver_emergency.kill_spid,
        "start-job": sqlserver_emergency.start_job,
        "disable-job": job_control.disable_job,
        "sqlserver-precheck": sqlserver_patch.precheck,
        "sqlserver-apply-cu": sqlserver_patch.apply_cu,
        "sqlserver-verify-build": sqlserver_patch.verify_build,
        "sqlserver-export-instance": sqlserver_instance.export_instance,
        "sqlserver-replay-instance": sqlserver_instance.replay_instance,
        "sqlserver-verify-instance": sqlserver_instance.verify_instance,
    }
    from db_ops.lib import response

    try:
        outcome = runners[runner_name](
            request, echo=lambda line: print(line, file=sys.stderr, flush=True)
        )
    except Exception as exc:  # noqa: BLE001 - report as a response like every other command.
        return response.emit(response.fail(runner_name, str(exc)))

    # The whole gate report goes under `data`, unchanged: `blockers`, `gates`, `facts`,
    # `evidence_file` and the rest are what an incident review reads, and moving to the envelope
    # must not cost any of them. `GateReport.to_dict()` has an `operation` key of its own — it
    # names the *operation*, this one names the *command* — and nesting keeps both.
    blockers = [str(name) for name in (outcome.get("blockers") or [])]
    target = str(outcome.get("target") or "")
    status = str(outcome.get("status") or "")
    where = f" on {target}" if target else ""
    metrics = outcome.get("counts") if isinstance(outcome.get("counts"), dict) else {}

    if outcome.get("ok"):
        return response.emit(response.ok(
            runner_name, message=f"{runner_name} {status or 'ok'}{where}.",
            data=outcome, metrics=metrics))
    # A blocking gate is *the* reason, and naming it beats "it failed": these commands restart
    # hosts and patch instances, and the next action differs per gate.
    reason = str(outcome.get("error") or "")
    if not reason:
        reason = (f"blocked by {', '.join(blockers)}" if blockers
                  else f"{runner_name} did not succeed{where}.")
    return response.emit(response.fail(
        runner_name, reason, message=f"{runner_name} {status or 'failed'}{where}.",
        data=outcome, metrics=metrics))


def _ask_command(argv: list[str]) -> int:
    """``ask`` - the CLI face of :func:`db_ops.common.confirm.ask_request`. Reads nothing."""
    from db_ops.common import confirm
    from db_ops.lib import response

    source = ""
    rest = list(argv)
    while rest:
        token = rest.pop(0)
        if token in {"-h", "--help"}:
            print(ASK_USAGE)
            return 0
        if not source:
            source = token
        else:
            print(f"Unexpected argument: {token}\n\n{ASK_USAGE}", file=sys.stderr)
            return 2

    request, code = _read_json_request(source or "{}", ASK_USAGE)
    if request is None:
        return code
    try:
        data = confirm.ask_request(request)
    except Exception as exc:  # noqa: BLE001 - report as a response like every other command.
        return response.emit(response.fail("ask", str(exc)))
    if data["answer"]:
        said = f"answered {data['answer']!r}"
    else:
        said = "no terminal to ask on" if not data["interactive"] else "no answer"
    return response.emit(response.ok("ask", message=f"{said}.", data=data))
