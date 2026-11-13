"""How to run one backup or restore entry by hand: the entry's facts and the exact lines, built from
the entry's own configuration (0.38.0, 1.327 / 1.328).

The operator, 2026-11-11: *backup and restore are complex - when a user asks over Telegram, the AI
must answer how to run one by hand, from the CLI and from a Telegram command, for a backup and for
a restore.* An answer written from memory names a job the entry does not have, or the command its
kind of entry refuses: ``restore-latest`` does not run a script-driven entry, ``restore-by-id``
does not run a share-driven one, ``backup --force`` does not run an inactive job, and
``/spbot_backup`` names no job, so on an entry with two it runs both. So the answer is assembled
here, from what ``restore_config.json`` states for that entry, the bot commands this node really
has, the inventory's word for each server and the node's own install - and the assistant relays it.

Three things are decided here and nowhere else:

* **the line** - :func:`node_form` writes it for the node the assistant runs on (``dbabrain
  backup-restore ...`` when pip-installed, ``<that Python> -m dbabrain.backup_restore.cli ...`` in a
  source tree) and, where the estate has a worker, the master's ``control.cli worker-run -- ...``
  form of the same command;
* **what changes** - every step says whether it changes anything, and a step that does says what it
  touches and what stands between the line and its effect (these commands ask no confirmation);
* **who is handed it** (1.328) - :func:`for_asker` takes a changing step's lines away from an asker
  whose Telegram level may not run the bot command that does the same thing on this node, and says
  so in their place. The rule that compares the levels is the bot's own, passed in as ``may_run``.

Pure: dictionaries in, dictionaries out. The Telegram app reads the files and asks the questions
(``telegram.ai_tools``); nothing here opens one, starts a process or knows the time.
"""

from __future__ import annotations

import difflib
import json
import re
import shlex
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from dbabrain.lib import cleanup_retention
from dbabrain.lib.backup_level import BACKUP_LEVEL_BY_ENGINE
from dbabrain.lib.integrity import PRODUCTION_ENVIRONMENTS, production_target_refusal
from dbabrain.lib.restore.kind import is_script_restore
from dbabrain.lib.time_window import time_window_closed_reason, weekdays_text, window_of
from dbabrain.lib.timezone import format_display
from dbabrain.lib.webhost_endpoints import STABLE_PAGES

#: The CLI every line of a runbook starts, and the master's that reaches the worker's container.
BACKUP_RESTORE_MODULE = "dbabrain.backup_restore.cli"
CONTROL_MODULE = "dbabrain.control.cli"
#: The dispatcher's words for the two (``dbabrain/cli.py`` ``APPS``), for a pip install.
BACKUP_RESTORE_WORD = "backup-restore"
CONTROL_WORD = "control"
#: Stated on every line, as the bot's own templates and the scheduled command do: a line that
#: leaves it out resolves the file from an environment variable or the working directory.
CONFIG_ARGS = ("--config", "config.json")
#: Where every line reads the passphrase from. No line carries it, and none carries a placeholder
#: for it: an answer passes through the secret redactor (``lib.redaction``), which takes whatever
#: follows a key flag for a key - a placeholder there came out as a broken line.
KEY_ENV = "DBABRAIN_SECRET_KEY"
#: The form ``--point-in-time`` and the bot's ``point_in_time`` take, as the reader fills it in.
MOMENT_PLACEHOLDER = "YYYY-MM-DD HH:MM:SS +HH:MM"
#: What the bot's restore commands take for "the newest backup".
LATEST = "LATEST"

#: The kinds of bot command a runbook step is held to, and the ``backup_restore.cli`` sub-command
#: each one runs.
GATE_BACKUP, GATE_RESTORE, GATE_COPY_ONLY = "backup", "restore", "copy_only"
LIST_BACKUPS, LIST_RESTORES = "list_backups", "list_restores"

#: A runbook answered in full for at most this many entries of one server; more are listed by id.
MAX_ENTRIES_IN_FULL = 8
#: As much of an entry's own ``note`` as an answer carries: what the operator wrote it is for.
NOTE_CHARS = 240
#: What every command of a runbook asks before it acts. The guards that do stand in the way are
#: listed once, on the runbook (``guards``).
NOTHING_ASKS = "none - the command asks nothing and starts at once; what can refuse it is under guards"


# --------------------------------------------------------------------------- #
# The node the lines are written for
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class Node:
    """How a command line is written where the assistant runs, and whether a worker is reached
    from a master. Built by :func:`node_form` from ``lib.node_environment.describe``."""
    role: str
    shell: str
    cli: tuple[str, ...]
    control: tuple[str, ...]
    #: Whether ``control`` is this node's own form (it is the master) or the general one a worker
    #: can only describe - it does not know the master's Python.
    control_is_exact: bool
    has_worker: bool
    known: bool
    in_container: bool = False


def node_form(environment: Mapping[str, Any] | None, *, node_role: str, has_worker: bool) -> Node:
    """The node's own way to start the two CLIs. An environment that was not read gives the general
    ``python -m ...`` form and ``known=False``, so the answer says the Python is the reader's to name."""
    facts = dict(environment or {})
    role = str(node_role or "").strip().lower() or "master"
    python = str(facts.get("python") or "").strip()
    if facts.get("install") == "pip":
        cli: tuple[str, ...] = ("dbabrain", BACKUP_RESTORE_WORD)
        control: tuple[str, ...] = ("dbabrain", CONTROL_WORD)
    else:
        cli = (python or "python", "-m", BACKUP_RESTORE_MODULE)
        control = (python or "python", "-m", CONTROL_MODULE)
    exact = role != "worker"
    if not exact:
        # Typed on the master, which this node is not: its Python is not this one's.
        control = ("python", "-m", CONTROL_MODULE)
    return Node(role=role, shell=str(facts.get("shell") or ""), cli=cli, control=control,
                control_is_exact=exact, has_worker=bool(has_worker) or role == "worker",
                known=bool(facts.get("install") or python),
                in_container=str(facts.get("runtime") or "host") not in ("", "host"))


_PLAIN_TOKEN = re.compile(r"[A-Za-z0-9_@%+=:,./\\-]+\Z")


def _quoted(token: str) -> str:
    """One argument as a shell takes it. Double quotes serve bash and PowerShell alike for a value
    with spaces; a value holding double quotes (a JSON request) is single-quoted, which only bash
    passes through whole - the caller never writes such a line for another shell."""
    if _PLAIN_TOKEN.match(token):
        return token
    if '"' in token or "$" in token or "`" in token:
        return shlex.quote(token)
    return f'"{token}"'


def render(tokens: tuple[str, ...] | list[str], shell: str = "") -> str:
    """A command line from its arguments, for ``shell``."""
    parts = [_quoted(str(token)) for token in tokens]
    if shell.startswith("powershell") and parts and parts[0].startswith('"'):
        # A quoted path is a string to PowerShell until the call operator says to run it.
        parts.insert(0, "&")
    return " ".join(parts)


def _lines(node: Node, args: list[str], *, bash_only: bool = False) -> dict[str, str]:
    """The step's line on this node and, where a worker is reached from a master, from there."""
    out: dict[str, str] = {}
    if bash_only and node.shell != "bash":
        out["cli_not_written"] = (
            "this command takes its request as inline JSON, which PowerShell does not pass through "
            "whole - on this node use the restore-workflow line of the same step")
    else:
        out["cli"] = render([*node.cli, *args], node.shell)
    if node.has_worker:
        # In the worker's container the image's own `python` runs dbabrain, as the bot's templates
        # and docs/11 write it. No key flag: worker-run opens the worker's SSH login with the
        # passphrase in the master's environment (control._support.resolve_password), and the
        # container holds its own.
        master_shell = node.shell if node.control_is_exact else ""
        if bash_only and master_shell != "bash":
            out["from_master_not_written"] = (
                "inline JSON does not survive the master's shell and the hop to the worker - from the "
                "master use the restore-workflow line of the same step")
        else:
            out["from_master"] = render(
                [*node.control, "worker-run", "--", "python", "-m", BACKUP_RESTORE_MODULE, *args],
                master_shell)
    return out


def node_block(node: Node) -> dict[str, Any]:
    """What the reader must know about the lines before running one."""
    block: dict[str, Any] = {
        "role": node.role,
        "cli": "typed on this node, from its tool root (the folder holding config.json and data/)"
               + ("; this node is a container, so the line is typed inside it - from its host, put "
                  "`docker exec <container>` in front" if node.in_container else "")
               + ("" if node.known else "; how this node is installed was not read - `python` is the "
                                        "Python dbabrain is installed in, never just the one on PATH"),
        "passphrase": f"no line carries the passphrase: each reads it from the environment variable {KEY_ENV}. "
                      "A worker's container holds it already; in any other shell it is set before the line "
                      "is typed (bash: export; PowerShell: $env:). Never write the passphrase in a chat.",
        "order": "the dry run first; read its answer; then the run.",
    }
    if node.has_worker:
        block["from_master"] = (
            "typed on the master, from its tool root: control.cli worker-run runs what follows `--` in the "
            "worker's container, where the scheduled entries run. It opens the worker's SSH login with the "
            f"passphrase in the master's {KEY_ENV}, and asks for the SSH login's own when that is not set."
            + ("" if node.control_is_exact else
               " `python` there is the master's own - its venv's in a source tree, or `dbabrain control "
               "worker-run ...` when pip-installed."))
        if node.role != "worker":
            block["which_to_use"] = ("this node is the master: the scheduled entries run on the worker, so use "
                                     "the from_master line; the cli line would run the command from this node.")
    return block


# --------------------------------------------------------------------------- #
# The bot commands that do the same thing
# --------------------------------------------------------------------------- #
def bot_twins(commands_document: Any) -> dict[str, dict[str, Any]]:
    """The bot commands of this node that run ``backup_restore.cli``, by what they do.

    Found by what a command **runs** (its ``command_argv``), never by its name: a node may rename
    ``/spbot_backup`` or switch it off, and the level that decides who may run a backup is that
    command's ``command_type`` on *this* node. The first command of each kind is the one taken.
    """
    records = (commands_document or {}).get("telegram_support_commands") \
        if isinstance(commands_document, dict) else None
    twins: dict[str, dict[str, Any]] = {}
    for record in records or []:
        if not isinstance(record, dict):
            continue
        action = record.get("action_config") if isinstance(record.get("action_config"), dict) else {}
        argv = [str(item) for item in action.get("command_argv") or []]
        if BACKUP_RESTORE_MODULE not in argv:
            continue
        rest = argv[argv.index(BACKUP_RESTORE_MODULE) + 1:]
        sub = next((item for item in rest if not item.startswith("-") and not item.startswith("{")), "")
        conditional = [str(item) for block in action.get("conditional_args") or [] if isinstance(block, dict)
                       for item in block.get("argv") or []]
        kind = {"backup": GATE_BACKUP, "list-backups": LIST_BACKUPS, "list-restores": LIST_RESTORES,
                "restore-workflow": GATE_COPY_ONLY if "--copy-only" in rest else GATE_RESTORE}.get(sub)
        if kind is None or kind in twins:
            continue
        parameters = [dict(item) for item in action.get("parameters") or []
                      if isinstance(item, dict) and (item.get("name") or item.get("key"))
                      and item.get("source", "arg") == "arg"]
        try:
            level = int(record.get("command_type", 0))
        except (TypeError, ValueError):
            level = -1  # a level that cannot be read is nobody's to run
        twins[kind] = {
            "command": "/" + str(record.get("command_text") or "").lstrip("/"),
            "level": level,
            "in_group": bool(record.get("is_group")), "in_private_chat": bool(record.get("is_private")),
            "parameters": sorted(parameters, key=lambda item: int(item.get("position") or 0)),
            "runs": " ".join(rest), "names_a_job": "--job" in rest,
            "takes_a_level": "--backup-type" in conditional or "--backup-type" in rest,
            "takes_a_moment": "--point-in-time" in conditional or "--point-in-time" in rest,
        }
    return twins


def _bot_line(twin: dict[str, Any], values: dict[str, str]) -> str:
    """The twin on one line, its arguments in position order: a value this module knows, a
    ``<name>`` for a required one it does not, and nothing from the first optional one it does not."""
    tokens = [twin["command"]]
    for parameter in twin["parameters"]:
        name = str(parameter.get("name") or parameter.get("key"))
        if name in values:
            tokens.append(values[name])
        elif parameter.get("required"):
            tokens.append(f"<{name}>")
        else:
            break
    return " ".join(tokens)


def _bot_confirmation(twin: dict[str, Any]) -> str:
    asked = [f"{parameter.get('name')} ({parameter.get('pattern') or 'typed'})"
             for parameter in twin["parameters"] if "confirm" in str(parameter.get("name") or "").lower()]
    if asked:
        return "the bot asks " + ", ".join(asked) + " before it starts"
    return ("none - it starts when the message is sent; given without its arguments, the bot asks for "
            "each one first")


def _bot_where(twin: dict[str, Any]) -> str:
    places = [text for flag, text in ((twin["in_private_chat"], "a private chat with the bot"),
                                      (twin["in_group"], "a group cleared to that level")) if flag]
    return " or ".join(places) or "no chat: the command is allowed in neither a group nor a private chat"


def _bot_block(twin: dict[str, Any] | None, line: str, *, runs: str, note: str = "") -> dict[str, Any]:
    if twin is None:
        return {"available": False,
                "why": "this node's telegram_support_commands.json has no command that runs it"}
    block = {"available": True, "line": line, "command": twin["command"], "level": twin["level"],
             "where": _bot_where(twin), "runs": runs, "confirmation": _bot_confirmation(twin)}
    if twin["level"] < 0:
        block["switched_off"] = "its level is below 0 on this node: nobody runs it from Telegram"
    if note:
        block["note"] = note
    return block


# --------------------------------------------------------------------------- #
# Reading an entry
# --------------------------------------------------------------------------- #
def _text(value: Any) -> str:
    return str(value or "").strip()


def _environment(server_id: str, environments: Mapping[str, str]) -> str:
    if not server_id:
        return ""
    if server_id not in environments:
        return "not in this node's inventory"
    return _text(environments[server_id]) or "not stated in the inventory"


def _is_production(server_id: str, environments: Mapping[str, str]) -> bool:
    return _text(environments.get(server_id)).lower() in PRODUCTION_ENVIRONMENTS


def _schedule(record: dict[str, Any], now: datetime | None) -> dict[str, Any]:
    """The record's window in words, and whether it is open at ``now`` (the node's own clock)."""
    window = window_of(record)
    if window is None:
        return {"window": "none that parses - the scheduler names why where it runs"}
    parts = []
    if window.from_hour is not None or window.to_hour is not None:
        start = 0 if window.from_hour is None else window.from_hour
        end = 23 if window.to_hour is None else window.to_hour
        parts.append(f"hours {start:02d}-{end:02d}")
    else:
        parts.append("any hour")
    if window.weekdays is not None:
        parts.append(f"on {weekdays_text(window.weekdays)}")
    if window.repeat_interval:
        parts.append(f"every {window.repeat_interval} s")
    out: dict[str, Any] = {"window": ", ".join(parts) + " (the node's configured timezone)"}
    if window.timeout:
        out["timeout_seconds"] = window.timeout
    if now is not None:
        closed = time_window_closed_reason(window, now)
        out["open_now"] = not closed
        # In the clock it was given - the node's own, which is the one the window is judged on -
        # and in the one form a person reads anywhere here (`... +07`).
        out["now"] = format_display(now, now.tzinfo)
        if closed:
            out["closed_because"] = closed
    return out


def _notify(*records: Any) -> dict[str, str]:
    """Where a run's messages go: the first record that states a rule decides it (a job over its entry)."""
    out: dict[str, str] = {}
    for rule, label in (("logging_on_run", "each run"), ("alert_on_error", "an error")):
        for record in records:
            block = (record or {}).get("notify") if isinstance(record, dict) else None
            stated = block.get(rule) if isinstance(block, dict) else None
            if isinstance(stated, dict):
                if stated.get("enabled") is False:
                    out[label] = "not sent (the rule is off)"
                else:
                    chat = _text(stated.get("telegram_chat")) or _text(stated.get("chat_id"))
                    out[label] = f"the chat of level {chat}" if chat else "the chat of the message's own level"
                break
        else:
            out[label] = "the chat of the message's own level (no notify stated)"
    return out


def _scope(job: dict[str, Any], engine: str) -> str:
    names = [str(name) for name in job.get("databases") or [] if _text(name)]
    if names:
        return "database(s) " + ", ".join(names)
    if engine == "sqlserver":
        return "every user database" if job.get("all_databases") else "the databases its script takes"
    return "the whole cluster" if engine == "postgresql" else "the whole instance"


def _level(job: dict[str, Any]) -> str:
    env = job.get("env") if isinstance(job.get("env"), dict) else {}
    return _text(env.get("BACKUP_LEVEL"))


#: The jobs that only ship the engine's log stream, by the name every entry gives them. Such a job
#: has no full / diff choice, so ``--backup-type`` is never offered for it - `list-backups` reads
#: the same names for the same reason, and imports them from here.
LOG_STREAM_JOBS = ("archivelog", "wal", "log")
#: The same, read from what a job writes, for an entry that names its job otherwise.
LOG_STREAM_TYPES = frozenset({"LOG", "WAL", "ARCHIVELOG"})


def _is_log_stream(job: dict[str, Any], engine: str) -> bool:
    if _text(job.get("job")).lower() in LOG_STREAM_JOBS:
        return True
    written = cleanup_retention.types_a_job_writes(engine, job)
    return bool(written) and set(written) <= LOG_STREAM_TYPES


def _repository(entry: dict[str, Any]) -> dict[str, Any]:
    """The entry's ``offload_*`` fields as the flow reads them."""
    if "offload_enabled" not in entry:
        return {"enabled": False, "why": "the entry states no offload fields (upgrade-config writes them)"}
    enabled = entry.get("offload_enabled") is True
    out: dict[str, Any] = {"enabled": enabled}
    if not enabled:
        out["why"] = "offload_enabled is false: the files stay on the server, and `offload` has nothing to do"
        return out
    share = _text(entry.get("offload_source_share"))
    transfer = _text(entry.get("offload_transfer")) or "cat"
    out.update({
        "server_id": _text(entry.get("offload_server_id")),
        "folder": _text(entry.get("offload_folder")),
        "source_read": (f"over its share {share} (SMB, as {_text(entry.get('offload_source_username')) or '?'})"
                        if share else "over SSH, by SFTP (offload_transfer sftp)" if transfer == "sftp"
                        else "over SSH, a cat channel (offload_transfer cat)"),
        "encrypt": entry.get("offload_encrypt") is True,
        "delete_at_the_source": entry.get("offload_delete_source") is True,
        "source_keeps_newest_chain": entry.get("offload_source_keeps_newest_chain") is not False,
        "files_per_pass": entry.get("offload_files_per_pass"),
        "copy_max_retry": entry.get("offload_copy_max_retry"),
        "repository_check": entry.get("offload_repository_check") is not False,
        "retention": entry.get("offload_retention") if entry.get("offload_retention") else
                     "none stated: the repository keeps every copy",
    })
    since = _text(entry.get("offload_files_since"))
    if since:
        out["only_files_since"] = f"{since} UTC - older files stay their server's own"
    return out


def _backup_flow(entry: dict[str, Any], repository: dict[str, Any]) -> list[dict[str, str]]:
    """The flow of this entry, step by step, each on or off as the entry states it.

    ``message`` is the step's number in the run's Telegram messages (``<backup_id> · 1. Backup ...``,
    ``<backup_id> · 2-5. Repository ...``), so a reader can match what they see to what ran.
    """
    server, folder = _text(entry.get("server_id")), _text(entry.get("backup_dir"))
    on = bool(repository.get("enabled"))
    off = "off - offload_enabled is false"
    if not on:
        return [
            {"step": "Backup", "message": "1. Backup", "command": "backup", "state": "on",
             "does": f"the job's script writes the backup into {folder} on {server}; every file is hashed there "
                     "and recorded in the catalog (backup_runs, backup_files)"},
            {"step": "Copy to the repository, verify, encrypt, verify, delete at the source, repository check",
             "message": "2-5. Repository", "command": "offload", "state": off,
             "does": "nothing: the backup stays on the server, and `offload` has nothing to do for this entry"},
        ]
    where = f"{repository.get('server_id')}:{repository.get('folder')}/{server}/" if on else ""
    encrypt = on and bool(repository.get("encrypt"))
    delete = on and bool(repository.get("delete_at_the_source"))
    return [
        {"step": "Backup", "message": "1. Backup", "command": "backup", "state": "on",
         "does": f"the job's script writes the backup into {folder} on {server}; every file is hashed there and "
                 "recorded in the catalog (backup_runs, backup_files)"},
        {"step": "Copy to the repository", "message": "2. Copy", "command": "offload", "state": "on" if on else off,
         "does": (f"each catalogued file without a proven copy goes to {where}, oldest first, at most "
                  f"{repository.get('files_per_pass')} a pass, the server read {repository.get('source_read')}; "
                  "the source is hashed first and a backup that changed since it was written is never copied")
         if on else "nothing: the files stay on the server"},
        {"step": "Verify the copy", "message": "2. Copy", "command": "offload", "state": "on" if on else off,
         "does": (f"the copy is held to the server's bytes and SHA-256; a difference is copied again, up to "
                  f"{repository.get('copy_max_retry')} time(s), then the row is failed and its source kept")
         if on else "nothing"},
        {"step": "Encrypt", "message": "3. Encrypt", "command": "offload",
         "state": "on" if encrypt else ("off - offload_encrypt is false" if on else off),
         "does": ("the file is encrypted with the server's repository key (age) as it is copied, and lands on "
                  "the repository only as <file>.age - the repository cannot decrypt what it holds")
         if encrypt else "nothing: the copy stays plain on the repository"},
        {"step": "Verify the encryption", "message": "3. Encrypt", "command": "offload",
         "state": "on" if encrypt else ("off - offload_encrypt is false" if on else off),
         "does": "the .age is streamed back and decrypted; its plaintext must be the backup's bytes and SHA-256"
         if encrypt else "nothing"},
        {"step": "Delete at the source", "message": "4. Delete at the source", "command": "offload",
         "state": "on" if delete else ("off - offload_delete_source is false" if on else off),
         "does": ("a file whose repository copy is proven" + (" and wholly encrypted" if encrypt else "")
                  + f" is deleted on {server}, one verified file per command, after the copy is hashed again; "
                  + ("the newest chain of each database stays on the server"
                     if repository.get("source_keeps_newest_chain") else
                     "every file goes, the newest chain too (offload_source_keeps_newest_chain is false) - the "
                     "server then holds no restorable backup")
                  + "; it waits while a lockdown is on")
         if delete else "nothing: the backup stays on the server"},
        {"step": "Repository check", "message": "5. Repository check", "command": "offload",
         "state": "on" if on and repository.get("repository_check") else
                  ("off - offload_repository_check is false" if on else off),
         "does": f"{where} is listed against the catalog: a stray, missing or changed file fails the run"
         if on else "nothing"},
    ]


def _watch(*, kind: str, entry_id: str, notify: dict[str, str], report_base: str,
           jobs: list[str] | None = None) -> list[str]:
    page = STABLE_PAGES["backup_restore"]
    url = (report_base.rstrip("/") + "/" + page) if report_base else ""
    lines = []
    if kind == "backup":
        lines.append(f"the run's messages in Telegram, named by the entry: `{entry_id} · 1. Backup <job> ...`, then "
                     f"`{entry_id} · 2-5. Repository ...` - each run to {notify['each run']}, an error to "
                     f"{notify['an error']}")
        lines.append("the store: job_runs (job_code ending ." + entry_id + ".<job>"
                     + (f", jobs {', '.join(jobs)}" if jobs else "") + "; status RUNNING, DONE or ERROR, "
                     "error_text), and the catalog - backup_runs and backup_files by backup_id")
        lines.append("the log on the node that ran it: logs/backup*.log")
    else:
        lines.append(f"the run's messages in Telegram, each naming restore_id={entry_id}: started, copy "
                     f"started / finished, restore started / finished, verify, delete, then finished or FAILED - "
                     f"each run to {notify['each run']}, an error to {notify['an error']}")
        lines.append("the store: job_runs rows carrying this restore_id, and the catalog - restore_runs and "
                     "restore_run_files by restore_id")
        lines.append("the log on the node that ran it: logs/restore_workflow*.log (restore-workflow), "
                     "logs/backup_restore*.log (restore-by-id, restore-latest)")
    lines.append(f"the report page {page}" + (f": {url}" if url else
                                               " - the console's Reports link `Backup & restore` (no "
                                               "published address is configured on this node)"))
    return lines


# --------------------------------------------------------------------------- #
# A backup entry
# --------------------------------------------------------------------------- #
def backup_runbook(entry: dict[str, Any], *, node: Node, twins: Mapping[str, dict[str, Any]],
                   environments: Mapping[str, str], now: datetime | None = None,
                   report_base: str = "") -> dict[str, Any]:
    """How to run one ``backup_restore.backups[]`` entry by hand."""
    backup_id = _text(entry.get("backup_id"))
    engine = _text(entry.get("db_type")).lower()
    server = _text(entry.get("server_id"))
    entry_active = entry.get("active", True) is not False
    repository = _repository(entry)
    words = sorted(BACKUP_LEVEL_BY_ENGINE.get(engine, {}))
    production = _is_production(server, environments)

    jobs: list[dict[str, Any]] = []
    steps: list[dict[str, Any]] = []
    for raw in entry.get("jobs") or []:
        if not isinstance(raw, dict) or not _text(raw.get("job")):
            continue
        name = _text(raw.get("job"))
        active = entry_active and raw.get("active", True) is not False
        level, scope, schedule = _level(raw), _scope(raw, engine), _schedule(raw, now)
        stream = _is_log_stream(raw, engine)
        retention = cleanup_retention.describe_config(
            cleanup_retention.retention_source(raw, entry).get(cleanup_retention.FIELD))
        jobs.append({"job": name, "active": active,
                     "level": (f"{level} - the engine's log stream, no full / diff choice" if stream and level
                               else "the engine's log stream, no full / diff choice" if stream
                               else f"{level} (stated by the job)" if level
                               else "the script's own choice (no BACKUP_LEVEL stated)"),
                     "takes": scope, "script": _text(raw.get("script")), "cleanup_retention": retention,
                     "notify": _notify(raw, entry), **schedule})
        select = ["backup", *CONFIG_ARGS, "--backup-id", backup_id, "--job", name, "--force"]
        steps.append({
            "step": f"Dry run of job {name}", "kind": "dry_run", "changes": False,
            "does": "lists the job with status dry-run and starts nothing on the server (it reads the store, "
                    "and closes a run of this entry left RUNNING past its timeout, as every pass does)"
                    + ("" if active else "; this job is inactive, so it answers due 0"),
            **_lines(node, [*select, "--dry-run"])})
        outside = schedule.get("open_now") is False
        steps.append({
            "step": f"Run job {name} now", "kind": "run", "changes": True, "gate": GATE_BACKUP,
            "touches": (
                f"writes a new backup ({'the log stream' if stream else level or 'level chosen by the script'}) "
                f"of {scope} on {server} into "
                f"{_text(raw.get('backup_dir')) or _text(entry.get('backup_dir'))}; --force skips the job's "
                f"schedule ({schedule['window']})"
                + (f" - the window is closed now ({schedule.get('closed_because')})" if outside else "")
                + (f"; {server} is a production instance in the inventory"
                   + (", so the backup's reads run beside the day's work" if outside else "") if production else "")
                + f"; the engine's own retention then applies to that folder (cleanup_retention {retention})"
                + ("; the next offload pass then copies the new files to the repository"
                   + (", encrypts them" if repository.get("encrypt") else "")
                   + (" and deletes them at the source" if repository.get("delete_at_the_source") else "")
                   if repository.get("enabled") else "")),
            "confirmation": NOTHING_ASKS,
            **({"will_not_run": "this job is inactive: `backup --force` leaves it out. `entry-active` switches "
                                "an entry on; a job's own active flag is edited in restore_config.json."}
               if not active else {}),
            # Only where the script derives the level: a job that states one, or ships the log
            # stream, has no level for a word to force (what `list-backups` says of them too).
            **({"level_words": f"add --backup-type <{'|'.join(words)}> to force the level for this run "
                               f"({engine} takes: {', '.join(words)}); without it the script decides"}
               if words and not level and not stream else {}),
            **_lines(node, select)})

    names = [job["job"] for job in jobs]
    twin = twins.get(GATE_BACKUP)
    telegram = _bot_block(
        twin, _bot_line(twin, {"backup_id": backup_id, "backup_type": "-"}) if twin else "",
        runs=f"backup --backup-id {backup_id} --force" + (
            "" if twin is None or twin["names_a_job"] or len(names) < 2 else
            f" - it names no job, so it runs EVERY active job of the entry ({', '.join(names)}), one after "
            "the other; the CLI line with --job runs one"),
        note=("the last word is the level: - lets each job take its own, as a scheduled run does; "
              f"{' | '.join(words)} forces that level for this run"
              if twin and twin["takes_a_level"] and words else ""))

    if repository.get("enabled"):
        offload = ["offload", *CONFIG_ARGS, "--backup-id", backup_id]
        steps.append({"step": "Dry run of the repository pass", "kind": "dry_run", "changes": False,
                      "does": "answers how many files the pass would copy, release at the source, expire on the "
                              "repository and encrypt (to_copy, to_release, to_expire, to_encrypt) and does none "
                              "of it; an entry with nothing to do is left out of the answer",
                      **_lines(node, [*offload, "--dry-run"])})
        steps.append({
            "step": "Run the repository pass now", "kind": "run", "changes": True, "gate": GATE_BACKUP,
            "touches": (f"copies the entry's catalogued files to {repository['server_id']}:{repository['folder']}/"
                        f"{server}/ and proves each copy"
                        + (", encrypted as .age" if repository["encrypt"] else "")
                        + (f"; then DELETES on {server} every file whose copy is proven"
                           + ("" if repository["source_keeps_newest_chain"] else ", the newest chain too")
                           if repository["delete_at_the_source"] else "; deletes nothing on the server")
                        + "; deletes repository copies past offload_retention, when one is stated"),
            "confirmation": NOTHING_ASKS,
            "note": "the scheduled workflow runs this pass by itself after every backup; by hand it only "
                    "brings it forward. A hand run takes at most 100 files of the entry (--limit N for another "
                    "number).",
            **_lines(node, offload)})
    steps.append({
        "step": "Check the files are still there and unchanged", "kind": "check", "changes": False,
        "does": f"re-hashes the entry's catalogued files on {server} and holds each to the checksum its backup "
                "recorded (on_disk, checksum_matches in the catalog); it changes nothing on the server but "
                "reads every byte of up to 500 files, the ones checked longest ago first (--limit N for fewer); "
                "exit code 1 when a file changed or is gone",
        **_lines(node, ["verify-backup-files", *CONFIG_ARGS, "--backup-id", backup_id])})
    steps.append({
        "step": "Stop a run of this entry that is running", "kind": "stop", "changes": True, "gate": GATE_BACKUP,
        "touches": f"ends the running backup of {backup_id} on {server}: its script's process tree first, then "
                   "(SQL Server) every session the run opened; the run is recorded as stopped by the operator. "
                   "Never KILL the session alone - the script then starts the next database",
        "confirmation": "none - it stops at once; with nothing running it says so and changes nothing.",
        **_lines(node, ["backup-stop", *CONFIG_ARGS, "--backup-id", backup_id])})
    guards = ["--force skips the job's schedule - its window and its interval - and nothing else",
              "a run of the same job still in flight is never doubled: the second answers already running",
              "an inactive job, or a job of an inactive entry, is never run, forced or not"]
    if repository.get("enabled"):
        guards.append("one repository pass per entry at a time: a second is skipped")
        if repository.get("delete_at_the_source"):
            guards.append("the delete at the source waits while a lockdown is on; the copy goes on")

    return {
        "kind": "backup", "backup_id": backup_id, "db_type": engine, "server_id": server,
        "server_environment": _environment(server, environments), "active": entry_active,
        "backup_dir": _text(entry.get("backup_dir")), "note": _text(entry.get("note"))[:NOTE_CHARS],
        "jobs": jobs, "repository": repository,
        "flow": _backup_flow(entry, repository),
        "flow_commands": "`backup` runs step Backup; `offload` runs every later step in one pass; the scheduled "
                         "`workflow` (the APP-BACKUP-RESTORE job) runs the due backups, then the due restores, "
                         "then the repository pass.",
        "steps": steps, "guards": guards, "telegram": telegram,
        "list": _list_block(node, twins.get(LIST_BACKUPS), "list-backups"),
        "watch": _watch(kind="backup", entry_id=backup_id, report_base=report_base, jobs=names,
                        notify=jobs[0]["notify"] if jobs else _notify(entry)),
    }


def _list_block(node: Node, twin: dict[str, Any] | None, sub: str) -> dict[str, Any]:
    block: dict[str, Any] = {"does": "lists the configured ids (active ones; the rest are counted)",
                             "cli": render([*node.cli, sub, *CONFIG_ARGS], node.shell)}
    if twin is not None and twin["level"] >= 0:
        block["telegram"] = twin["command"]
    return block


# --------------------------------------------------------------------------- #
# A restore entry
# --------------------------------------------------------------------------- #
def _restore_databases(entry: dict[str, Any], *, script: bool, engine: str) -> str:
    mappings = [item for item in entry.get("database_mappings") or [] if isinstance(item, dict)]
    if mappings:
        return "; ".join(f"{_text(item.get('source_database'))} -> "
                         f"{_text(item.get('target_database')) or _text(item.get('source_database'))}"
                         for item in mappings)
    if entry.get("restore_all_databases") is True:
        return "every database whose FULL is on the share, each under its backup's own name"
    if script and engine in ("postgresql", "oracle", "mysql"):
        return "the whole cluster" if engine == "postgresql" else "the whole instance"
    return "the databases of the staged backup chain"


def restore_runbook(entry: dict[str, Any], *, node: Node, twins: Mapping[str, dict[str, Any]],
                    environments: Mapping[str, str], now: datetime | None = None,
                    report_base: str = "") -> dict[str, Any]:
    """How to run one ``backup_restore.restores[]`` entry by hand - by the commands its kind takes."""
    restore_id = _text(entry.get("restore_id"))
    script = is_script_restore(entry)
    engine = _text(entry.get("db_type")).lower() or ("" if script else "sqlserver")
    source = _text(entry.get("server_id"))
    target = _text(entry.get("target_server_id"))
    in_place = script and (not target or target == source)
    restore_from = _text(entry.get("restore_from")).lower() or "not stated"
    action = _text(entry.get("action")).lower() or "not stated"
    overwrite = entry.get("overwrite_existing") is True
    databases = _restore_databases(entry, script=script, engine=engine)
    container = _text(entry.get("target_container")) or _text((entry.get("target") or {}).get("sql_container")
                                                             if isinstance(entry.get("target"), dict) else "")
    staging = _text(entry.get("target_backup_dir")) or _text(
        (entry.get("target") or {}).get("vm_import_linux_path") if isinstance(entry.get("target"), dict) else "")
    retention = cleanup_retention.describe_config(entry.get(cleanup_retention.FIELD))
    metadata = isinstance(entry.get("server_metadata"), dict) and entry["server_metadata"].get("enabled") is True
    refusal = production_target_refusal(environments.get(target), allowed=entry.get("allow_production_target") is True,
                                        restore_id=restore_id, target=target)
    where = target or source
    if script:
        reads = ("the repository's proven copies of the server's backups (.age copies are decrypted on the way)"
                 if restore_from == "repository" else f"the backup folder of {source}")
        copies = "nothing is copied: the target reads the source's own folder" if in_place else \
            f"the backup chain is copied from {reads} to {staging or 'the staging folder'} on {where}, every " \
            "file proven by checksum before it is used"
    else:
        share = _text((entry.get("source") or {}).get("backup_share") if isinstance(entry.get("source"), dict) else "")
        reads = f"the share {share}" if share else f"the backup share of {source}"
        copies = (f"after the space check, the backup files are copied from {reads} to "
                  f"{staging or 'the import folder'} on {where}, every file proven by checksum")

    flow = [
        {"step": "Copy", "does": copies},
        {"step": "Instance metadata, before the databases",
         "does": "the source's logins, roles and Agent jobs are replayed on the target" if metadata
         else "nothing: server_metadata is off for this entry"},
        {"step": "Restore", "does": {
            "sqlserver": "per database: the newest FULL, its newest DIFF, then every LOG after it - to the newest "
                         "backup, or to the moment asked for",
            "postgresql": "the base backup and its incrementals are combined, then WAL is replayed - to the newest "
                          "backup, or to the moment asked for",
            "oracle": "one RMAN DUPLICATE: RMAN picks the level 0, the incrementals and the archived logs itself",
        }.get(engine, "the engine's own restore of the staged chain")},
        {"step": "Verify", "does": "each restored database is opened with a real query"
                                   + ("" if entry.get("checkdb") is False else
                                      ", and the engine's consistency check runs (checkdb)")
                                   + "; a database that does not open fails the run"},
        {"step": "Instance metadata, after the databases",
         "does": "Agent job steps and the rest that need the databases" if metadata else "nothing"},
        {"step": "Delete the staged files", "does":
            "nothing is staged, so nothing is deleted" if in_place else
            f"only after a verify that passed: staged files older than cleanup_retention ({retention}) are "
            f"removed from {staging or 'the staging folder'} on {where}"},
    ]
    whole = engine in ("postgresql", "oracle", "mysql")
    touches = (f"RESTORES {databases} onto {where}" + (f" (container {container})" if container else "")
               + (" - what runs there now is REPLACED by the restored copy" if whole else
                  " - a database ONLINE there under the same name is REPLACED (overwrite_existing is true)"
                  if overwrite else " - a full restore over a database that is ONLINE there is refused "
                                    "(overwrite_existing is not true)")
               + ("" if in_place else f"; first writes the backup chain into {staging or 'the staging folder'}")
               + ("; replays the source's logins, roles and Agent jobs on the target" if metadata else "")
               + ("" if in_place else f"; afterwards deletes staged files older than {retention}")
               + ("; the target answers to the source's logins afterwards - a physical restore brings them"
                  if whole else ""))
    allowed = entry.get("allow_production_target") is True
    guards = [f"a target the inventory calls production is refused unless the entry states "
              f"allow_production_target (this entry: {str(allowed).lower()}; {where}: "
              f"{_environment(where, environments)})"]
    if not whole:
        guards.append("a full restore over a database that is ONLINE on the target is refused unless the entry "
                      f"states overwrite_existing (this entry: {str(overwrite).lower()}) - and always while a "
                      "lockdown is on")
    guards.append("restore-workflow takes the entry's run claim: while one run of it is in flight, a second "
                  "answers already-running and does nothing. restore-by-id takes no claim" if script else
                  "a run started by hand takes no run claim: look at job_runs and the entry's chat for a run "
                  "in flight before starting one")
    if restore_from == "repository":
        guards.append("restore_from is repository: only the copies an offload proved are used, and any other "
                      "file found there refuses the run")
    workflow = ["restore-workflow", *CONFIG_ARGS, "--restore-id", restore_id]
    steps: list[dict[str, Any]] = []
    if script:
        request = json.dumps({"restore_id": restore_id, "dry_run": True})
        steps.append({
            "step": "Plan the restore (dry run)", "kind": "dry_run", "changes": False,
            "does": "answers the steps the restore would run, in order, and runs none: nothing is copied, "
                    "nothing restored. It still reports `started` and `finished: done` to the entry's chat, as "
                    "a run does - say there that it is a dry run",
            **_lines(node, ["restore-by-id", *CONFIG_ARGS, request], bash_only=True)})
        steps.append({
            "step": "Dry run of the workflow", "kind": "dry_run", "changes": False,
            "does": "answers only that the entry would run (status dry-run); it plans and copies nothing",
            **_lines(node, [*workflow, "--dry-run"])})
    else:
        steps.append({
            "step": "Rehearse the workflow (dry run)", "kind": "dry_run", "changes": False,
            "does": "the space check runs (it only reads), the copy is not run, the restore and the cleanup are "
                    "dry runs over what is staged now. It still reports its start and its end to the entry's "
                    "chat, as a run does - say there that it is a dry run",
            **_lines(node, [*workflow, "--dry-run"])})
        steps.append({
            "step": "Print the RESTORE statements (dry run)", "kind": "dry_run", "changes": False,
            "does": "prints the RESTORE statements for the files staged on the target now and runs none",
            **_lines(node, ["restore-latest", *CONFIG_ARGS, "--restore-id", restore_id, "--dry-run"])})
    run = {"kind": "run", "changes": True, "gate": GATE_RESTORE, "touches": touches, "confirmation": NOTHING_ASKS}
    same = {**run, "touches": "what `Restore to the newest backup` touches"}
    if refusal:
        run["will_be_refused"] = refusal
    if entry.get("active", True) is False:
        run["inactive"] = ("the entry is inactive: the schedule never runs it, but a run named by its "
                           "restore_id still does")
    steps.append({"step": "Restore to the newest backup", **run,
                  "note": "the run /spbot_restore starts: every step reported, the outcome kept on the run",
                  **_lines(node, workflow)})
    steps.append({"step": "Restore to a moment (point in time)", **same,
                  "fill_in": f"{MOMENT_PLACEHOLDER}: the moment to restore to, with its UTC offset; it needs "
                             "log / WAL / archived-log backups reaching that moment",
                  **_lines(node, [*workflow, "--point-in-time", MOMENT_PLACEHOLDER])})
    if script:
        steps.append({
            "step": "Restore through restore-by-id", **same,
            "note": "the same restore through the same steps, without the entry's run claim: first make "
                    "sure no run of this entry is in flight. Add \"point_in_time\" to the request for a moment.",
            **_lines(node, ["restore-by-id", *CONFIG_ARGS, json.dumps({"restore_id": restore_id})],
                     bash_only=True)})
    else:
        steps.append({
            "step": "Restore only what is staged", **{**run, "touches": (
                f"RESTORES {databases} onto {where} from the files already staged there; it copies nothing and "
                "deletes nothing" + (" - a database ONLINE under the same name is REPLACED" if overwrite else ""))},
            **_lines(node, ["restore-latest", *CONFIG_ARGS, "--restore-id", restore_id])})
    copy_only_ok = not in_place
    if copy_only_ok:
        steps.append({
            "step": "Copy only - nothing restored", "kind": "run", "changes": True, "gate": GATE_COPY_ONLY,
            "touches": f"writes the backup chain into {staging or 'the staging folder'} on {where} and proves "
                       "every file by checksum, then stops: no database is restored, no metadata replayed, the "
                       "staged files kept",
            "confirmation": NOTHING_ASKS,
            **_lines(node, [*workflow, "--copy-only"])})

    twin, copy_twin = twins.get(GATE_RESTORE), twins.get(GATE_COPY_ONLY)
    moment_note = (f"the last argument is the moment: {LATEST}, or {MOMENT_PLACEHOLDER} (it takes the rest "
                   "of the line)") if twin and twin["takes_a_moment"] else ""
    telegram = {
        "restore": _bot_block(twin, _bot_line(twin, {"restore_id": restore_id, "point_in_time": LATEST})
                              if twin else "", runs=f"restore-workflow --restore-id {restore_id}", note=moment_note),
        **({"copy_only": _bot_block(
            copy_twin, _bot_line(copy_twin, {"restore_id": restore_id, "point_in_time": LATEST})
            if copy_twin else "", runs=f"restore-workflow --restore-id {restore_id} --copy-only")}
           if copy_only_ok else {}),
    }
    return {
        "kind": "restore", "restore_id": restore_id,
        "entry_kind": ("script-driven: restore-workflow and restore-by-id run it; restore-latest refuses it"
                       if script else
                       "share-driven (SQL Server from a backup share): restore-workflow and restore-latest run "
                       "it; restore-by-id refuses it"),
        "db_type": engine, "action": action, "restore_from": restore_from, "reads": reads,
        "source_server_id": source, "source_environment": _environment(source, environments),
        "target_server_id": where, "target_environment": _environment(where, environments),
        "target_container": container, "in_place": in_place,
        "active": entry.get("active", True) is not False, "databases": databases,
        "overwrite_existing": overwrite, "allow_production_target": entry.get("allow_production_target") is True,
        "staging_folder": staging, "cleanup_retention": retention,
        "note": _text(entry.get("note"))[:NOTE_CHARS],
        "schedule": _schedule(entry, now), "notify": _notify(entry),
        "flow": flow, "steps": steps, "guards": guards, "telegram": telegram,
        "list": _list_block(node, twins.get(LIST_RESTORES), "list-restores"),
        "watch": _watch(kind="restore", entry_id=restore_id, notify=_notify(entry), report_base=report_base),
    }


# --------------------------------------------------------------------------- #
# Finding the entry, and the answer
# --------------------------------------------------------------------------- #
def _section(document: Any) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    section = document.get("backup_restore") if isinstance(document, dict) else None
    section = section if isinstance(section, dict) else {}
    return ([item for item in section.get("backups") or [] if isinstance(item, dict)],
            [item for item in section.get("restores") or [] if isinstance(item, dict)])


def index(document: Any) -> dict[str, Any]:
    """Every entry by id, briefly - what a question that names no id is answered with, so the next
    call can name one ("the store's FULL" is found by its server and its job, not guessed)."""
    backups, restores = _section(document)
    return {
        "backups": [{"backup_id": _text(item.get("backup_id")), "db_type": _text(item.get("db_type")),
                     "server_id": _text(item.get("server_id")), "active": item.get("active", True) is not False,
                     "jobs": [f"{_text(job.get('job'))}" + (f" ({_level(job)})" if _level(job) else "")
                              for job in item.get("jobs") or [] if isinstance(job, dict)],
                     "repository": item.get("offload_enabled") is True}
                    for item in backups],
        "restores": [{"restore_id": _text(item.get("restore_id")),
                      "db_type": _text(item.get("db_type")) or ("" if is_script_restore(item) else "sqlserver"),
                      "from": _text(item.get("server_id")),
                      "onto": _text(item.get("target_server_id")) or _text(item.get("server_id")),
                      "restore_from": _text(item.get("restore_from")),
                      "active": item.get("active", True) is not False}
                     for item in restores],
    }


def listing(answer: dict[str, Any]) -> dict[str, Any]:
    """The entries of a built answer by id, briefly - what is given when the runbooks of a server
    are more than one tool result holds: a runbook cut in the middle is a line without its warning."""
    return {
        "backups": [{"backup_id": item["backup_id"], "db_type": item["db_type"], "server_id": item["server_id"],
                     "active": item["active"], "jobs": [job["job"] for job in item["jobs"]],
                     "repository": bool(item["repository"].get("enabled"))}
                    for item in answer.get("backups") or []],
        "restores": [{"restore_id": item["restore_id"], "db_type": item["db_type"],
                      "from": item["source_server_id"], "onto": item["target_server_id"],
                      "restore_from": item["restore_from"], "active": item["active"]}
                     for item in answer.get("restores") or []],
    }


def _match(items: list[dict[str, Any]], key: str, wanted: str) -> list[dict[str, Any]]:
    exact = [item for item in items if _text(item.get(key)) == wanted]
    return exact or [item for item in items if _text(item.get(key)).lower() == wanted.lower()]


def build(document: Any, *, backup_id: str = "", restore_id: str = "", server_id: str = "",
          node: Node, twins: Mapping[str, dict[str, Any]], environments: Mapping[str, str],
          now: datetime | None = None, report_base: str = "") -> dict[str, Any]:
    """The answer to one question: the runbook of the entry named, or of every entry of a server;
    the index when nothing is named; and, for a name nothing carries, the names that are close."""
    backups, restores = _section(document)
    backup_id, restore_id, server_id = _text(backup_id), _text(restore_id), _text(server_id)
    if not (backup_id or restore_id or server_id):
        return {"asked": "nothing named", **index(document),
                "next": "name one backup_id, one restore_id, or a server_id for all of its entries."}
    chosen_backups = _match(backups, "backup_id", backup_id) if backup_id else []
    chosen_restores = _match(restores, "restore_id", restore_id) if restore_id else []
    if server_id:
        chosen_backups += [item for item in _match(backups, "server_id", server_id) if item not in chosen_backups]
        chosen_restores += [item for item in restores
                            if server_id.lower() in (_text(item.get("server_id")).lower(),
                                                     _text(item.get("target_server_id")).lower())
                            and item not in chosen_restores]
    answer: dict[str, Any] = {"asked": {key: value for key, value in (
        ("backup_id", backup_id), ("restore_id", restore_id), ("server_id", server_id)) if value}}
    not_found = {}
    if backup_id and not _match(backups, "backup_id", backup_id):
        not_found["backup_id"] = {"asked": backup_id, "close": difflib.get_close_matches(
            backup_id, [_text(item.get("backup_id")) for item in backups], n=5, cutoff=0.5)}
    if restore_id and not _match(restores, "restore_id", restore_id):
        not_found["restore_id"] = {"asked": restore_id, "close": difflib.get_close_matches(
            restore_id, [_text(item.get("restore_id")) for item in restores], n=5, cutoff=0.5)}
    if server_id and not (chosen_backups or chosen_restores):
        known = sorted(({_text(item.get("server_id")) for item in [*backups, *restores]}
                        | {_text(item.get("target_server_id")) for item in restores}) - {""})
        not_found["server_id"] = {"asked": server_id, "close": difflib.get_close_matches(
            server_id, known, n=5, cutoff=0.5), "note": "no backup or restore entry names this server_id"}
    if not_found:
        answer["not_found"] = not_found
    if len(chosen_backups) + len(chosen_restores) > MAX_ENTRIES_IN_FULL:
        listing = index({"backup_restore": {"backups": chosen_backups, "restores": chosen_restores}})
        return {**answer, **listing,
                "next": f"{len(chosen_backups) + len(chosen_restores)} entries: name one backup_id or "
                        "restore_id for its runbook."}
    answer["node"] = node_block(node)
    common = {"node": node, "twins": twins, "environments": environments, "now": now, "report_base": report_base}
    answer["backups"] = [backup_runbook(item, **common) for item in chosen_backups]
    answer["restores"] = [restore_runbook(item, **common) for item in chosen_restores]
    return answer


# --------------------------------------------------------------------------- #
# Who is handed a line (1.328)
# --------------------------------------------------------------------------- #
#: A step's own lines, taken away when the asker may not run it.
LINE_KEYS = ("cli", "from_master", "cli_not_written", "from_master_not_written", "level_words", "fill_in")


def _gate_twin(runbook: dict[str, Any], gate: str) -> dict[str, Any] | None:
    block = runbook.get("telegram") or {}
    if runbook.get("kind") == "restore":
        block = block.get("copy_only" if gate == GATE_COPY_ONLY else "restore") or block.get("restore") or {}
    return block if block.get("available") else None


def _held(block: dict[str, Any], *, asker_level: int) -> str:
    if block["level"] < 0:
        return (f"{block['command']} is switched off on this node (level {block['level']}), so no Telegram "
                "level may run this from a chat; it is given only at the node's own command line "
                "(dbabrain telegram ai-ask).")
    return (f"it changes something, and on this node that takes Telegram level {block['level']} "
            f"({block['command']}); the asker's level is {asker_level}. Not given: say so, and that a user "
            f"at level {block['level']} or above can run it or ask for it.")


def for_asker(answer: dict[str, Any], *, asker_level: int | None,
              may_run: Callable[[int], bool] | None = None) -> dict[str, Any]:
    """The answer as this asker may have it (1.328): a step that changes something keeps its lines
    only when the asker's Telegram level may run the bot command that does the same on this node.

    ``asker_level`` is the level ``telegram_users.json`` gives the asker; ``None`` is the node's own
    operator at its command line (``telegram ai-ask``), who holds the passphrase already and is
    handed every line. ``may_run(level)`` is the bot's own rule for "may this user run a command of
    that level" - passed in, so the levels are compared in one place. A node with no bot command
    for a kind of step has no level to hold it to: the step is then given at the command line only -
    the strictest reading, as for an operation its configuration forgot.
    """
    operator = asker_level is None
    answer["asker"] = ({"is": "the node's operator, at its command line", "handed": "every line"} if operator
                       else {"telegram_level": asker_level,
                             "handed": "every step that reads or rehearses; a step that changes something only "
                                       "when this level may run the bot command that does the same"})
    withheld = 0
    for runbook in [*answer.get("backups", []), *answer.get("restores", [])]:
        for step in runbook.get("steps", []):
            if not step.get("changes"):
                continue
            twin = _gate_twin(runbook, str(step.get("gate") or ""))
            if twin is not None:
                step["telegram_level_needed"] = twin["level"]
            if operator:
                continue
            if twin is None:
                why = ("it changes something, and this node has no bot command that does the same, so no "
                       "Telegram level says who may run it. Not given in a chat: it is given at the node's "
                       "own command line (dbabrain telegram ai-ask).")
            elif may_run is not None and may_run(int(twin["level"])):
                continue
            else:
                why = _held(twin, asker_level=int(asker_level or 0))
            for key in LINE_KEYS:
                step.pop(key, None)
            step["withheld"] = why
            withheld += 1
        blocks = [runbook.get("telegram") or {}] if runbook.get("kind") == "backup" else \
            list((runbook.get("telegram") or {}).values())
        for block in blocks:
            if not block.get("available") or operator:
                continue
            if may_run is not None and may_run(int(block["level"])):
                block["asker_may_run"] = True
                continue
            block["asker_may_run"] = False
            for key in ("line", "runs", "note"):
                block.pop(key, None)
            block["withheld"] = _held(block, asker_level=int(asker_level or 0))
    if withheld:
        answer["asker"]["withheld_steps"] = withheld
    return answer
