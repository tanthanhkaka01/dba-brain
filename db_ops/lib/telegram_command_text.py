"""How a bot command is written down: text in, text out.

Reading a command message and writing one back are the same rule seen from two sides, and both
are pure text — no store, no config, no Telegram. They lived in the Telegram app because only the
dispatcher needed them; then :mod:`db_ops.common.telegram_command_history` needed to *rebuild* a
command line from what a person typed, and `common` may not import an app. So the rule moved here
and the app re-exports it, the same shape as ``telegram_severity``.

The two directions have to agree, which is why they are one module:
:func:`parse_command_message` splits a message the way the dispatcher does, and
:func:`render_command_line` joins a command back into the line a person would type to repeat it.
"""

from __future__ import annotations

import re
import shlex
from typing import Any


def command_key_from_message(command_prefix: str, command_payload: str) -> str:
    """The catalogue key for a message, from the two columns the sync writes.

    ``/spbot_run_sql_task 28`` is stored as prefix ``/spbot`` and payload ``_run_sql_task 28``,
    so the key is the prefix and the payload's first word, normalized.
    """
    prefix = normalize_command_text(command_prefix)
    payload = normalize_command_text(strip_bot_username(first_command_token(command_payload)))
    if payload:
        return f"{prefix}_{payload}"
    return prefix


def parse_command_message(text: str) -> dict[str, Any]:
    tokens = split_command_tokens(text)
    if not tokens:
        return {"command_key": "", "args": []}
    command_token = strip_bot_username(tokens[0])
    return {
        "command_key": normalize_command_text(command_token),
        "args": tokens[1:],
    }


def split_command_tokens(text: str) -> list[str]:
    try:
        return shlex.split(text.strip())
    except ValueError:
        return text.strip().split()


def normalize_command_text(value: str) -> str:
    text = value.strip().lower()
    text = text.lstrip("/")
    text = re.sub(r"[^a-z0-9_]+", "_", text)
    text = re.sub(r"_+", "_", text)
    return text.strip("_")


def first_command_token(value: str) -> str:
    return value.strip().split(maxsplit=1)[0] if value.strip() else ""


def strip_bot_username(value: str) -> str:
    text = value.strip()
    if "@" in text:
        return text.split("@", 1)[0]
    return text


def render_command_line(command_text: str, args: list[str] | tuple[str, ...] = ()) -> str:
    """``/spbot_run_sql_task 18 0 30`` — one command and its answers, as a person would type it.

    A command answered one prompt at a time arrives as several messages, and its arguments are
    kept positionally. Written back on one line they are the *same* command: the dispatcher's
    inline form reads position 1, 2, 3 exactly as the prompt loop filled them.

    Arguments are joined plainly, because the last one is often ``consume_rest`` — the rest of
    the message, spaces and all — and quoting it would show the reader something they did not
    type. An earlier argument holding a space is quoted, since without that the line would parse
    back with its later arguments shifted by one, which is a different command.

    Trailing empty arguments are dropped: an optional answer nobody gave is not part of the line.
    """
    values = [str(value) for value in args]
    while values and values[-1].strip() == "":
        values.pop()
    last = len(values) - 1
    rendered = [
        value if index == last or _is_one_token(value) else shlex.quote(value)
        for index, value in enumerate(values)
    ]
    return " ".join([f"/{normalize_command_text(command_text)}", *rendered]).rstrip()


def _is_one_token(value: str) -> bool:
    """Whether this value survives :func:`split_command_tokens` as exactly itself."""
    try:
        return split_command_tokens(value) == [value]
    except ValueError:  # pragma: no cover - split_command_tokens already handles it
        return False
