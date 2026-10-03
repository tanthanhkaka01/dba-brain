"""The ``common.cli`` commands that reach one host: ``run-cmd``, the file transfers, and ``probe-host``.

Split out of ``common/cli.py`` on 2026-10-03 (Q11,
``audits/20261002_audit_typed_requests_and_errors.md`` section 5). ``cli`` re-exports every
name, so no import changes.
"""

from __future__ import annotations
import sys
from db_ops.common.cli_usage import FILE_TRANSFER_USAGE, PROBE_HOST_USAGE, RUN_CMD_USAGE
from db_ops.common.cli_request import _read_json_request


def _run_cmd_command(argv: list[str]) -> int:
    """``run-cmd`` — run one shell command on a configured host.

    In ``common`` for the same reason ``run-sql`` is: reaching a host is not owned by metrics,
    backup or Telegram, and every one of them needs it. ``remote_exec`` has had the transport all
    along; what was missing was a front door, so the gap was filled by hand-typed ``ssh`` — which
    resolves the target differently every time and leaves no record of what was run.
    """
    from db_ops.lib.secret_text import set_key_env

    source = ""
    config_path = "config.json"
    key = key_base64 = None
    rest = list(argv)
    while rest:
        token = rest.pop(0)
        if token in {"-h", "--help"}:
            print(RUN_CMD_USAGE)
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
            print(f"Unexpected argument: {token}\n\n{RUN_CMD_USAGE}", file=sys.stderr)
            return 2

    if not source:
        print(RUN_CMD_USAGE, file=sys.stderr)
        return 2
    try:
        set_key_env(key, key_base64)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    request, code = _read_json_request(source, RUN_CMD_USAGE)
    if request is None:
        return code

    from db_ops.lib import response

    command = str(request.get("command") or "").strip()
    script = str(request.get("script") or "")
    if bool(command) == bool(script.strip()):
        return response.emit(response.fail(
            "run-cmd", 'give exactly one of "command" (one line) or "script" (text).'))

    from db_ops.common import confirm, host_ops
    from db_ops.lib import result_format

    try:
        fmt = result_format.normalize_format(request.get("format") or "json")
    except result_format.ResultFormatError as exc:
        return response.emit(response.fail("run-cmd", str(exc)))
    if fmt in {"xml", "xlsx"}:
        return response.emit(response.fail(
            "run-cmd", "run-cmd supports json, txt or raw; a command's stdout is not a result "
                       "set. Use run-sql for xml/xlsx."))

    # The host and its login are the request's "access" (rules R09): a bare server_id is refused
    # rather than looked up. `--config` is still accepted so an older command line keeps parsing.
    _ = config_path
    from db_ops.common.evidence import GateReport

    # Gate lines go to stderr so the JSON (or raw stdout) stays machine-readable — the same
    # split every other gate command here uses.
    report = GateReport("run-cmd", echo=lambda line: print(line, file=sys.stderr))
    try:
        target = host_ops.resolve_stated_host(request, what="run-cmd")
        # Same two locks as host-service / host-restart: "confirm": true is the payload declaring
        # intent, typing yes at a terminal is a human confirming they are looking at THIS host.
        allowed = confirm.require_confirmation(
            report,
            request,
            operation="run a shell command",
            target=f"{target.describe()} — {target.host}",
            effects=[(command or script.strip().splitlines()[0])[:200]],
        )
        if not allowed:
            return response.emit(response.fail(
                "run-cmd",
                'not confirmed; run-cmd needs "confirm": true and a typed yes '
                '(or "assume_yes": true when unattended).',
                data={"gates": report.to_dict().get("gates")}))
        session = host_ops.open_host_session(target)
    except Exception as exc:  # noqa: BLE001 - report as a response like every other command.
        return response.emit(response.fail("run-cmd", str(exc)))

    try:
        timeout = request.get("timeout_seconds")
        # "sudo": true routes through the same helper host-service and host-restart use: the
        # password comes from the target's OWN configured credential and goes in on stdin, never
        # into the request and never onto the remote argv where `ps` would show it. Without this
        # the only way to run one privileged command was to write the password into the script.
        # With a container runtime the sudo belongs to the `docker exec` itself, and
        # `wrap_for_runtime` puts it there; run_privileged would sudo the wrong command.
        in_container = target.profile.runtime in {"docker", "k8s"}
        if bool(request.get("sudo", False)) and not in_container:
            outcome = host_ops.run_privileged(
                session, script if script.strip() else command, timeout_seconds=timeout)
        elif script.strip():
            # `runtime: docker|k8s` puts the script inside the container; a plain host is
            # unchanged. This is the half `hostcmd` had and `host_ops` did not, which is why
            # backup-database could reach a containerised engine and run-cmd could not.
            outcome = session.run_script(
                host_ops.wrap_for_runtime(target, script), timeout_seconds=timeout)
        else:
            outcome = session.run(
                host_ops.wrap_for_runtime(target, command), timeout_seconds=timeout)
    except Exception as exc:  # noqa: BLE001
        # No close() here: `finally` runs before the return completes, so adding one would close
        # the session twice.
        return response.emit(response.fail("run-cmd", str(exc)))
    finally:
        session.close()

    if fmt == "raw":
        # stdout verbatim and nothing else, so `| grep` works. stderr still goes to stderr, where
        # a pipeline can ignore it and a person can still see it.
        sys.stdout.write(outcome.stdout or "")
        if outcome.stderr:
            sys.stderr.write(outcome.stderr)
    elif fmt == "txt":
        print(f"{target.describe()}  exit={outcome.exit_code}")
        if outcome.stdout:
            print(outcome.stdout.rstrip("\n"))
        if outcome.stderr:
            print("--- stderr ---", file=sys.stderr)
            print(outcome.stderr.rstrip("\n"), file=sys.stderr)
    else:
        # `host` says which machine; `host_profile` says what that machine *is* and which shell
        # dialect that implies — the same "say what you chose" the run-sql answer grew, and the
        # only way an operator can tell a Server 2012 fact script from a Server 2003 refusal
        # without re-reading the inventory themselves.
        data = {
            "server_id": target.server_id,
            "host": target.host,
            "host_profile": target.to_dict().get("profile"),
            "shell": str(target.access.get("shell") or ""),
            "shell_dialect": target.to_dict().get("shell_dialect"),
            **outcome.to_dict(),
        }
        answer = (response.ok if outcome.ok else response.fail)
        detail = (outcome.stderr or outcome.stdout or "").strip()
        response.emit(
            answer("run-cmd",
                   message=f"exit={outcome.exit_code} on {target.describe()}",
                   data=data, metrics={"exit_code": outcome.exit_code,
                                       "duration_ms": int(round(outcome.duration_seconds * 1000))})
            if outcome.ok else
            answer("run-cmd",
                   f"exit={outcome.exit_code}" + (f": {detail[:400]}" if detail else ""),
                   message=f"exit={outcome.exit_code} on {target.describe()}",
                   data=data, metrics={"exit_code": outcome.exit_code,
                                       "duration_ms": int(round(outcome.duration_seconds * 1000))})
        )
    # **The one command whose exit code is not a summary of its response**, and deliberately so:
    # it passes through the REMOTE command's code. `run-cmd ... ; echo $?` is asking what the
    # command did, not whether db_ops managed to start it — the usage text has promised that since
    # the command existed, and `2` from a remote `grep` means "no match", not "db_ops failed".
    # Everything the envelope rule wants is still true: the answer is one object, `success`
    # mirrors the exit code, and the number is also in `data.exit_code` / `metrics.exit_code`.
    return int(outcome.exit_code or 0)


def _file_transfer_command(argv: list[str], direction: str) -> int:
    """``fetch-file`` / ``send-file`` / ``pack-files`` / ``relay-file`` — the CLI face of
    :mod:`db_ops.common.file_transfer`.

    In ``common`` rather than an app because no app owns it: metrics, backup and the restore
    drills all reach hosts, and "put this file there" is not any one of their jobs. It exists at
    all because the alternative kept being a hand-typed ``scp``, which answers once and takes its
    target resolution and its edge cases with it.
    """
    from db_ops.lib.secret_text import set_key_env

    source = ""
    config_path = "config.json"
    key = key_base64 = None
    rest = list(argv)
    while rest:
        token = rest.pop(0)
        if token in {"-h", "--help"}:
            print(FILE_TRANSFER_USAGE)
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
            print(f"Unexpected argument: {token}\n\n{FILE_TRANSFER_USAGE}", file=sys.stderr)
            return 2

    if not source:
        print(FILE_TRANSFER_USAGE, file=sys.stderr)
        return 2
    try:
        # The login is in the request (rules R09); the key is still taken so a 0.23.0 command line
        # parses, and a password_ref it names may be exported in the environment.
        set_key_env(key, key_base64)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    request, code = _read_json_request(source, FILE_TRANSFER_USAGE)
    if request is None:
        return code
    # The host and its login are the request's "access" (rules R09), so no configuration is read:
    # `--config` is still accepted so an older command line keeps parsing.
    _ = config_path

    from db_ops.common import file_transfer

    runner = {
        "fetch": file_transfer.fetch_file,
        "send": file_transfer.send_file,
        "pack": file_transfer.pack_files,
        "relay": file_transfer.relay_file,
    }[direction]
    from db_ops.lib import response

    command = {"fetch": "fetch-file", "send": "send-file", "pack": "pack-files",
               "relay": "relay-file"}[direction]
    try:
        result = runner(request)
    except Exception as exc:  # noqa: BLE001 - report as a response like every other command.
        return response.emit(response.fail(command, str(exc)))
    # `status` is the fact worth reading first — COPIED / REPLACED / SKIPPED_EXISTS are three
    # different outcomes of a successful transfer, and a caller that only saw "ok" could not
    # tell "I moved it" from "it was already there".
    status = str(result.get("status") or "")
    size = result.get("bytes")
    return response.emit(response.ok(
        command,
        message=f"{command} {status or 'done'}"
                + (f" ({size} bytes)" if isinstance(size, int) else "") + ".",
        data=result,
        metrics={"size_bytes": size} if isinstance(size, int) else {},
    ))


def _probe_host_command(argv: list[str]) -> int:
    """``probe-host`` — the CLI face of :mod:`db_ops.common.host_probe`.

    Nothing is looked up, here or below (rules R09): ``host_probe`` decides what open ports mean,
    and the address it probes is the request's. Until 0.24.0 this function resolved a bare
    server_id to an ip and an OS caption from ``data/``; the app that holds the server_id does that
    now (``lib.data_sources.request_fill``), so the command runs the same on an empty node.
    """
    from db_ops.common import host_probe
    from db_ops.lib import response

    source = ""
    config_path = "config.json"
    rest = list(argv)
    while rest:
        token = rest.pop(0)
        if token in {"-h", "--help"}:
            print(PROBE_HOST_USAGE)
            return 0
        if token == "--config":
            config_path = rest.pop(0) if rest else config_path
        elif not source:
            source = token
        else:
            print(f"Unexpected argument: {token}\n\n{PROBE_HOST_USAGE}", file=sys.stderr)
            return 2

    request, code = _read_json_request(source or "{}", PROBE_HOST_USAGE)
    if request is None:
        return code

    # The address is the request's (rules R09): a server_id alone is refused, never looked up in
    # db_instances.json. `--config` is still accepted so an older command line keeps parsing.
    _ = config_path
    if not str(request.get("host") or "").strip():
        return response.emit(response.fail(
            "probe-host", 'needs "host" (and "port" or "ports") - common.cli reads no configuration '
                          "(rules R09), so a server_id is only the label; the caller states the "
                          "address (db_ops.lib.data_sources.request_fill does it from data/)."))

    try:
        outcome = host_probe.probe(request)
    except host_probe.HostProbeError as exc:
        return response.emit(response.fail("probe-host", str(exc)))
    except Exception as exc:  # noqa: BLE001 - report as a response like every other command.
        return response.emit(response.fail("probe-host", str(exc)))

    ports = outcome["ports"]
    return response.emit(response.ok(
        "probe-host",
        message=f"{outcome['server_id']} ({outcome['host']}): {outcome['verdict']} - {outcome['detail']}",
        data=outcome,
        metrics={"probed": len(ports), "open": len(outcome["open_ports"]),
                 "management_ports": len(outcome["management_ports"])},
    ))
