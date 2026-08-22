"""What a ``/spbot_list_*`` reply shows: the things an operator can act on.

Every listing command in db_ops answers the same question - "what can I run right now?" - so a
disabled entry does not belong in the answer. It cannot be run, and printing it next to the
runnable ones invites someone to type an id that will be refused, or to read a listing of eleven
backup ids as eleven working backups.

Hiding is not enough on its own. Someone who knows an id exists in the config and does not see it
must be able to tell "disabled" from "lost", so a listing that dropped anything says how many it
dropped. That pairing - filter, then account for what was filtered - is the whole rule, and it
lives here once because five commands implement it (backups, restores, server targets, sql_tasks,
metrics) and five separate copies drifted into four different spellings of "off".
"""

from __future__ import annotations

from typing import Any, Callable, Iterable, TypeVar

T = TypeVar("T")


def is_active(item: Any) -> bool:
    """Whether an entry is switched on, reading either an attribute or a mapping key.

    Absent means active: every config in this project treats a missing ``active`` as on, and a
    listing must not hide an entry the scheduler would happily run.
    """
    if isinstance(item, dict):
        return bool(item.get("active", True))
    return bool(getattr(item, "active", True))


def active_only(
    items: Iterable[T],
    *,
    key: Callable[[T], bool] = is_active,
) -> tuple[list[T], int]:
    """The runnable entries, plus how many were left out.

    Returned as a pair so the caller cannot report the filtered listing without the count -
    which is the half that keeps "hidden" from reading as "missing".
    """
    kept: list[T] = []
    hidden = 0
    for item in items:
        if key(item):
            kept.append(item)
        else:
            hidden += 1
    return kept, hidden


#: How many choices a prompt offers before it stops listing and says how many are left.
#: A prompt is one Telegram message against a 4096-char cap, and the estate has instances with
#: well over a hundred databases. The cap exists so a long list degrades into a short list plus an
#: honest count, rather than into a message that fails to send and leaves the flow with no prompt
#: at all.
MAX_PROMPT_CHOICES = 40


def choice_lines(names: Iterable[str], *, limit: int = MAX_PROMPT_CHOICES) -> str:
    """The choices to show under a prompt, capped, with the remainder accounted for.

    Same pairing as :func:`active_only` and :func:`hidden_note` above, for the same reason: a
    listing that silently stopped at 40 tells the operator their database does not exist. It has
    to say that it stopped.

    Returns ``""`` for no names at all, so a caller can append the result unconditionally and get
    the bare prompt back when there is nothing to add.
    """
    kept = [str(name).strip() for name in names if str(name).strip()]
    if not kept:
        return ""
    shown = kept[:limit] if limit > 0 else kept
    remaining = len(kept) - len(shown)
    text = "\n".join(f"  {index}. {name}" for index, name in enumerate(shown, start=1))
    if remaining > 0:
        text += f"\n  ... and {remaining} more (type the name)"
    return text


def hidden_note(hidden: int, *, noun: str = "entry") -> str:
    """One line accounting for the entries a listing left out, or ``""`` when it left out none."""
    if hidden <= 0:
        return ""
    plural = noun if noun.endswith("s") else (f"{noun[:-1]}ies" if noun.endswith("y") else f"{noun}s")
    # ASCII only: this line is printed to a Windows console (cp1252) as well as sent to Telegram,
    # and a listing must not die on its own footnote.
    return f"({hidden} inactive {plural if hidden != 1 else noun} hidden; set active:true to use them.)"
