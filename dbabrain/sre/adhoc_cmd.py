"""``run-cmd-adhoc`` — one shell command on a host named by ``server_id``, the login resolved by the app.

Restarting a lab database container needed a shell command on the lab VM. ``common.cli run-cmd`` runs
one command but takes the host login complete in its request and reads no configuration (rules R09);
``sre ssh`` goes through a bastion that is not reachable from every node. The host's own ``cmd_access``
(``data/db_instances.json``) is what the OS metrics already use. A script would be a missing command,
so this is the command — the host twin of ``sql_tasks run-sql-adhoc``.

**Always asks before running.** A shell command cannot be classified: what it does is what it says.
Every command costs one typed ``yes`` through ``common.cli authorize`` (operation ``run-cmd-adhoc``,
level 50) — the ladder every operation of that weight is priced on. Once authorized, ``run-cmd``
is sent its own ``confirm: true`` and ``assume_yes: true`` so the prompt is not asked a second time.

The answer is ``run-cmd``'s own, passed through: its stdout and stderr as it gave them, and its exit
code. Nothing is written to a runtime store.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, Callable

from dbabrain.lib import errors, response
from dbabrain.lib.json_io import read_json_request_answered
from dbabrain.transport import common_cli

COMMAND = "run-cmd-adhoc"

#: Its name in ``emergency_operations.json``: what running an ad-hoc shell command costs to authorize.
OPERATION = "run-cmd-adhoc"

#: What this command reads itself.
OWN_FIELDS = ("server_id", "command", "script", "confirm", "assume_yes", "reason")
#: Passed to ``run-cmd`` as they are; ``run-cmd`` checks their values.
RUN_CMD_OPTIONS = ("timeout_seconds", "format", "sudo", "runtime")
ACCEPTED = OWN_FIELDS + RUN_CMD_OPTIONS

USAGE = (
    'run-cmd-adhoc {"server_id": "<server_id>", "command": "uptime"}\n'
    "\n"
    "  One JSON object: inline, @file or - (on Windows use @file; single-quoted JSON breaks there).\n"
    "  server_id        the instance in db_instances.json (required)\n"
    "  command | script the shell command or script to run, exactly one\n"
    "  sudo             true to run it through sudo, the password on stdin (run-cmd's own)\n"
    "  runtime          host | docker | k8s. Omitted, an instance with a container_name runs the\n"
    "                   command INSIDE that container; \"host\" runs it on the machine itself\n"
    '  confirm "yes", assume_yes, reason   the answer to the authorization question, asked every time\n'
    "  " + ", ".join(RUN_CMD_OPTIONS) + "   passed to run-cmd as they are\n"
)


def checked(request: dict[str, Any]) -> dict[str, Any]:
    """``request`` checked: every field this command or ``run-cmd`` takes, and nothing else - a
    misspelt ``sudoo`` read as "not asked" would run the command as another login, and say success."""
    unknown = sorted(set(request) - set(ACCEPTED))
    if unknown:
        raise errors.InvalidRequest(f"{COMMAND}: unknown field(s) {', '.join(unknown)}; "
                                f"accepted: {', '.join(ACCEPTED)}.")
    server_id = request.get("server_id")
    if not isinstance(server_id, str) or not server_id.strip():
        raise errors.InvalidRequest(f"{COMMAND}: server_id is required, as a string.")
    for name in ("command", "script", "reason", "format", "runtime"):
        if name in request and not isinstance(request[name], str):
            raise errors.InvalidRequest(f"{COMMAND}: {name} must be a string.")
    if "timeout_seconds" in request:
        timeout = request["timeout_seconds"]
        if not isinstance(timeout, int) or isinstance(timeout, bool) or timeout <= 0:
            raise errors.InvalidRequest(f"{COMMAND}: timeout_seconds must be a positive integer.")
    # `true` is intent and the terminal still asks; the word `yes` is an answer (common.confirm).
    if "confirm" in request and not (isinstance(request["confirm"], str) or request["confirm"] is True):
        raise errors.InvalidRequest(f'{COMMAND}: confirm must be "yes" or true.')
    for name in ("assume_yes", "sudo"):
        if name in request and not isinstance(request[name], bool):
            raise errors.InvalidRequest(f"{COMMAND}: {name} must be true or false.")

    has_cmd = bool(str(request.get("command") or "").strip())
    has_script = bool(str(request.get("script") or "").strip())
    if has_cmd == has_script:
        raise errors.InvalidRequest(f"{COMMAND}: exactly one of command and script.")

    result = dict(request)
    result["server_id"] = server_id.strip()
    return result


def run_cmd_request(request: dict[str, Any]) -> dict[str, Any]:
    """The ``run-cmd`` request, before this node's login is filled in."""
    built: dict[str, Any] = {"target": request["server_id"]}
    if request.get("command"):
        built["command"] = request["command"]
    if request.get("script"):
        built["script"] = request["script"]
    built.update({name: request[name] for name in RUN_CMD_OPTIONS if name in request})
    # Once authorized, run-cmd is told confirm: true and assume_yes: true so it
    # runs unattended without asking a second time (the question was asked here).
    built["confirm"] = True
    built["assume_yes"] = True
    return built


def finished(built: dict[str, Any], *, data_dir: Path) -> dict[str, Any]:
    """``built`` with the login this node states for its target (rules R09)."""
    from dbabrain.lib.data_sources import request_fill

    try:
        return request_fill.fill_request("run-cmd", built, data_dir=data_dir)
    except request_fill.RequestFillError as exc:
        raise errors.NotConfigured(f"{COMMAND}: {exc}") from exc


def lands_in(built: dict[str, Any]) -> str:
    """Where ``run-cmd`` runs the command, in words. Said in the question and the rehearsal because
    it surprised once: an instance with a ``container_name`` runs it INSIDE that container (no
    ``docker`` there, root, the engine's filesystem) unless the request says ``runtime: host``."""
    access = built.get("access") if isinstance(built.get("access"), dict) else {}
    runtime = str(built.get("runtime") or access.get("runtime") or "").strip().lower()
    container = str(access.get("container_name") or access.get("container") or "").strip()
    if not runtime and container:
        runtime = "docker"
    host = str(access.get("host") or built.get("target") or "")
    if runtime == "docker":
        return f"inside container {container or '(none named)'} on {host}"
    if runtime == "k8s":
        return f"inside pod {access.get('pod') or '(none named)'} reached from {host}"
    return f"on the host {host}"


def masked(request: dict[str, Any]) -> dict[str, Any]:
    """``request`` fit to print: every password and key material replaced."""
    shown = json.loads(json.dumps(request, default=str))
    access = shown.get("access")
    if isinstance(access, dict):
        for key in ("password", "passphrase", "key_text", "private_key", "secret"):
            if access.get(key):
                access[key] = "***"
    connection = shown.get("connection")
    if isinstance(connection, dict) and connection.get("password"):
        connection["password"] = "***"
    if isinstance(shown.get("secrets"), dict):
        shown["secrets"] = {name: "***" for name in shown["secrets"]}
    return shown


def _what_runs(request: dict[str, Any]) -> str:
    """One line naming what runs - a script by its first line and its length, as run-cmd's own
    question does, so the line stays one line."""
    command = str(request.get("command") or "").strip()
    if command:
        return f"the command: {command[:200]}"
    lines = str(request.get("script") or "").strip().splitlines()
    return f"the script ({len(lines)} line(s)): {lines[0][:200] if lines else ''}"


def authorize_command(request: dict[str, Any], *, data_dir: Path | None = None, where: str = "",
                      echo: Callable[[str], Any] | None = None) -> bool:
    """One typed ``yes`` before a shell command runs — asked at the terminal, or answered in the
    request, and priced on the ladder of the ``data_dir`` the login was read from."""
    from dbabrain.lib.data_sources import request_fill

    gate: dict[str, Any] = {
        "operation": OPERATION,
        "target_id": request["server_id"],
        "target_label": request["server_id"],
        "effects": [
            "one shell command runs on the host as its configured OS login"
            + (", through sudo" if request.get("sudo") is True else ""),
            "nothing classifies the command: what it does is what it says",
            # The question names what it is about: a yes typed to "run something" is not a review.
            _what_runs(request),
            *([f"it runs {where}"] if where else []),
        ],
        # The field itself is the declared intent; the word `yes` is the separate answer.
        "confirm": (request["confirm"].strip() if isinstance(request.get("confirm"), str) else "")
                   or True,
        "reason": str(request.get("reason") or "ad-hoc shell command"),
    }
    if request.get("assume_yes") is True:
        gate["assume_yes"] = True
    try:
        gate = request_fill.fill_request("authorize", gate, data_dir=data_dir)
        authorized, report, error = common_cli.run_allowing_failure("authorize", gate)
    except (common_cli.CommonCliError, request_fill.RequestFillError) as exc:
        # The gate could not be asked at all: a confirmation that fails open is not one.
        if echo is not None:
            echo(f"[FAIL] confirm: {exc}")
        return False
    if not authorized and echo is not None:
        for line in report.get("gates") or ():
            if str(line.get("status")) != "OK":
                echo(f"[{line.get('status')}] {line.get('name')}: {line.get('detail')}")
        if not (report.get("gates") or ()) and error:
            echo(f"[FAIL] confirm: {error}")
    return bool(authorized)


def main(source: str, *, data_dir: Path, dry_run: bool = False, logger: Any = None,
         log: Callable[..., Any] | None = None) -> int:
    """Run the request in ``source`` (inline JSON, ``@file`` or ``-``); the exit code of the answer."""
    def note(level: str, message: str) -> None:
        if logger is not None and log is not None:
            log(logger, level=level, message=message)

    def stderr(line: str) -> None:
        print(line, file=sys.stderr, flush=True)

    raw, code = read_json_request_answered(source, operation=COMMAND)
    if raw is None:
        return code
    try:
        request = checked(raw)
        built = finished(run_cmd_request(request), data_dir=data_dir)
    except (errors.InvalidRequest, errors.NotConfigured) as exc:
        return response.emit(response.fail(COMMAND, str(exc), kind=exc.kind))

    if dry_run:
        # A rehearsal asks nobody and runs nothing: it shows the request run-cmd would receive,
        # which is how to see which login a server_id resolves to.
        print(json.dumps({"dry_run": True, "runs": lands_in(built), "request": masked(built)},
                         ensure_ascii=False, indent=1))
        return 0

    where = f"server_id={request['server_id']}"
    if not authorize_command(request, data_dir=data_dir, where=lands_in(built), echo=stderr):
        note("logging", f"sre.adhoc_cmd.refused|{where}|reason=not_confirmed")
        # An answer like every other: a caller parsing stdout reads the refusal, not silence.
        return response.emit(response.fail(
            COMMAND, f"{COMMAND} on {request['server_id']} was not authorized. Nothing ran.",
            kind=errors.KIND_REFUSED))

    cmd_len = len(request.get("command") or request.get("script") or "")
    note("logging", f"sre.adhoc_cmd.start|{where}|sudo={request.get('sudo') is True}"
                    f"|cmd_chars={cmd_len}")
    result, error = common_cli.spawn("run-cmd", built)
    if result is None:
        note("error", f"sre.adhoc_cmd.failed|{where}|error={error}")
        return response.emit(response.fail(COMMAND, error))
    # run-cmd's answer as it gave it.
    sys.stdout.write(result.stdout)
    if result.stderr:
        sys.stderr.write(result.stderr)
    note("logging" if result.returncode == 0 else "error",
         f"sre.adhoc_cmd.done|{where}|returncode={result.returncode}")
    return int(result.returncode or 0)
