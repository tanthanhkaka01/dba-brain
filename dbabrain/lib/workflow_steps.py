"""One step of a Telegram workflow: what may be answered, what may be skipped, what Back means.

A conversation command is a list of steps in ``telegram_support_commands.json``, and the bot walks
them one reply at a time. Everything about *which* step comes next, whether this one may be
skipped, and what keyboard the operator sees is decided here — as pure functions over the step
definition and the answers so far, with no store, no network and no Telegram vocabulary beyond the
shape of a keyboard object.

**Why this module exists at all.** The alternative was `if message.text == "back"` inside each
command's handler, which is how a workflow ends up with fourteen slightly different ideas of what
Back does. The operator asked for the opposite and was right to: Back is a state transition, and
the state it moves through is the same for every command.

**Back walks the ask history, never ``position - 1``.** With branching, some positions are never
asked — answer ``worker`` to ``deploy_target`` and the three SSH questions do not happen — so
counting backwards would re-ask a question this run deliberately excluded, and would then treat
its answer as meaningful. The history is the only structure that knows what was actually asked.

**Skip is offered only where it is real.** Every step of ``/spbot_create_db_docker`` is
``required: true`` with ``|-`` bolted onto its pattern, so the operator types ``-`` to mean "not
applicable" — twice in a row, for a credential they had already given. A Skip button on a step
that must have a value would be the same defect with a nicer surface: :func:`is_skippable` is what
decides, and it says no unless the step says yes.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

#: What the operator can say instead of answering. Recognised case-insensitively, with or without
#: a leading slash, and matched against the **button labels** too — the buttons in
#: :func:`keyboard_for` say the same words, so tapping and typing are one vocabulary rather than
#: two that drift apart.
CONTROL_BACK = "back"
CONTROL_SKIP = "skip"
CONTROL_CANCEL = "cancel"

#: Label -> control. The emoji are on the buttons only; a person typing plain ``back`` is answering
#: the same way, which is the point of keeping one table.
CONTROL_LABELS: dict[str, str] = {
    "back": CONTROL_BACK,
    "⬅️ back": CONTROL_BACK,
    "⬅ back": CONTROL_BACK,
    "skip": CONTROL_SKIP,
    "⏭️ skip": CONTROL_SKIP,
    "⏭ skip": CONTROL_SKIP,
    "cancel": CONTROL_CANCEL,
    "🚫 cancel": CONTROL_CANCEL,
    "❌ cancel": CONTROL_CANCEL,
}

#: The button captions, in one place so the keyboard and :data:`CONTROL_LABELS` cannot disagree.
BACK_LABEL = "⬅️ Back"
SKIP_LABEL = "⏭️ Skip"
CANCEL_LABEL = "🚫 Cancel"

#: What a skipped step stores when it does not name its own ``skip_value``. Every CLI this bot
#: drives already reads ``-`` as "not given" — that convention is why the ``|-`` patterns exist —
#: so skipping produces exactly what typing a dash produced, and no CLI has to learn a new word.
DEFAULT_SKIP_VALUE = "-"

#: Steps whose answer must never be echoed back into the chat. ``secret: true`` is the existing
#: spelling and stays authoritative; ``input_type: "secret"`` is the new one.
_SECRET_INPUT_TYPE = "secret"


@dataclass(frozen=True)
class Control:
    """A control word the operator typed or tapped, rather than an answer to the question."""

    kind: str

    @property
    def is_back(self) -> bool:
        return self.kind == CONTROL_BACK

    @property
    def is_skip(self) -> bool:
        return self.kind == CONTROL_SKIP

    @property
    def is_cancel(self) -> bool:
        return self.kind == CONTROL_CANCEL


def resolve_control(text: str, step: dict[str, Any] | None = None) -> Control | None:
    """Is this reply a control word? ``None`` means it is an ordinary answer.

    ``step`` is consulted so a step can legitimately *want* one of these words as its value: a
    parameter whose ``options`` offer ``skip`` means the string, not the action. Rare, and cheap
    to honour — the alternative is a workflow that can never ask "cancel or continue?".
    """
    candidate = str(text or "").strip().lower().lstrip("/")
    if not candidate:
        return None
    kind = CONTROL_LABELS.get(candidate)
    if kind is None:
        return None
    if step is not None and candidate in {
        str(option.get("value", "")).strip().lower() for option in option_list(step)
    }:
        return None
    return Control(kind=kind)


def option_list(step: dict[str, Any]) -> list[dict[str, str]]:
    """The step's offered answers as ``{label, value}``, tolerating the short spellings.

    ``["yes", "no"]`` and ``[{"label": "Yes", "value": "yes"}]`` both appear in hand-written
    config; refusing the short form would make the schema more annoying than the ``|-`` it
    replaces. An entry with no value is dropped rather than rendered as an unanswerable button.
    """
    raw = step.get("options")
    if not isinstance(raw, list):
        return []
    options: list[dict[str, str]] = []
    for item in raw:
        if isinstance(item, str):
            value = item.strip()
            if value:
                options.append({"label": value, "value": value})
            continue
        if not isinstance(item, dict):
            continue
        value = str(item.get("value", "")).strip()
        if not value:
            continue
        label = str(item.get("label") or value).strip()
        options.append({"label": label, "value": value})
    return options


def is_secret(step: dict[str, Any]) -> bool:
    """Must this answer stay out of the chat transcript and out of any echo?"""
    return bool(step.get("secret")) or str(step.get("input_type") or "") == _SECRET_INPUT_TYPE


def is_skippable(step: dict[str, Any]) -> bool:
    """May **[Skip]** be offered for this step?

    ``allow_skip`` decides when it is present, in **both** directions — that is the whole reason
    it is a separate key rather than a synonym for ``not required``. ``required`` today means two
    things at once ("must hold a value" and "must be asked"), and the SSH branch needs them apart:
    inside its branch ``remote_password_ref`` must be answered, outside it the question never
    happens. Without an explicit key, the default follows ``required``, which is what every step
    written before this module meant.
    """
    declared = step.get("allow_skip")
    if isinstance(declared, bool):
        return declared
    return not bool(step.get("required", True))


def is_asked_when_optional(step: dict[str, Any]) -> bool:
    """Should this *optional* step be asked at all?

    An optional parameter has never been prompted for: the walk that picks the next question only
    considers required ones, and an optional value the operator did not type simply stays empty.
    That is right for most of them — nobody wants `/spbot_trace_session` to start interrogating
    people about arguments they left out on purpose — and it is also why a Skip button could not
    exist: the only steps ever shown were the ones that must be answered.

    So asking is **opt-in**, spelled by declaring ``allow_skip`` true. A step that says "you may
    skip me" is a step that means to be offered; one that says nothing keeps the old behaviour
    exactly.
    """
    return step.get("allow_skip") is True and not bool(step.get("required", True))


def skip_value(step: dict[str, Any]) -> str:
    """What is stored when a step is skipped."""
    declared = step.get("skip_value")
    return str(declared) if declared is not None else DEFAULT_SKIP_VALUE


def accepts_free_text(step: dict[str, Any]) -> bool:
    """May the operator type something other than one of the offered options?"""
    declared = step.get("allow_text_input")
    if isinstance(declared, bool):
        return declared
    return True


def answers_by_name(steps: list[dict[str, Any]], args: list[str]) -> dict[str, str]:
    """The answers so far, keyed by step name — the input every branch condition reads."""
    answered: dict[str, str] = {}
    for step in steps:
        name = str(step.get("name") or "").strip()
        if not name:
            continue
        position = int(step.get("position", 1))
        answered[name] = str(args[position - 1] if len(args) >= position else "").strip()
    return answered


def ask_when_holds(step: dict[str, Any], answers: dict[str, str]) -> bool:
    """Does this step's ``ask_when`` branch condition hold? A step without one is always asked.

    The declarative replacement for the two hardcoded conditions in ``command_processor`` — those
    each understood exactly one question, so every new branch meant new Python. Written against
    answers rather than positions because a branch that renumbers its steps must keep working::

        "ask_when": {"parameter": "remote_auth", "equals": "secret_ref"}
        "ask_when": {"parameter": "deploy_target", "not_equals": "worker"}
        "ask_when": {"parameter": "engine", "in": ["mssql", "oracle"]}

    **An unanswered controlling parameter means "do not ask yet".** The step is reconsidered on
    every pass, so a branch whose condition is not yet decidable is simply asked later, once the
    answer it depends on exists. Returning true instead would ask the branch's questions before
    the branch is chosen, which is the bug this key exists to remove.
    """
    rule = step.get("ask_when")
    if not isinstance(rule, dict):
        return True
    name = str(rule.get("parameter") or "").strip()
    if not name:
        return True
    if name not in answers:
        return False
    value = str(answers.get(name, "")).strip()
    if not value:
        return False
    if "equals" in rule:
        return value == str(rule.get("equals"))
    if "not_equals" in rule:
        return value != str(rule.get("not_equals"))
    if "in" in rule:
        allowed = rule.get("in")
        return isinstance(allowed, list) and value in {str(item) for item in allowed}
    if "not_in" in rule:
        blocked = rule.get("not_in")
        return isinstance(blocked, list) and value not in {str(item) for item in blocked}
    # A rule this build does not understand asks the question. The alternative — silently not
    # asking — hides a required step and the run fails much later, with the CLI complaining about
    # an argument the operator was never offered.
    return True


def clear_unreachable_answers(steps: list[dict[str, Any]], args: list[str]) -> list[str]:
    """Blank the answers of steps the current branch no longer reaches.

    Going Back and choosing a different branch is exactly when this matters: answer ``password``,
    give one, go back, choose ``key_file`` — and without this the run still carries the password
    from the abandoned branch and hands it to the CLI. The visible flow said the answer was
    discarded; only the args list disagreed.
    """
    cleared = list(args)
    for step in steps:
        position = int(step.get("position", 1))
        if len(cleared) < position:
            continue
        if not ask_when_holds(step, answers_by_name(steps, cleared)):
            cleared[position - 1] = ""
    return cleared


def keyboard_for(step: dict[str, Any], *, can_go_back: bool) -> dict[str, Any]:
    """The reply keyboard for one prompt: the step's options, then the control row.

    A **reply** keyboard, not an inline one, so a tap arrives as an ordinary text message through
    the intake that already works — and typing the same word stays exactly as valid. ``selective``
    shows it to the person the prompt replies to rather than to a whole operations group, and
    ``one_time_keyboard`` folds it away once used so the chat is not left wearing a keyboard for a
    question that has been answered.

    Cancel is on every prompt, Back on every prompt that has somewhere to go, Skip only where
    :func:`is_skippable` says so — see that function for why that is not the same as
    ``not required``.
    """
    rows: list[list[dict[str, str]]] = []
    options = option_list(step)
    if options and not is_secret(step):
        # Two per row: long values (an IP, a secret ref) are unreadable three-up on a phone.
        for index in range(0, len(options), 2):
            rows.append([{"text": option["label"]} for option in options[index:index + 2]])
    controls: list[dict[str, str]] = []
    if can_go_back:
        controls.append({"text": BACK_LABEL})
    if is_skippable(step):
        controls.append({"text": SKIP_LABEL})
    controls.append({"text": CANCEL_LABEL})
    rows.append(controls)
    return {
        "keyboard": rows,
        "resize_keyboard": True,
        "one_time_keyboard": True,
        "selective": True,
    }


def hide_keyboard() -> dict[str, Any]:
    """Take the keyboard away — sent with the last message of a workflow.

    Without it the buttons of the final question stay on the operator's screen after the workflow
    has ended, and tapping ``Back`` then answers nothing at all.
    """
    return {"remove_keyboard": True, "selective": True}


def control_hint(step: dict[str, Any], *, can_go_back: bool) -> str:
    """One line naming what can be typed, for clients that do not show the keyboard.

    A keyboard is a convenience; the words are the interface. A person on a client that hides
    custom keyboards, or reading the message in a notification, still has to be told that ``back``
    is a thing they may type.
    """
    words = []
    if can_go_back:
        words.append("back")
    if is_skippable(step):
        words.append("skip")
    words.append("cancel")
    return "Type " + " / ".join(words) + " at any point."
