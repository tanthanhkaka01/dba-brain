"""The Telegram support commands: a command whose action is a CLI - its argv, its request finished from this app's data, its result parsed and masked.

Split out of ``telegram/command_processor.py`` on 2026-10-03 (Q11,
``audits/20261002_audit_typed_requests_and_errors.md`` section 5). ``command_processor`` re-exports every
name, so no import changes.
"""

from __future__ import annotations
from db_ops.lib import errors
import json
import os
import re
import shlex
import subprocess
import sys
from pathlib import Path
from typing import Any, Iterable
from db_ops.lib.config import DEFAULT_CONFIG_PATH, load_config
from db_ops.transport import common_cli
from db_ops.lib.common_cli import build_command, common_invocation
from db_ops.lib.data_sources import request_fill
from db_ops.lib import workflow_steps as ws
from db_ops.lib.process_liveness import (  # noqa: F401 - re-exported, see above
    is_pid_alive as _is_pid_alive,
    is_windows_pid_alive as _is_windows_pid_alive,
    is_zombie as _is_zombie,
    process_start_marker,
    stop_process_tree,
)
from db_ops.lib.telegram_command_text import (  # noqa: F401 - re-exported, see above
    command_key_from_message,
    first_command_token,
    normalize_command_text,
    parse_command_message,
    split_with_verbatim_tail,
    render_command_line,
    split_command_tokens,
    strip_bot_username,
)
from db_ops.db import DbOpsStore
from db_ops.lib.paths import DEFAULT_DATA_DIR, REPO_ROOT, TOOL_ROOT  # noqa: F401 - one definition, see that module
from db_ops.lib.timezone import file_stamp
from db_ops.telegram.command_base import SupportCommand, TelegramCommandError, mask_sensitive_text, mask_sensitive_value
from db_ops.telegram.command_replies import validate_target_ip


def create_sql_run_result_file(
    *,
    store: DbOpsStore,
    config: dict[str, Any],
    values: dict[str, Any],
) -> dict[str, Any]:
    source = str(config.get("source") or "latest_sql_run_result")
    if source != "latest_sql_run_result":
        raise TelegramCommandError(f"Unsupported result_file source: {source}", exit_code=2)
    sql_id = int(config.get("sql_id") or values.get("sql_id") or 0)
    if sql_id <= 0:
        raise TelegramCommandError("result_file.sql_id is required.", exit_code=2)
    sql_run = store.fetch_latest_sql_run_for_sql_id(sql_id=sql_id, status=str(config.get("status") or "done"))
    if sql_run is None:
        raise TelegramCommandError(f"No completed SQL run found for sql_id={sql_id}.", exit_code=1)

    result = json.loads(str(sql_run["result_json"] or "{}"))
    result_text = extract_result_column_text(result, column_name=str(config.get("result_column") or "ResultJson"))
    timestamp = file_stamp()
    output_root = resolve_result_output_dir(str(config.get("output_dir") or "runtime/output/telegram"))
    folder_template = str(config.get("folder_name_template") or "")
    folder_name = ""
    if folder_template:
        folder_name = safe_output_path_component(
            render_template(
                folder_template,
                values
                | {
                    "sql_id": sql_id,
                    "sql_run_id": int(sql_run["sql_run_id"]),
                    "timestamp": timestamp,
                },
            )
        )
    output_dir = (output_root / folder_name).resolve() if folder_name else output_root.resolve()
    if not is_relative_to(output_dir, TOOL_ROOT):
        raise TelegramCommandError(f"Refusing to create result folder outside tools/db_ops: {output_dir}", exit_code=2)
    try:
        output_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise TelegramCommandError(f"Cannot create result folder: {output_dir}: {exc}", exit_code=1) from exc
    file_name = render_template(
        str(config.get("file_name_template") or "sql_task_{sql_id}_{timestamp}.json"),
        values
        | {
            "sql_id": sql_id,
            "sql_run_id": int(sql_run["sql_run_id"]),
            "timestamp": timestamp,
        },
    )
    file_name = safe_output_file_name(file_name)
    if not file_name.lower().endswith(".json"):
        file_name += ".json"
    file_path = (output_dir / file_name).resolve()
    if not is_relative_to(file_path, TOOL_ROOT):
        raise TelegramCommandError(f"Refusing to write result file outside tools/db_ops: {file_path}", exit_code=2)
    file_path.write_text(result_text, encoding="utf-8")
    if bool(config.get("validate_json", True)):
        try:
            json.loads(result_text)
        except json.JSONDecodeError as exc:
            raise TelegramCommandError(f"Result JSON validation failed; raw file kept at {file_path}: {exc}", exit_code=1) from exc
    return {
        "file_path": str(file_path),
        "file_name": file_path.name,
        "folder_name": output_dir.name,
        "folder_path": str(output_dir),
        "file_size": file_path.stat().st_size,
        "sql_id": sql_id,
        "sql_run_id": int(sql_run["sql_run_id"]),
        "row_count": int(sql_run["row_count"] or 0),
    }


def extract_result_column_text(result: dict[str, Any], *, column_name: str) -> str:
    for file_result in result.get("files") or []:
        for result_set in file_result.get("result_sets") or []:
            columns = [str(column) for column in result_set.get("columns") or []]
            if column_name not in columns:
                continue
            column_index = columns.index(column_name)
            rows = result_set.get("rows") or []
            if not rows:
                continue
            row = rows[0]
            if not isinstance(row, list) or len(row) <= column_index:
                continue
            return str(row[column_index] or "")
    raise TelegramCommandError(f"Result column not found in SQL run output: {column_name}", exit_code=1)


def resolve_result_output_dir(value: str) -> Path:
    path = Path(value)
    if path.is_absolute():
        return path
    return (TOOL_ROOT / path).resolve()


def safe_output_file_name(value: str) -> str:
    name = safe_output_path_component(value)
    return name.strip("._") or "db_ops_export.json"


def safe_output_path_component(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", str(value).strip()).strip("._")


def is_relative_to(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent.resolve())
        return True
    except ValueError:
        return False


# `docker compose` writes its progress to stderr — "Network x Creating", "Volume y Created",
# pull percentages — so a compose-driven command's stderr is mostly noise. Reporting the head of
# it as "the error" is what hid the real failure behind a wall of "Volume ... Creating".
_PROGRESS_RE = re.compile(
    r"^(container|volume|network|image|service)\s+\S+\s+"
    r"(creating|created|starting|started|stopping|stopped|removing|removed|recreate|recreated|"
    r"pulling|pulled|waiting|healthy|running|built|building|skipped|interrupted)\b",
    re.IGNORECASE,
)
_PULL_RE = re.compile(r"^\S{12}:\s|(pulling from|downloading|extracting|download complete|"
                      r"pull complete|waiting|verifying checksum|already exists)\b", re.IGNORECASE)
_ERROR_RE = re.compile(r"error|failed|failure|exception|traceback|denied|refused|not found|"
                       r"no such|cannot|unable|conflict", re.IGNORECASE)


def _is_progress_noise(line: str) -> bool:
    return bool(_PROGRESS_RE.match(line) or _PULL_RE.match(line))


def _extract_error_from_output(stderr_text: str, stdout_text: str) -> str:
    """The lines that say what went wrong — not the first lines that happened to be printed.

    Progress chatter is dropped, then the lines that look like an error are preferred, taken
    from the *end* (a failure is reported where it happens, not at the top of the log). Only if
    there are none does it fall back to the tail of whatever was printed.
    """
    for text in (stderr_text, stdout_text):
        lines = [line for line in _meaningful_cli_error_lines(text) if not _is_progress_noise(line)]
        if not lines:
            continue
        error_lines = [line for line in lines if _ERROR_RE.search(line)]
        chosen = (error_lines or lines)[-5:]
        return safe_error_summary(Exception("\n".join(chosen)))
    return "CLI command failed; see runtime logs for details."


def _meaningful_cli_error_lines(text: str) -> list[str]:
    lines = str(text or "").replace("\r\n", "\n").replace("\r", "\n").split("\n")
    return [line.strip() for line in lines if line.strip() and not _is_normal_cli_log_line(line.strip())]


def _is_normal_cli_log_line(line: str) -> bool:
    if line.startswith("[db_ops."):
        return True
    if re.match(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\|LOGGING\|", line):
        return True
    return "|LOGGING|" in line and not any(marker in line.lower() for marker in ("error", "exception", "traceback", "failed"))


class CliCommandError(errors.OperationFailed):
    def __init__(self, message: str, *, exit_code: int, result: dict[str, Any]) -> None:
        super().__init__(message)
        self.exit_code = exit_code
        self.result = result


def _extract_flag_words(
    *, parameters: list[dict[str, Any]], args: list[str]
) -> tuple[list[str], dict[str, str]]:
    """Pull the standalone keyword arguments out of ``args`` before positions are read.

    A parameter declared ``"source": "flag"`` is a word the operator may add anywhere in the
    message (``/spbot_report_hourly_metrics ACME-192-0-2-248 full``). It has to be removed
    *first*, because a ``consume_rest`` positional such as ``target`` would otherwise swallow it
    and hand "ACME-192-0-2-248 full" to the target resolver as one spec.

    Returns the remaining args and ``{name: value}``, where value is the parameter's
    ``present``/``absent`` text - which ``conditional_args`` then turns into real CLI flags.
    """
    flags = [p for p in parameters if str(p.get("source") or "") == "flag"]
    if not flags:
        return list(args), {}
    remaining = list(args)
    values: dict[str, str] = {}
    for parameter in flags:
        name = str(parameter.get("name") or "").strip()
        if not name:
            continue
        words = {str(w).strip().casefold() for w in (parameter.get("flag_words") or []) if str(w).strip()}
        matched = [item for item in remaining if str(item).strip().casefold() in words]
        remaining = [item for item in remaining if str(item).strip().casefold() not in words]
        values[name] = str(parameter.get("present" if matched else "absent") or "")
    return remaining, values


def _default_worker_host(config_path: str | Path | None = None) -> str:
    """The first worker host declared in config.json, or ``""`` when there is none.

    Empty rather than raising: a master-only install has no worker, and a command that does not use
    ``{worker_host}`` must still run there. A command that *does* use it fails on the unresolved
    placeholder, which names the missing setting instead of connecting somewhere unintended.
    """
    try:
        config = load_config(config_path or DEFAULT_CONFIG_PATH)
    except Exception:  # noqa: BLE001 - a command must not fail because config.json is unreadable.
        return ""
    for node in getattr(config, "worker", ()) or ():
        host = getattr(node, "host", "")
        if host:
            return str(host)
    return ""


def cli_action_values(
    *, command: SupportCommand, args: list[str], config_path: str | Path,
    chat_id: str | None = None, user_id: str | None = None,
) -> dict[str, Any]:
    config = dict(command.action_config or {})
    values: dict[str, Any] = dict(config.get("defaults") or {})
    values["config_path"] = str(config_path)
    values["command_text"] = command.command_text
    values["python"] = sys.executable
    # The worker's address belongs to the deployment, not to the command. Writing it into
    # `command_argv` means the shipped catalogue carries somebody else's address - a fresh install
    # got a documentation-range one, which is nobody's worker - and moving the worker means editing
    # every command that named it. `{worker_host}` resolves from config.json, where the cluster is
    # already declared, so the command states *what* it wants and the deployment says where.
    worker_host = _default_worker_host(config_path)
    if worker_host:
        values["worker_host"] = worker_host
    # The chat that asked. A command whose result is a *deliverable* (an xlsx from a SQL task)
    # has to be able to send it back where it was requested; without this the file goes to the
    # target's configured notify chat and the person who ran it never sees it.
    if chat_id:
        values["chat_id"] = str(chat_id)
    # Who asked. A command whose answer is *about the caller* - "what did I run" - cannot take
    # the person as an argument: it would let anyone read anyone's history by typing a number.
    # Always set, even when unknown: an absent key leaves `{user_id}` standing in the argv, and a
    # CLI handed that literal would search for a person by that name and report an empty history
    # rather than saying it does not know who is asking.
    values["user_id"] = str(user_id or "")
    parameters = [dict(item) for item in (config.get("parameters") or config.get("args") or [])]
    args, flag_values = _extract_flag_words(parameters=parameters, args=list(args))
    values.update(flag_values)
    for parameter in parameters:
        parameter = dict(parameter)
        name = str(parameter.get("name") or "").strip()
        if not name:
            continue
        if str(parameter.get("source") or "") == "flag":
            continue  # already resolved above, and it occupies no position
        position = int(parameter.get("position", len(values) + 1))
        value = (
            " ".join(args[position - 1:])
            if bool(parameter.get("consume_rest")) and len(args) >= position
            else args[position - 1] if len(args) >= position
            else ""
        )
        if not ws.ask_when_holds(parameter, ws.answers_by_name(parameters, list(args))):
            # A branch this run did not take. The step is required *inside* its branch and was
            # never asked outside it, so it is neither missing nor answered: it resolves to the
            # step's skip value, which is the `-` every `conditional_args` rule already tests for
            # with `not_equals`. Leaving it empty instead would pass that test and hand the CLI a
            # flag with no value - `--remote-password-ref ''` - which is worse than either.
            values[name] = ws.skip_value(parameter)
            continue
        if bool(parameter.get("required", True)) and str(value).strip() == "":
            raise TelegramCommandError(f"Missing required argument: {name}.", exit_code=2)
        if str(value).strip() == "" and not bool(parameter.get("required", True)):
            # An optional argument that was not typed falls back to its default. Assigning the
            # empty string over it is what broke `/spbot_trace_session` with no argument on
            # 2026-08-12: `"session_id":{session_id}` rendered as `"session_id":,` and the CLI
            # rejected its own payload as malformed JSON.
            #
            # A default may be written in either of two places, and BOTH are read here since
            # 2026-09-18. `action_config.defaults` is the older spelling; `parameters[].default`
            # is the one the prompt flow already uses and the one a reader writes without
            # thinking, next to the parameter it belongs to. Reading only the first is what broke
            # `/spbot_list_sql_runs` the same way three weeks after the comment above was
            # written - "request is not valid JSON: Expecting value: line 1 column 12" - because
            # its two zeros live on the parameters.
            #
            # An optional parameter with no default in either place still resolves to "", which
            # is what `conditional_args` tests with `equals`/`not_equals`.
            if name in values:
                continue
            fallback = parameter.get("default")
            if fallback is not None:
                values[name] = str(fallback)
                continue
        if str(parameter.get("validator") or "") == "target_ip" and str(value).strip():
            value = validate_target_ip(value)
        if str(parameter.get("validator") or "") == "regex" and str(value).strip():
            pattern = str(parameter.get("pattern") or "")
            if not pattern or not re.fullmatch(pattern, str(value), flags=re.IGNORECASE):
                raise TelegramCommandError(
                    str(parameter.get("validation_error") or f"Invalid value for {name}."),
                    exit_code=2,
                )
        values[name] = value
        if str(parameter.get("resolve") or "") == "target" and str(value).strip():
            _inject_resolved_target(values, spec=str(value))
    return values


def _inject_resolved_target(values: dict[str, Any], *, spec: str) -> None:
    """Resolve a unified target spec (server_id or '<db_type> <ip> [port]') and inject the
    canonical ``server_id`` (plus ip/db_type/port) into the CLI value map, so a command can be
    built with ``--server-id {server_id}`` from whichever form the user typed."""
    from db_ops.lib import data_sources as target_resolve

    try:
        instance = target_resolve.resolve_target_instance(spec)
    except target_resolve.TargetResolveError as exc:
        raise TelegramCommandError(str(exc), exit_code=2) from exc
    values["server_id"] = str(instance.get("server_id") or "")
    values["target_ip"] = str(instance.get("ip") or "")
    values["db_type"] = target_resolve.normalize_db_type(instance.get("db_type"))
    port = instance.get("port")
    values["port"] = int(port) if str(port or "").strip().isdigit() else ""


def command_env(config: dict[str, Any], values: dict[str, Any]) -> dict[str, str]:
    """The child process environment, extended with any ``env_from_parameters`` values.

    A secret parameter (a database password) must not be rendered into argv: a command line is
    readable by every process on the host (`ps`) and is stored verbatim in the background-task
    row. Declaring ``"env_from_parameters": {"DB_OPS_NEW_DB_PASSWORD": "password_text"}`` hands
    the value to the CLI through the environment instead."""
    env = os.environ.copy()
    for env_name, parameter_name in (config.get("env_from_parameters") or {}).items():
        value = str(values.get(str(parameter_name), "") or "")
        if value:
            env[str(env_name)] = value
    return env


#: `common.cli` commands this app finishes from its own store, beyond what `request_fill` fills from
#: `data/`: `self-status`'s last-run column is in `job_runs`, which `common` may not open (R04, R09).
FINISHED_FROM_THE_STORE = frozenset({"self-status"})


def finishes(command: str) -> bool:
    """Whether a configured ``common.cli`` action is finished here before it runs."""
    return command in request_fill.FILLS or command in FINISHED_FROM_THE_STORE


def finish_common_request(command: str, request: dict[str, Any], *,
                          store: DbOpsStore | None = None) -> dict[str, Any]:
    """``request`` with what ``common.cli <command>`` needs stated, from this node's ``data/``.

    Since 0.24.0 ``common.cli`` reads no configuration (rules R09): the bot's actions name a
    ``server_id``, and the login, the policy and the confirmation rules behind it are this app's to
    send. The result carries a password, so it goes on stdin (``db_ops.transport``), never argv.
    """
    if command == "self-status":
        return with_last_runs(request, store=store)
    try:
        return request_fill.fill_request(command, request)
    except request_fill.RequestFillError as exc:
        raise TelegramCommandError(str(exc), exit_code=2) from exc


def with_last_runs(request: dict[str, Any], *, store: DbOpsStore | None = None,
                   data_dir: str | Path | None = None) -> dict[str, Any]:
    """``self-status``'s request with each app command's newest run of the last day stated.

    Until 0.24.0 ``/spbot_self_status`` ran ``db.cli self-status``, a second door to the report
    that could read the store; the report is ``common``'s alone now (rules R43), so the bot states
    the column. A store that cannot be read costs the column and says why - never the reply, which
    is most wanted exactly when something is down.
    """
    from datetime import datetime, timedelta, timezone

    from db_ops.db import ops_status

    finished = dict(request)
    if "last_runs" in finished:
        return finished
    try:
        commands = ops_status.load_app_commands(Path(data_dir or DEFAULT_DATA_DIR))
        codes = sorted({str(c.get("app_command_id") or c.get("app_code") or "") for c in commands}
                       - {""})
        store = store or DbOpsStore.from_config(load_config(DEFAULT_CONFIG_PATH))
        since = (datetime.now(timezone.utc) - timedelta(hours=24)).strftime("%Y-%m-%dT%H:%M:%SZ")
        finished["last_runs"] = ops_status.latest_runs(store, codes, since=since)
    except Exception as exc:  # noqa: BLE001 - the column is optional; the report is not.
        first = (str(exc).splitlines() or [type(exc).__name__])[0]
        finished["store_error"] = f"store not read: {first[:160]}"
    return finished


def run_configured_cli_command(*, command: SupportCommand, values: dict[str, Any]) -> dict[str, Any]:
    config = dict(command.action_config or {})
    argv = build_cli_argv(config, values)
    working_dir = resolve_working_dir(str(config.get("working_dir") or "tools/db_ops"))
    timeout_seconds = int(config.get("timeout_seconds") or 1800)
    invocation = common_invocation(argv)
    if invocation is not None and finishes(invocation.command):
        return _run_finished_common_command(invocation.command, invocation.request,
                                            timeout_seconds=timeout_seconds)
    completed = subprocess.run(  # noqa: S603 - argv is built without shell and comes from trusted command config.
        argv,
        cwd=working_dir,
        capture_output=True,
        text=True,
        timeout=timeout_seconds,
        shell=False,
        check=False,
        env=command_env(config, values),
    )
    result = parse_cli_result(stdout=completed.stdout, stderr=completed.stderr)
    result.update(
        {
            "exit_code": completed.returncode,
            "argv": mask_sensitive_value(argv),
            "working_dir": str(working_dir),
        }
    )
    if completed.returncode != 0:
        error_summary = result.get("error_summary") or result.get("stderr") or result.get("stdout") or f"CLI failed with exit code {completed.returncode}"
        raise CliCommandError(str(error_summary), exit_code=completed.returncode, result=sanitized_cli_result(result))
    return sanitized_cli_result(result)


def _run_finished_common_command(command: str, request: dict[str, Any], *,
                                 timeout_seconds: int) -> dict[str, Any]:
    """A configured ``common.cli`` action, its request finished here and sent on stdin.

    Read back exactly as the configured command line was - the same stdout, stderr and exit code -
    so the action's ``success_text: "{stdout}"`` and its failure reply do not change.
    """
    started, why = common_cli.spawn(command, finish_common_request(command, request),
                                    timeout_seconds=timeout_seconds)
    if started is None:
        raise CliCommandError(why, exit_code=1, result={"error_summary": why})
    result = parse_cli_result(stdout=started.stdout, stderr=started.stderr)
    result.update({"exit_code": started.returncode,
                   "argv": list(build_command(command, {}).argv), "working_dir": ""})
    if started.returncode != 0:
        error_summary = (result.get("error_summary") or result.get("stderr") or result.get("stdout")
                         or f"CLI failed with exit code {started.returncode}")
        raise CliCommandError(str(error_summary), exit_code=started.returncode,
                              result=sanitized_cli_result(result))
    return sanitized_cli_result(result)


def build_cli_argv(config: dict[str, Any], values: dict[str, Any]) -> list[str]:
    if isinstance(config.get("command_argv"), list):
        argv = [render_template(str(part), values) for part in config["command_argv"]]
    else:
        template = str(config.get("command_template") or "").strip()
        if not template:
            raise TelegramCommandError("CLI command_template or command_argv is required.", exit_code=2)
        argv = [render_template(part, values) for part in shlex.split(template)]
    for condition in config.get("conditional_args") or []:
        condition = dict(condition)
        parameter = str(condition.get("parameter") or "")
        actual = str(values.get(parameter) or "")
        equals = condition.get("equals")
        not_equals = condition.get("not_equals")
        matches = True
        if equals is not None:
            matches = actual.casefold() == str(equals).casefold()
        if not_equals is not None:
            matches = matches and actual.casefold() != str(not_equals).casefold()
        if matches:
            argv.extend(render_template(str(part), values) for part in condition.get("argv") or [])
    return argv


def resolve_working_dir(value: str) -> Path:
    path = Path(value)
    if path.is_absolute():
        return path
    if value.replace("\\", "/") == "tools/db_ops":
        return TOOL_ROOT
    return REPO_ROOT / path


def parse_cli_result(*, stdout: str, stderr: str) -> dict[str, Any]:
    safe_stdout = mask_sensitive_text(stdout.strip())
    safe_stderr = mask_sensitive_text(stderr.strip())
    parsed = parse_json_from_output(safe_stdout)
    if parsed is None:
        parsed = {}
    parsed.setdefault("stdout", safe_stdout)
    parsed.setdefault("stderr", safe_stderr)
    parsed.setdefault("error_summary", _extract_error_from_output(safe_stderr, safe_stdout))
    return parsed


def parse_json_from_output(text: str) -> dict[str, Any] | None:
    if not text:
        return None
    try:
        data = json.loads(text)
        return dict(data) if isinstance(data, dict) else {"json": data}
    except json.JSONDecodeError:
        pass
    start = text.find("{")
    end = text.rfind("}")
    if start >= 0 and end > start:
        try:
            data = json.loads(text[start : end + 1])
            return dict(data) if isinstance(data, dict) else {"json": data}
        except json.JSONDecodeError:
            return None
    return None


def render_template(template: str, values: dict[str, Any]) -> str:
    """Fill ``{name}`` placeholders. A part that is a JSON object is filled as JSON (B1.5)."""
    stripped = template.strip()
    if stripped.startswith("{") and stripped.endswith("}") and "{" in stripped[1:]:
        rendered = render_json_template(stripped, values)
        if rendered is not None:
            return rendered
    result = template
    for key, value in values.items():
        result = result.replace("{" + key + "}", str(value))
    return result


_PLACEHOLDER = re.compile(r"\{([A-Za-z_][A-Za-z0-9_]*)\}")
_JSON_SCALAR = re.compile(r"-?(0|[1-9][0-9]*)(\.[0-9]+)?|true|false|null")


def render_json_template(template: str, values: dict[str, Any]) -> str | None:
    """Fill a JSON request template so that a value can only ever be a value.

    Plain text substitution let a Telegram user's argument close the string or number it was put
    in and add keys: ``json.loads`` keeps the last duplicate, so ``1,"limit":999999`` or
    ``x","target":"OTHER_SERVER`` overrode what the template fixed (review 0.25.0, B1.5). Here a
    placeholder inside a string is filled with the JSON-escaped text; one standing alone is filled
    with the value if it is a JSON number/true/false/null, else with a quoted string. Returns
    ``None`` when the template is not JSON at all, so the caller keeps the plain behaviour.
    """
    out: list[str] = []
    in_string = escaped = False
    index = 0
    while index < len(template):
        char = template[index]
        match = _PLACEHOLDER.match(template, index) if char == "{" else None
        if match and match.group(1) in values:
            value = str(values[match.group(1)])
            if in_string:
                out.append(json.dumps(value)[1:-1])
            else:
                out.append(value if _JSON_SCALAR.fullmatch(value) else json.dumps(value))
            index = match.end()
            continue
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
        elif char == '"':
            in_string = True
        out.append(char)
        index += 1
    text = "".join(out)
    try:
        json.loads(text)
    except ValueError:
        return None
    return text


def sanitized_cli_result(result: dict[str, Any]) -> dict[str, Any]:
    return mask_sensitive_value(result)


# After masking, a leftover credential looks like a label followed by something that is not
# the mask. Prose *about* passwords ("the password contains '&'") has no such pair, so it is
# not a leak — blanket-blocking on the word alone destroyed exactly the messages that explain
# a password rule to the operator.
_UNMASKED_CREDENTIAL_RE = re.compile(r"(?i)\b(password|token|secret|pwd)\b\s*[:=]\s*(?!\*\*\*)\S+")


def safe_error_summary(error: object, *, secrets: Iterable[str] = ()) -> str:
    """One line describing a failure, with credential *values* removed.

    ``secrets`` are values known to be sensitive for this run (a ``secret: true`` parameter).
    They are removed literally, whatever shape they appear in — the only reliable way to
    scrub a value a child process echoed back in a format no pattern anticipated.
    """
    text = " ".join(str(error).replace("\r", " ").replace("\n", " ").split())
    if not text:
        return "workflow failed"
    for secret in secrets:
        value = str(secret or "")
        if len(value) >= 4:          # too short to redact without mangling ordinary words
            text = text.replace(value, "***")
    masked = mask_sensitive_text(text)
    # Belt and braces: if something still reads as a live credential, say nothing rather
    # than risk it.
    if _UNMASKED_CREDENTIAL_RE.search(masked):
        return "workflow failed; sensitive error detail hidden"
    return masked[:300]
