"""The Telegram support commands: the conversation - the prompts a command asks, the answers it takes, and the workflow state between them.

Split out of ``telegram/command_processor.py`` on 2026-10-03 (Q11,
``audits/20261002_audit_typed_requests_and_errors.md`` section 5). ``command_processor`` re-exports every
name, so no import changes.
"""

from __future__ import annotations
import json
import re
import sys
from pathlib import Path
from typing import Any
from db_ops.lib.listing import choice_lines
from db_ops.transport import common_cli
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
from db_ops.db.queue_message import queue_message, store_block_from
from db_ops.db import DbOpsStore
from db_ops.lib.paths import DEFAULT_DATA_DIR, REPO_ROOT, TOOL_ROOT  # noqa: F401 - one definition, see that module
from db_ops.telegram.command_base import SupportCommand, _dispatch_log, _parameter_at_position
from db_ops.telegram.command_replies import COMMAND_STATUS_DONE
from db_ops.telegram.command_cli import safe_error_summary


def load_json_object(path: Path, root_key: str) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    with path.open("r", encoding="utf-8-sig") as file:
        data = json.load(file)
    return list(data.get(root_key, []))


def state_json_dict(state: Any) -> dict[str, Any]:
    try:
        return dict(json.loads(str(state["state_json"] or "{}")))
    except json.JSONDecodeError:
        return {}


def first_missing_prompt_parameter(command: SupportCommand, args: list[str]) -> dict[str, Any] | None:
    """The next question to ask, or ``None`` when the command has everything it needs.

    ``ask_when`` is consulted before anything else: a step whose branch was not taken is not
    missing, it was never asked. Before that, answers belonging to branches this run no longer
    reaches are cleared — going Back and choosing a different credential type has to forget the
    one abandoned, or it still reaches the CLI (see :func:`db_ops.lib.workflow_steps.
    clear_unreachable_answers`).
    """
    config = dict(command.action_config or {})
    parameters = list(config.get("parameters") or [])
    cleared = ws.clear_unreachable_answers(parameters, args)
    for index, value in enumerate(cleared):
        if index < len(args):
            args[index] = value
    for parameter in parameters:
        if not ws.ask_when_holds(parameter, ws.answers_by_name(parameters, args)):
            continue
        position = int(parameter.get("position", 1))
        required = (
            bool(parameter.get("required", True))
            or prompt_condition_holds(parameter, parameters, args)
            # An optional step is asked only when it says it wants to be — see
            # workflow_steps.is_asked_when_optional for why that is opt-in.
            or ws.is_asked_when_optional(parameter)
        )
        value = args[position - 1] if len(args) >= position else ""
        if required and str(value).strip() == "" and parameter.get("prompt_text"):
            skipped_value = skip_parameter_value(parameter, parameters, args)
            if skipped_value is None:
                return dict(parameter)
            while len(args) < position:
                args.append("")
            args[position - 1] = skipped_value
    return None


def workflow_run_key(source_command_message_id: Any, state_id: Any = None) -> str:
    """One id for a whole workflow run, stable across its steps.

    The source command message is what every step of a run already carries, so it identifies the
    run without a new column: the conversation state rows churn (each is `replaced` as the run
    moves on) and could not name the run they belong to.
    """
    if source_command_message_id not in (None, "", 0):
        return f"tcm:{source_command_message_id}"
    return f"state:{state_id}"


def answer_kind_for(parameter: dict[str, Any], value: str) -> str:
    """Did the answer match one of the offered options, or is it free text?

    Not "was a button tapped": with a reply keyboard a tap and the typed word are the same
    Telegram message, and a column that claimed otherwise would be inventing evidence.
    """
    options = {str(item["value"]).strip().lower() for item in ws.option_list(parameter)}
    labels = {str(item["label"]).strip().lower() for item in ws.option_list(parameter)}
    candidate = str(value).strip().lower()
    return "option" if candidate in options or candidate in labels else "text"


def normalise_answer(parameter: dict[str, Any], value: str) -> str:
    """The value to store: a button's label becomes the value it stands for.

    The keyboard shows `Yes` and the CLI wants `yes`; the operator sees the label and the pattern
    validates the value, so the translation has to happen here rather than in either of them.
    """
    candidate = str(value).strip()
    for option in ws.option_list(parameter):
        if candidate.lower() in (option["label"].strip().lower(), option["value"].strip().lower()):
            return option["value"]
    return candidate


def answer_rejection(parameter: dict[str, Any], value: str) -> str | None:
    """Why this answer cannot be accepted, or ``None`` when it can.

    Validation used to run only when the command finally executed, so a value mistyped at step 2
    of `/spbot_create_db_docker` was reported after step 14 — by which point the operator had
    answered twelve more questions and could not go back to fix it. Checking here costs one regex
    and turns the same mistake into a re-ask of the question they are already looking at.
    """
    candidate = str(value).strip()
    if not candidate:
        return "That answer was empty. Please answer the question, or use the buttons below."
    if not ws.accepts_free_text(parameter):
        allowed = ws.option_list(parameter)
        known = {item["value"].strip().lower() for item in allowed} | {
            item["label"].strip().lower() for item in allowed}
        if candidate.lower() not in known:
            offered = ", ".join(item["label"] for item in allowed)
            return f"Please choose one of: {offered}"
    if str(parameter.get("validator") or "") == "regex":
        pattern = str(parameter.get("pattern") or "")
        if pattern and not re.fullmatch(pattern, normalise_answer(parameter, candidate),
                                        flags=re.IGNORECASE):
            return str(parameter.get("validation_error")
                       or f"Invalid value for {parameter.get('name', 'this step')}.")
    return None


def prompt_condition_holds(
    parameter: dict[str, Any], parameters: list[dict[str, Any]], args: list[str]
) -> bool:
    """Whether an *optional* parameter must be asked for on this particular run.

    The mirror of ``skip_when``: that one declines to ask a question that cannot apply, this one
    asks a question that only some runs need. ``/spbot_run_sql_task`` is why it exists — its
    ``task_params`` is optional because most tasks declare no parameters, so it was never
    prompted, and a task that *requires* one ran with none and failed with a message about a
    missing ``--param`` that the operator was never given a chance to supply::

        "prompt_when": {"condition": "sql_task_has_parameters", "parameter": "sql_id"}

    ``sql_task_has_parameters`` holds when the sql_id already answered names a task that declares
    parameters in ``sql_commands.json``. A config read failure never blocks the flow: the run
    proceeds exactly as it did before, which is the behaviour every task without parameters wants.
    """
    rule = parameter.get("prompt_when")
    if not isinstance(rule, dict):
        return False
    if str(rule.get("condition") or "") != "sql_task_has_parameters":
        return False
    source_name = str(rule.get("parameter") or "sql_id").strip()
    source = next((item for item in parameters if str(item.get("name") or "") == source_name), None)
    if source is None:
        return False
    position = int(source.get("position", 1))
    sql_id = str(args[position - 1] if len(args) >= position else "").strip()
    return bool(sql_task_parameter_names(sql_id))


def sql_tasks_listing(sql_id: str | int | None = None) -> dict[str, Any]:
    """What SQL tasks exist, read in-process by the reader the runner itself uses.

    This app does not parse ``sql_commands.json``: it used to, and then it disagreed with the app
    that runs those tasks - whether a task counted as runnable was decided twice, and a task's
    declared parameters were not part of the bot's picture at all, so ``/spbot_run_sql_task`` never
    asked for one and every run of a task that required a parameter failed. Then it asked the
    runner's CLI (``sql_tasks.cli list-tasks``): one app driving another's CLI (rules R42). Since
    0.24.0 the reader is ``lib.sql_task_catalog`` - one definition, read by both, no process.

    Returns the listing, or ``{"ok": False, "error": ...}``. Never raises: a listing that cannot be
    produced is reported to the operator, and a prompt decision that cannot be made falls back to
    not prompting - the behaviour that was correct for every task before parameters existed.
    """
    from db_ops.lib import paths, sql_task_catalog

    wanted = str(sql_id or "").strip()
    try:
        # Read at call time, as it was before the module split: the name this module bound at
        # import is a copy, and a data folder set afterwards never reached it. The 0.26.0 public
        # suite found it - its test read this repo's own data/ in the private tree and passed.
        return dict(sql_task_catalog.collect_sql_tasks(
            Path(paths.DEFAULT_DATA_DIR).resolve(), sql_id=int(wanted) if wanted else None))
    except (OSError, ValueError, RuntimeError) as exc:
        _dispatch_log(
            None, f"telegram.command_processor.sql_tasks_listing.failed|error={safe_error_summary(exc)}",
            level="warning")
        return {"ok": False, "error": safe_error_summary(exc)}


def sql_task_parameter_names(sql_id: str) -> list[str]:
    """The parameter names a SQL task declares, or ``[]`` when it declares none."""
    if not str(sql_id).strip().isdigit():
        return []
    listing = sql_tasks_listing(sql_id)
    if not listing.get("ok"):
        return []
    for task in listing.get("sql_tasks") or []:
        if str(task.get("sql_id")) == str(sql_id).strip():
            return [str(name) for name in (task.get("parameter_names") or [])]
    return []


def render_prompt_text(parameter: dict[str, Any], parameters: list[dict[str, Any]],
                       args: list[str]) -> str:
    """The prompt as the operator sees it, with ``{sql_task_parameters}`` filled in.

    A prompt that says "values for the parameters this task declares" and then leaves the person
    to guess the names is only half an answer — they have just chosen a task by number, not by
    reading its config. Naming them turns the prompt into something answerable.
    """
    text = str(parameter.get("prompt_text") or f"Please input {parameter.get('name', 'value')}:")
    if "{sql_task_parameters}" not in text:
        choices = prompt_choice_text(parameter, parameters, args)
        return f"{text}\n\n{choices}" if choices else text
    rule = parameter.get("prompt_when") if isinstance(parameter.get("prompt_when"), dict) else {}
    source_name = str((rule or {}).get("parameter") or "sql_id").strip()
    source = next((item for item in parameters if str(item.get("name") or "") == source_name), None)
    sql_id = ""
    if source is not None:
        position = int(source.get("position", 1))
        sql_id = str(args[position - 1] if len(args) >= position else "").strip()
    names = sql_task_parameter_names(sql_id)
    return text.replace("{sql_task_parameters}", ", ".join(names) if names else "none")


#: A prompt that lists choices runs a `common` CLI command *while the operator waits*, so it needs
#: a deadline of its own. `list-databases` opens a connection to the server, and the default of no
#: timeout would leave the conversation with no prompt at all when an instance is unreachable —
#: the one situation in which the plain prompt is most needed.
PROMPT_CHOICES_TIMEOUT_SECONDS = 25


def prompt_choice_text(parameter: dict[str, Any], parameters: list[dict[str, Any]],
                       args: list[str]) -> str:
    """The list of values this parameter will accept, or ``""`` when it cannot be produced.

    Declared per parameter in ``telegram_support_commands.json``::

        "prompt_choices": {"command": "list-schemas", "data_key": "schemas",
                           "request": {"target": "{server_id}", "database_name": "{database}"}}

    ``{name}`` in the request is filled from the answer already given for that parameter, which is
    what makes the two steps of ``/spbot_xlsx_to_table`` chain: the database list needs the server
    just answered, and the schema list needs both.

    **Every failure returns an empty string.** A prompt is the only thing that keeps the flow
    moving, and the cases where listing fails — unreachable instance, wrong credential, an engine
    `list-schemas` does not know — are exactly the cases where the operator still needs to be asked
    the question. Typing the name has always worked and still does; the list is an aid, never a
    gate. This is the same fail-open contract as ``sql_task_parameter_names`` and
    ``_target_has_no_database``.
    """
    rule = parameter.get("prompt_choices")
    if not isinstance(rule, dict):
        return ""
    command = str(rule.get("command") or "").strip()
    data_key = str(rule.get("data_key") or "").strip()
    template = rule.get("request")
    if not command or not data_key or not isinstance(template, dict):
        return ""

    answered = _answered_parameters(parameters, args)
    request: dict[str, Any] = {}
    for key, value in template.items():
        resolved = _fill_placeholders(value, answered)
        if isinstance(resolved, str) and not resolved.strip():
            # A placeholder with no answer yet means this list cannot be asked for. Happens when a
            # command is invoked with its arguments out of order; the bare prompt is the answer.
            return ""
        request[str(key)] = resolved

    try:
        # The server's login is this node's to state (rules R09): common.cli reads no config.
        request = request_fill.fill_request(command, request)
        success, data, _error = common_cli.run_allowing_failure(
            command, request, timeout_seconds=PROMPT_CHOICES_TIMEOUT_SECONDS)
    except Exception:  # noqa: BLE001 - listing is an aid; nothing here may block the prompt.
        return ""
    if not success or not isinstance(data, dict):
        return ""

    entries = data.get(data_key)
    if not isinstance(entries, list):
        return ""
    names = [
        str(entry.get("name") or "") if isinstance(entry, dict) else str(entry)
        for entry in entries
    ]
    return choice_lines(names)


def _answered_parameters(parameters: list[dict[str, Any]], args: list[str]) -> dict[str, str]:
    """``parameter name -> the answer given so far``, for the ones that have one."""
    answered: dict[str, str] = {}
    for item in parameters:
        name = str(item.get("name") or "").strip()
        if not name:
            continue
        position = int(item.get("position", 1))
        answered[name] = str(args[position - 1] if len(args) >= position else "").strip()
    return answered


def _fill_placeholders(value: Any, answered: dict[str, str]) -> Any:
    """Substitute ``{parameter_name}`` in a request value; non-strings pass through untouched."""
    if not isinstance(value, str):
        return value
    filled = value
    for name, answer in answered.items():
        filled = filled.replace("{" + name + "}", answer)
    return filled


def skip_parameter_value(
    parameter: dict[str, Any], parameters: list[dict[str, Any]], args: list[str]
) -> str | None:
    """Value to auto-fill instead of prompting the user, or None to prompt as usual.

    A parameter may declare ``skip_when`` in the command config, e.g.::

        "skip_when": {"condition": "target_has_no_database", "parameter": "target_ip", "value": "-"}

    ``target_has_no_database`` holds when every db instance configured for that IP has no
    db_type — an OS-only host such as an ERP AOS application VM. Asking such a host for
    db_type or port is meaningless, so the bot fills the skip value and runs the command.
    """
    rule = parameter.get("skip_when")
    if not isinstance(rule, dict) or str(rule.get("condition") or "") != "target_has_no_database":
        return None
    source_name = str(rule.get("parameter") or "").strip()
    source = next((item for item in parameters if str(item.get("name") or "") == source_name), None)
    if source is None:
        return None
    source_position = int(source.get("position", 1))
    target_ip = str(args[source_position - 1] if len(args) >= source_position else "").strip()
    if not target_ip or not _target_has_no_database(target_ip):
        return None
    return str(rule.get("value") or "-")


def _target_has_no_database(target_ip: str) -> bool:
    from db_ops.lib.data_sources import load_config_metric_targets

    try:
        targets = [item for item in load_config_metric_targets() if str(item.ip) == target_ip]
    except Exception:  # noqa: BLE001 - a config read failure must not block the prompt flow.
        return False
    return bool(targets) and all(not str(item.db_type or "").strip() for item in targets)


def workflow_history(state_data: dict[str, Any]) -> list[int]:
    """The positions this run has actually asked, oldest first.

    Back is defined over this list and never over ``position - 1``: with ``ask_when`` branching,
    some positions are never asked at all, so counting backwards would re-ask a question this run
    deliberately excluded — and then treat its answer as meaningful.
    """
    raw = state_data.get("history")
    if not isinstance(raw, list):
        return []
    history: list[int] = []
    for item in raw:
        try:
            history.append(int(item))
        except (TypeError, ValueError):
            continue
    return history


def forget_secret_answer(store: Any, message: Any, *, delete_message: Any = None) -> None:
    """Redact a secret answer where it is stored, and delete it from the chat. Never raises.

    Deleting can fail - a bot that is not an admin of a group may not delete others' messages, and
    a message older than 48 hours cannot be deleted - so the stored copies are redacted first and
    a failed delete is only logged.
    """
    try:
        store.redact_telegram_message(telegram_message_id=int(message["telegram_message_id"]))
    except Exception as exc:  # noqa: BLE001 - the answer itself must still be processed.
        print(f"telegram: could not redact a secret answer in the store: {exc}", file=sys.stderr)
    if delete_message is None or message["message_id"] is None:
        return
    try:
        delete_message(str(message["chat_id"]), int(message["message_id"]))
    except Exception as exc:  # noqa: BLE001
        print(f"telegram: could not delete a secret answer from chat {message['chat_id']}: {exc} "
              "(the bot needs 'delete messages' rights in a group)", file=sys.stderr)


def masked_answer(parameter: dict[str, Any], value: str) -> str:
    """What the step trail is allowed to remember of an answer.

    A `secret` step's value is an SSH or database password. The trail exists to show what happened,
    which needs the fact that a value was given and nothing else — the store is read by the console
    and by anyone with a psql prompt.
    """
    if ws.is_secret(parameter):
        return f"*** ({len(str(value).strip())} chars)"
    return str(value)


def queue_step_prompt(
    *,
    store: DbOpsStore,
    command: SupportCommand,
    step: dict[str, Any],
    args: list[str],
    chat_id: str,
    reply_to_message_id: int | None,
    source_type: str,
    source_id: str,
    run_key: str,
    user_id: str,
    history: list[int],
    state_id: int | None = None,
) -> int:
    """Ask one step: the prompt, its keyboard, and the row that records having asked.

    One function for both entry points — the first question of a run and every question after it —
    because they had drifted into two copies of the same seven lines, and the keyboard, the hint
    and the trail would have had to be added to each.
    """
    parameters = list((command.action_config or {}).get("parameters") or [])
    # `history` ends with the step being asked, so anything before it is somewhere to go back to.
    # The first question of a run has nowhere, and must not offer a button that does nothing.
    can_go_back = len(history) > 1
    prompt_text = render_prompt_text(step, parameters, args)
    hint = ws.control_hint(step, can_go_back=can_go_back)
    keyboard = ws.keyboard_for(step, can_go_back=can_go_back)
    queued_id = queue_message({
        "store": store_block_from(store),
        "message_type": "plain",
        "chat_id": str(chat_id),
        "text": f"{prompt_text}\n\n{hint}",
        "reply_message_id": reply_to_message_id,
        "note": f"Prompt for command {command.command_text}",
        "source_type": source_type,
        "source_id": str(source_id),
        "metadata": {
            "command_id": command.command_id,
            "command_text": command.command_text,
            "conversation_state": "waiting",
            # force_reply and a keyboard are mutually exclusive in the Telegram API - the last
            # reply_markup wins - and the keyboard is the better of the two: it carries Cancel and
            # Back, which force_reply cannot.
            "reply_markup": keyboard,
            "workflow": {"run_key": run_key, "parameter": step.get("name"),
                         "position": int(step.get("position", 1))},
        },
    }, fallback_store=store)
    store.start_telegram_workflow_step(
        run_key=run_key,
        chat_id=str(chat_id),
        user_id=str(user_id),
        command_id=command.command_id,
        command_text=command.command_text,
        parameter_name=str(step.get("name") or "arg"),
        parameter_position=int(step.get("position", 1)),
        prompt_text=prompt_text,
        options=ws.option_list(step),
        controls=ws.keyboard_for(step, can_go_back=can_go_back)["keyboard"][-1],
        is_secret=ws.is_secret(step),
        state_id=state_id,
        prompt_send_tlgmsg_id=queued_id if isinstance(queued_id, int) else None,
    )
    return queued_id


def queue_workflow_closing_message(
    *,
    store: DbOpsStore,
    chat_id: str,
    text: str,
    reply_to_message_id: int | None,
    source_type: str,
    source_id: str,
    command: SupportCommand,
    message_type: str = "plain",
) -> None:
    """The last message of a run — and the one that takes the keyboard away.

    Without ``remove_keyboard`` the buttons of the final question stay on the operator's screen
    after the workflow has ended, and tapping Back then answers a question nobody asked.
    """
    queue_message({
        "store": store_block_from(store),
        "message_type": message_type,
        "chat_id": str(chat_id),
        "text": text,
        "reply_message_id": reply_to_message_id,
        "note": f"Workflow ended for {command.command_text}",
        "source_type": source_type,
        "source_id": str(source_id),
        "metadata": {
            "command_id": command.command_id,
            "command_text": command.command_text,
            "reply_markup": ws.hide_keyboard(),
        },
    }, fallback_store=store)


def record_inline_answers(
    *,
    store: DbOpsStore,
    row: Any,
    command: SupportCommand,
    args: list[str],
) -> int:
    """Write a trail row for every answer that arrived on the command line itself.

    A parameterised command can be run in one message — `/spbot_run_sql_task 18 0 30` — and then
    no prompt is ever queued, so nothing in the step trail would show the run at all. The person
    answered every question; they just answered them all at once.

    Recorded as ``answer_kind='inline'`` so the trail can still tell the two apart: what was typed
    ahead of being asked, and what was given in reply to a prompt.
    """
    parameters = list((command.action_config or {}).get("parameters") or [])
    written = 0
    for parameter in sorted(parameters, key=lambda item: int(item.get("position", 1))):
        if str(parameter.get("source") or "") == "flag":
            continue
        if not ws.ask_when_holds(parameter, ws.answers_by_name(parameters, args)):
            continue
        position = int(parameter.get("position", 1))
        value = str(args[position - 1] if len(args) >= position else "").strip()
        if not value:
            continue
        store.start_telegram_workflow_step(
            run_key=workflow_run_key(row["telegram_command_message_id"]),
            chat_id=str(row["chat_id"]),
            user_id=str(row["user_id"]),
            command_id=command.command_id,
            command_text=command.command_text,
            parameter_name=str(parameter.get("name") or "arg"),
            parameter_position=position,
            prompt_text="",
            options=ws.option_list(parameter),
            is_secret=ws.is_secret(parameter),
        )
        store.finish_telegram_workflow_step(
            run_key=workflow_run_key(row["telegram_command_message_id"]),
            status="answered",
            answer_text=masked_answer(parameter, value),
            answer_kind="inline",
            answer_telegram_message_id=int(row["telegram_message_id"])
            if row["telegram_message_id"] is not None else None,
        )
        written += 1
    return written


def queue_missing_parameter_prompt(
    *,
    store: DbOpsStore,
    row: Any,
    command: SupportCommand,
    missing_parameter: dict[str, Any],
    args: list[str],
) -> dict[str, int | str]:
    # Whatever arrived on the command line was still answered by this person; without this the
    # trail would start at the first *prompted* step and silently drop the rest.
    record_inline_answers(store=store, row=row, command=command, args=args)
    history = [int(missing_parameter.get("position", 1))]
    queued_reply_id = queue_step_prompt(
        store=store,
        command=command,
        step=missing_parameter,
        args=args,
        chat_id=str(row["chat_id"]),
        reply_to_message_id=int(row["message_id"]) if row["message_id"] is not None else None,
        source_type="telegram_command_messages",
        source_id=str(row["telegram_command_message_id"]),
        run_key=workflow_run_key(row["telegram_command_message_id"]),
        user_id=str(row["user_id"]),
        history=history,
    )
    state_id = store.upsert_telegram_conversation_state(
        chat_id=str(row["chat_id"]),
        user_id=str(row["user_id"]),
        command_id=command.command_id,
        command_text=command.command_text,
        state_key=str(missing_parameter.get("name") or "arg"),
        wait_after_message_id=int(row["message_id"]),
        source_telegram_command_message_id=int(row["telegram_command_message_id"]),
        state_data={
            "args": args,
            "parameter_name": missing_parameter.get("name"),
            "parameter_position": int(missing_parameter.get("position", 1)),
            "history": history,
        },
    )
    process_note = f"Command matched: {command.command_text}; waiting_state={state_id}; queued_prompt={queued_reply_id}"
    store.update_telegram_command_message_status(
        telegram_command_message_id=int(row["telegram_command_message_id"]),
        command_status=COMMAND_STATUS_DONE,
        command_id=command.command_id,
        process_note=process_note,
    )
    return {
        "telegram_command_message_id": int(row["telegram_command_message_id"]),
        "processed": 1,
        "queued_reply": 1,
        "skipped": 0,
        "not_found": 0,
        "status": "waiting_for_input",
    }


def _chain_next_conversation_parameter(
    *,
    store: DbOpsStore,
    state: Any,
    message: Any,
    command: SupportCommand,
    args: list[str],
    next_missing: dict[str, Any],
    state_data: dict[str, Any],
) -> None:
    # Mark current state done BEFORE upsert — upsert sets all 'waiting' to 'replaced' first.
    updated_state_data = dict(state_data)
    updated_state_data["args"] = args
    store.update_telegram_conversation_state(
        state_id=int(state["state_id"]),
        status="done",
        state_data=updated_state_data,
        consumed_telegram_message_id=int(message["telegram_message_id"]),
        note="chained to next parameter",
    )
    history = [item for item in workflow_history(state_data)
               if item != int(next_missing.get("position", 1))]
    history.append(int(next_missing.get("position", 1)))
    queue_step_prompt(
        store=store,
        command=command,
        step=next_missing,
        args=args,
        chat_id=str(state["chat_id"]),
        reply_to_message_id=int(message["message_id"]) if message["message_id"] is not None else None,
        source_type="telegram_conversation_states",
        source_id=str(state["state_id"]),
        run_key=workflow_run_key(state["source_telegram_command_message_id"], state["state_id"]),
        user_id=str(state["user_id"]),
        history=history,
        state_id=int(state["state_id"]),
    )
    store.upsert_telegram_conversation_state(
        chat_id=str(state["chat_id"]),
        user_id=str(state["user_id"]),
        command_id=command.command_id,
        command_text=command.command_text,
        state_key=str(next_missing.get("name") or "arg"),
        wait_after_message_id=int(message["message_id"]),
        source_telegram_command_message_id=int(state["source_telegram_command_message_id"] or state["state_id"]),
        state_data={
            "args": args,
            "parameter_name": next_missing.get("name"),
            "parameter_position": int(next_missing.get("position", 1)),
            "history": history,
        },
    )


#: :func:`apply_conversation_control`'s answer to a Skip that left nothing to ask: run the command.
SKIPPED_LAST_STEP = "skipped_last_step"


def apply_conversation_control(
    *,
    store: DbOpsStore,
    state: Any,
    message: Any,
    command: SupportCommand,
    args: list[str],
    state_data: dict[str, Any],
    step: dict[str, Any],
    control: ws.Control,
    run_key: str,
) -> str:
    """Act on Back / Skip / Cancel, and say which one was applied.

    The whole point of the module this reads from is that these are **state transitions on the
    workflow**, decided once, rather than a word each command checks for itself. What is left here
    is only the part that needs the store: closing the trail row, moving the conversation state,
    and asking the next question.
    """
    parameters = list((command.action_config or {}).get("parameters") or [])
    history = workflow_history(state_data)
    reply_to = int(message["message_id"]) if message["message_id"] is not None else None

    if control.is_cancel:
        store.finish_telegram_workflow_step(
            run_key=run_key, status="cancelled", answer_kind="cancel",
            answer_telegram_message_id=int(message["telegram_message_id"]))
        store.update_telegram_conversation_state(
            state_id=int(state["state_id"]), status="cancelled", state_data=state_data,
            consumed_telegram_message_id=int(message["telegram_message_id"]),
            note="cancelled by the operator")
        queue_workflow_closing_message(
            store=store, chat_id=str(state["chat_id"]),
            text=f"❌ {command.command_text} cancelled. No changes were made.",
            reply_to_message_id=reply_to, source_type="telegram_conversation_states",
            source_id=str(state["state_id"]), command=command)
        return "cancelled"

    if control.is_back:
        # history ends with the step being answered; the one before it is where Back goes.
        previous_positions = [item for item in history[:-1]]
        if not previous_positions:
            return _reask_with_note(
                store=store, state=state, message=message, command=command, args=args,
                state_data=state_data, step=step,
                note="This is the first question - there is nothing to go back to.",
                run_key=run_key, status="rejected")
        target_position = previous_positions[-1]
        target_step = _parameter_at_position(command, target_position)
        if target_step is None:
            return _reask_with_note(
                store=store, state=state, message=message, command=command, args=args,
                state_data=state_data, step=step,
                note="That step no longer exists in this command.", run_key=run_key,
                status="rejected")
        store.finish_telegram_workflow_step(
            run_key=run_key, status="back", answer_kind="back",
            answer_telegram_message_id=int(message["telegram_message_id"]))
        # Clear the answer being returned to, so the step is genuinely re-asked rather than
        # skipped over as "already answered" by the next-step search.
        while len(args) < target_position:
            args.append("")
        args[target_position - 1] = ""
        _chain_next_conversation_parameter(
            store=store, state=state, message=message, command=command, args=args,
            next_missing=dict(target_step),
            state_data=dict(state_data, history=previous_positions[:-1]))
        return "back"

    # Skip
    if not ws.is_skippable(step):
        return _reask_with_note(
            store=store, state=state, message=message, command=command, args=args,
            state_data=state_data, step=step,
            note="This step is required and cannot be skipped.", run_key=run_key,
            status="rejected")
    position = int(step.get("position", 1))
    while len(args) < position:
        args.append("")
    args[position - 1] = ws.skip_value(step)
    store.finish_telegram_workflow_step(
        run_key=run_key, status="skipped", answer_text=ws.skip_value(step), answer_kind="skip",
        answer_telegram_message_id=int(message["telegram_message_id"]))
    next_missing = first_missing_prompt_parameter(command, args)
    if next_missing is not None:
        _chain_next_conversation_parameter(
            store=store, state=state, message=message, command=command, args=args,
            next_missing=next_missing, state_data=state_data)
        return "skipped"
    # Nothing left to ask: the caller runs the command, exactly as it does after the last answer.
    # This re-chained the same step "so the next cycle would execute" - which queued the step's
    # question again and waited for a reply to it: /spbot_backup <id> asked for its level a second
    # time after Skip, for ever (1.87, the 0.26 soak node's bot, 2026-10-02).
    return SKIPPED_LAST_STEP


def _reask_with_note(
    *,
    store: DbOpsStore,
    state: Any,
    message: Any,
    command: SupportCommand,
    args: list[str],
    state_data: dict[str, Any],
    step: dict[str, Any],
    note: str,
    run_key: str,
    status: str,
) -> str:
    """Tell the operator why that did not work, then ask the same question again.

    Never leaves the conversation without a live question: a workflow that answers "you cannot do
    that" and then waits for nothing is one the operator has to abandon and restart.
    """
    store.finish_telegram_workflow_step(
        run_key=run_key, status=status, answer_kind="text",
        answer_telegram_message_id=int(message["telegram_message_id"]))
    queue_message({
        "store": store_block_from(store),
        "message_type": "failed",
        "chat_id": str(state["chat_id"]),
        "text": note,
        "reply_message_id": int(message["message_id"]) if message["message_id"] is not None else None,
        "note": f"Control refused for {command.command_text}",
        "source_type": "telegram_conversation_states",
        "source_id": str(state["state_id"]),
        "metadata": {"command_id": command.command_id, "command_text": command.command_text},
    }, fallback_store=store)
    _chain_next_conversation_parameter(
        store=store, state=state, message=message, command=command, args=args,
        next_missing=dict(step), state_data=state_data)
    return "reasked"
