"""The one place db_ops asks a human before doing something it cannot undo.

Every dangerous operation — restart a host, stop a database service, patch an instance, and
whatever is added next — goes through :func:`require_confirmation`. Not a copy per command: the
whole point of a safety control is that it behaves identically everywhere, so an operator learns
it once and cannot be surprised by the one command that spells it differently.

**Two locks, and they are not the same lock.**

* ``"confirm": true`` in the request is *intent*: whoever composed this payload meant to change
  the machine. A payload is a file, a Telegram action, a shell history entry — it can be reused,
  copied, or replayed against a target it was never written for.
* Typing ``yes`` at the prompt is *presence*: a human is looking at **this** target, right now,
  reading what is about to happen to it.

Intent without presence is how the right command reaches the wrong host. So at a terminal both
are required, and the prompt names the target and the consequence rather than asking a bare
"are you sure?" — a question with no content trains people to answer it without reading.

**Automation is a declaration, not a default.** With no terminal to ask on, the operation is
refused unless the request says ``"assume_yes": true``. A scheduled job that forgot to say "no
human will be asked here" fails loudly instead of quietly restarting a production host at 03:00.
A request piped in on stdin is *not* the unattended case: the question is asked on the
controlling terminal, which the pipe did not take away (see :func:`open_terminal`).

Whatever authorized the run is recorded on the gate and in ``report.facts["authorization"]``, so
the evidence file answers *who allowed this* — a typed confirmation and an unattended one are
different facts and must not read the same afterwards.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Sequence, TextIO

from db_ops.common.evidence import FAIL, OK, GateReport
from db_ops.lib.paths import TOOL_ROOT  # noqa: F401 - one definition, see that module

__all__ = [
    "CONFIRM_WORD",
    "authorize_operation",
    "DEFAULT_OPERATIONS_PATH",
    "banner",
    "is_interactive",
    "load_operation",
    "open_terminal",
    "read_answer",
    "require_confirmation",
]

# The whole word, nothing shorter. "y" is what a hand types while reading something else.
CONFIRM_WORD = "yes"

_RULE = "=" * 72

DEFAULT_OPERATIONS_PATH = TOOL_ROOT / "data" / "emergency_operations.json"


def load_operation(
    operation: str, *, path: str | Path = DEFAULT_OPERATIONS_PATH
) -> dict[str, Any]:
    """How hard ``operation`` is to authorize: ``{"level", "confirmations", "challenge", "effects"}``.

    An operation the file does not list gets the **strictest** answer, not the weakest: two
    confirmations and a typed target. A command added to the CLI but forgotten in the config must
    become harder to run, never easier — the failure mode of the opposite default is a destructive
    command that quietly needs no confirmation at all.
    """
    strictest = {"level": 100, "confirmations": 2, "challenge": "target_id", "effects": []}
    try:
        data = json.loads(Path(path).read_bytes().decode("utf-8-sig"))
    except (OSError, ValueError):
        return strictest
    entry = (data.get("operations") or {}).get(operation)
    if not isinstance(entry, dict):
        return strictest
    level = entry.get("level", 100)
    rules = (data.get("levels") or {}).get(str(level))
    if not isinstance(rules, dict):
        return strictest
    return {
        "level": int(level),
        "confirmations": int(rules.get("confirmations", 2)),
        "challenge": str(rules.get("challenge") or ""),
        "effects": [str(item) for item in (entry.get("effects") or [])],
    }


def open_terminal() -> TextIO | None:
    """The controlling terminal, opened for reading — or ``None`` when there is none.

    Needed because ``stdin`` is not always free to ask on: the request itself may have arrived
    through it (``... host-restart - < request.json``). Reading the answer from an exhausted pipe
    returns EOF instantly, which would abort a perfectly legitimate operation and teach operators
    that the prompt is broken. The terminal is a separate device from the pipe, so it is still
    there. ``/dev/tty`` on POSIX, ``CON`` on Windows; both fail when nothing is attached, which is
    exactly the answer we want in a container or under a scheduler.
    """
    for device in ("/dev/tty", "CON"):
        try:
            return open(device, "r", encoding="utf-8", errors="replace")
        except OSError:
            continue
    return None


def is_interactive() -> bool:
    """True when there is a human who can be asked — on stdin, or on the terminal behind it.

    A module-level function (rather than an inline ``sys.stdin.isatty()``) so the answer has one
    definition — and so a test can state which world it is in.
    """
    try:
        if sys.stdin is not None and sys.stdin.isatty():
            return True
    except (AttributeError, ValueError):  # closed or replaced stdin
        pass
    terminal = open_terminal()
    if terminal is None:
        return False
    terminal.close()
    return True


def banner(*, operation: str, target: str, effects: Sequence[str], reason: str = "") -> str:
    """The text shown before the prompt: what will happen, to what, and why.

    Everything an operator needs to catch a wrong target is on screen at the moment of the
    decision. A confirmation that does not name the host is a confirmation of nothing.
    """
    lines = [_RULE, "  DANGEROUS OPERATION — this changes a live system", _RULE,
             f"  Operation : {operation}", f"  Target    : {target}"]
    for index, effect in enumerate([str(item) for item in effects if str(item).strip()]):
        lines.append(f"  {'Effect    : ' if index == 0 else '            '}{effect}")
    if reason:
        lines.append(f"  Reason    : {reason}")
    lines.append(_RULE)
    return "\n".join(lines)


def read_answer(prompt: str, *, stream: TextIO | None = None) -> str:
    """Ask on the terminal and read one line.

    The prompt is written to ``stream`` (stderr by default) rather than through ``input``'s own
    prompt argument, because stdout carries the JSON result — a question printed there would end
    up inside whatever is parsing it.
    """
    target = stream or sys.stderr
    try:
        print(prompt, end="", file=target, flush=True)
        if sys.stdin is not None and sys.stdin.isatty():
            return sys.stdin.readline()
        terminal = open_terminal()
        if terminal is None:
            return ""
        with terminal:
            return terminal.readline()
    except (EOFError, KeyboardInterrupt, OSError, ValueError):
        # Ctrl-C, a closed stdin, or no readable terminal at a confirmation prompt all mean no —
        # never "carry on".
        return ""


def require_confirmation(
    report: GateReport,
    request: dict[str, Any],
    *,
    operation: str,
    target: str,
    effects: Sequence[str] = (),
    gate_name: str = "confirm",
    stream: TextIO | None = None,
    interactive: bool | None = None,
    input_fn: Callable[[str], str] | None = None,
    confirmations: int = 1,
    challenge: str = "",
) -> bool:
    """Authorize one dangerous operation. Returns True only if it may proceed.

    Records exactly one gate (``gate_name``) either way, so a refusal is as visible in the
    evidence as an approval. Callers check ``dry_run`` **before** calling this: rehearsing an
    operation is not performing one, and asking for confirmation of something that will not
    happen teaches people to type ``yes`` without reading.

    ``confirmations`` is how many answers the operation costs, and ``challenge`` is what the
    second one must be — the target's own id, for the operations that take a machine down. Two
    identical ``yes`` answers in a row are one answer typed twice: the hand learns the rhythm and
    stops reading. Having to copy the target's id out of the prompt cannot be done without
    looking at it, and it makes a payload written for one host fail against another.

    **Answers may arrive in the request instead of at a terminal** — ``"confirm": "yes"`` and
    ``"confirm_target": "<id>"``. That is not a bypass: it is how a human answers when the
    terminal is not the channel. The Telegram command processor asks its prompts one at a time and
    puts the replies here, so the same gate, the same words and the same target check apply to a
    phone at 3 a.m. and to a shell. ``"confirm": true`` stays *intent only* — a bare boolean has
    answered nothing, so a terminal run still asks.
    """
    reason = str(request.get("reason") or "").strip()
    decided_at = datetime.now().astimezone().isoformat(timespec="seconds")
    channel = str((request.get("authorized_by") or {}).get("channel") or "").strip() \
        if isinstance(request.get("authorized_by"), dict) else ""

    if not bool(request.get("confirm")):
        report.add(
            gate_name,
            FAIL,
            'this operation changes a live system; the request must declare intent with '
            '"confirm": true (or "dry_run": true to rehearse it). Nothing was executed.',
        )
        report.note("authorization", {"authorized": False, "by": "none",
                                      "reason": "confirm not set", "at": decided_at})
        return False

    if interactive is None:
        interactive = is_interactive()

    if bool(request.get("assume_yes")):
        # An explicit, greppable waiver. Honoured at a terminal too: a scripted run must behave
        # the same whether or not someone happens to be logged in.
        report.add(
            gate_name,
            OK,
            'authorized by "assume_yes": true in the request — no human was prompted',
        )
        report.note("authorization", {"authorized": True, "by": "assume_yes",
                                      "interactive": interactive, "at": decided_at})
        return True

    # What this operation costs, in order. Each step is answered from the request when the answer
    # is already there, and asked on the terminal when it is not.
    steps: list[dict[str, str]] = [{
        "field": "confirm",
        "expected": CONFIRM_WORD,
        "prompt": f'  Type "{CONFIRM_WORD}" to proceed (anything else aborts): ',
    }]
    if confirmations >= 2:
        wanted = str(challenge or target).strip()
        steps.append({
            "field": "confirm_target",
            "expected": wanted,
            "prompt": f"  Still sure? Type the target id exactly to proceed: {wanted}\n  > ",
        })

    banner_shown = False
    answered_by: list[str] = []
    for index, step in enumerate(steps, start=1):
        raw = request.get(step["field"])
        # A bare boolean is intent, not an answer; only text can have been typed by somebody.
        supplied = str(raw).strip() if isinstance(raw, str) else ""

        if not supplied:
            if not interactive:
                report.add(
                    gate_name,
                    FAIL,
                    f"there is no terminal to ask on and the request does not carry answer "
                    f"{index} of {len(steps)} ({step['field']!r}). Run it from a terminal, or "
                    f"send {step['field']!r} in the request if a human answered it elsewhere, or "
                    'add "assume_yes": true if this is genuinely unattended automation. '
                    "Nothing was executed.",
                )
                report.note("authorization", {"authorized": False, "by": "none",
                                              "reason": f"missing {step['field']}",
                                              "at": decided_at})
                return False
            if not banner_shown:
                print(banner(operation=operation, target=target, effects=effects, reason=reason),
                      file=stream or sys.stderr, flush=True)
                banner_shown = True
            ask = input_fn or (lambda prompt: read_answer(prompt, stream=stream))
            supplied = str(ask(step["prompt"]) or "").strip()
            source = "prompt"
        else:
            source = "request"

        if supplied.casefold() != step["expected"].casefold():
            # The wrong target id is the interesting failure: it usually means a payload aimed at
            # another host, so the evidence says what was expected as well as what arrived.
            report.add(
                gate_name,
                FAIL,
                f"aborted at confirmation {index} of {len(steps)}: expected "
                f"{step['expected']!r} for {step['field']!r}, got {supplied or '<empty>'!r}. "
                "Nothing was executed.",
            )
            report.note("authorization", {"authorized": False, "by": source,
                                          "step": step["field"], "answer": supplied,
                                          "expected": step["expected"], "at": decided_at})
            return False
        answered_by.append(source)

    how = " and ".join(f"{step['field']} via {src}" for step, src in zip(steps, answered_by))
    report.add(
        gate_name,
        OK,
        f"confirmed at {decided_at} — {len(steps)} of {len(steps)} answers accepted ({how})"
        + (f", channel={channel}" if channel else ""),
    )
    report.note("authorization", {
        "authorized": True,
        "by": answered_by[-1],
        "confirmations": len(steps),
        "answered_by": answered_by,
        "channel": channel,
        "at": decided_at,
    })
    return True


def authorize_operation(
    report: GateReport,
    request: dict[str, Any],
    *,
    operation: str,
    target_id: str,
    target_label: str,
    extra_effects: Sequence[str] = (),
) -> bool:
    """One gate for a named operation, with the cost read from ``emergency_operations.json``.

    Lives here rather than beside any one caller because it is the pairing that makes the two
    config files agree: ``load_operation`` decides *how hard* to confirm, ``require_confirmation``
    performs it, and the typed challenge is the target's own id.

    The challenge is deliberately ``target_id`` and never the free-text label — the operator has
    to reproduce the thing the payload will actually act on, so a payload written for one host is
    rejected by another rather than waved through by muscle memory.
    """
    rules = load_operation(operation)
    return require_confirmation(
        report,
        request,
        operation=operation,
        target=target_label,
        effects=[*rules["effects"], *extra_effects],
        confirmations=int(rules["confirmations"]),
        challenge=target_id,
    )
