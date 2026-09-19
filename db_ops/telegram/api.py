from __future__ import annotations
from db_ops.lib.telegram_text import TELEGRAM_MESSAGE_LIMIT  # noqa: F401 - one definition, see that module

import json
import logging
import mimetypes
import time
import uuid
from pathlib import Path
from typing import Any
from urllib import error, parse, request

from db_ops.lib.telegram_text import split_telegram_message
from db_ops.logging_ops import log_event
from db_ops.telegram.severity import decorate_message


DEFAULT_TELEGRAM_API_URL = "https://api.telegram.org"

#: How long to wait when Telegram rate-limits a call and does **not** say for how long. It always
#: does in practice; this is the floor for the case where the body is unreadable.
DEFAULT_RATE_LIMIT_WAIT_SECONDS = 5

#: The longest this layer will wait on one 429. Telegram can ask for minutes after a flood, and a
#: sleep longer than the daemon's own cycle is indistinguishable from a hang: past this, the part
#: is given back to the queue, which will bring the row round again.
MAX_RATE_LIMIT_WAIT_SECONDS = 60

#: Attempts per part, including the first. Two waits is enough for the burst this exists for -
#: a dozen-part report handed to Telegram at once - and short enough that a genuinely throttled
#: chat is handed back to the queue rather than held here.
RATE_LIMIT_ATTEMPTS = 3

#: The gap between the parts of one body. Telegram allows roughly 20 messages a minute to one
#: group and answers 429 above it. A dozen parts posted with no gap is over that limit by itself,
#: which is how a report arrived with its middle missing.
PART_PAUSE_SECONDS = 0.35


class TelegramRateLimited(RuntimeError):
    """Telegram refused this call for **its own** rate limit, and said for how long.

    A distinct type because the response is different from every other failure: nothing is wrong
    with the message, the token or the chat, and retrying after the interval Telegram hands back
    is the documented, expected thing to do. It used to arrive as a bare ``RuntimeError`` reading
    ``Telegram HTTP 429: ...``, so the queue's retry loop spent all three attempts inside the same
    second and marked the row failed - the row's whole output lost to a limit that had asked for a
    two-second pause.
    """

    def __init__(self, message: str, retry_after: float) -> None:
        super().__init__(message)
        self.retry_after = float(retry_after)


def _retry_after_seconds(body: str) -> float:
    """The ``retry_after`` Telegram sends with a 429, clamped to something a caller can wait for."""
    seconds: Any = None
    try:
        parsed = json.loads(body)
        seconds = (parsed.get("parameters") or {}).get("retry_after")
    except (ValueError, AttributeError):
        seconds = None
    try:
        wait = float(seconds)
    except (TypeError, ValueError):
        wait = float(DEFAULT_RATE_LIMIT_WAIT_SECONDS)
    return max(0.0, wait)


def _log_delivery(level: str, message: str) -> None:
    """Say it in the app log, not only in the store.

    On 2026-09-09 a report lost parts to a 429 and the only record anywhere was ``metadata_json``
    on the queue row. A warning nobody received is a warning that did not happen, so the loss has
    to be visible where an operator reads failures. The logger is the one the telegram CLI
    configures; outside it this is a no-op, which is what a test wants.
    """
    log_event(logging.getLogger("telegram"), level=level, message=message)



def call_telegram_api(
    *,
    bot_token: str,
    method_name: str,
    payload: dict[str, Any] | None = None,
    api_url: str = DEFAULT_TELEGRAM_API_URL,
    timeout_seconds: int = 20,
) -> dict[str, Any]:
    if not bot_token:
        raise RuntimeError("Telegram bot token is empty.")
    if not method_name:
        raise RuntimeError("Telegram method name is empty.")

    url = f"{api_url.rstrip('/')}/bot{bot_token}/{method_name}"
    encoded_payload = parse.urlencode(payload or {}).encode("utf-8")
    req = request.Request(
        url,
        data=encoded_payload,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )

    try:
        with request.urlopen(req, timeout=timeout_seconds) as response:
            body = response.read().decode("utf-8")
    except error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        # 429 is Telegram asking for a pause, not refusing the message. It carries
        # `parameters.retry_after`, and the caller can only honour it if this layer keeps it.
        if exc.code == 429:
            raise TelegramRateLimited(
                f"Telegram HTTP 429: {body}", _retry_after_seconds(body)) from exc
        raise RuntimeError(f"Telegram HTTP {exc.code}: {body}") from exc
    except error.URLError as exc:
        raise RuntimeError(f"Telegram request failed: {exc.reason}") from exc

    result = json.loads(body)
    if not result.get("ok"):
        raise RuntimeError(f"Telegram returned not ok: {body}")
    return result


def send_message(
    *,
    bot_token: str,
    chat_id: str,
    text: str,
    api_url: str = DEFAULT_TELEGRAM_API_URL,
    timeout_seconds: int = 20,
    disable_web_page_preview: bool = True,
    reply_to_message_id: int | None = None,
    reply_markup: dict[str, Any] | None = None,
    message_type: str | None = None,
    part_pause_seconds: float = PART_PAUSE_SECONDS,
) -> dict[str, Any]:
    if not chat_id:
        raise RuntimeError("Telegram chat id is empty.")
    if not text:
        raise RuntimeError("Telegram message text is empty.")

    # Telegram rejects a body longer than 4096 chars with HTTP 400 ("message is too long"), so an
    # over-long body goes out as several `[part i/n]` messages rather than being clipped. Every
    # producer inherits this: whatever it hands over arrives whole. See
    # db_ops.lib.telegram_text for why splitting beats cutting.
    #
    # Split BEFORE decorating, not after: the emoji belongs in front of the part marker, and
    # db_ops.telegram.severity reads that marker to tell a first chunk from a continuation — a
    # continuation starts mid-body where "running" is a column in a lock dump, not a status.
    #
    # If a later part fails, the exception propagates and send_queue retries the whole row, which
    # re-sends the parts that already landed. Duplicated output on a rare failure is the better
    # trade against a result that arrives with a hole in the middle and says nothing about it.
    parts = split_telegram_message(text)
    first_result: dict[str, Any] | None = None
    for index, part in enumerate(parts):
        # Paced, because the limit this hits is Telegram's own: roughly 20 messages a minute to
        # one group. A dozen parts posted back to back is over it before the first one is read,
        # and what came back was a 429 that lost the rest of the body.
        if index:
            time.sleep(part_pause_seconds)
        payload: dict[str, Any] = {
            "chat_id": str(chat_id),
            # Severity emoji goes on here, once, for every producer — see db_ops.telegram.severity.
            # `message_type` is what the producer stored on the row; when it is absent the header
            # is read instead.
            "text": decorate_message(part, message_type),
            "disable_web_page_preview": str(disable_web_page_preview).lower(),
        }
        # The quote belongs on the first part only — repeating it would quote the original once
        # per part. Buttons go on the last, which is where the reader ends up.
        if reply_to_message_id is not None and index == 0:
            payload["reply_to_message_id"] = reply_to_message_id
        if reply_markup is not None and index == len(parts) - 1:
            payload["reply_markup"] = json.dumps(reply_markup, ensure_ascii=False)

        result = _send_part_honouring_rate_limit(
            bot_token=bot_token,
            payload=payload,
            api_url=api_url,
            timeout_seconds=timeout_seconds,
            part_number=index + 1,
            part_count=len(parts),
            chat_id=str(chat_id),
        )
        # The first part's id is the one recorded against the queue row: it is where the output
        # starts, so a reply that quotes it quotes the beginning and not the tail.
        if first_result is None:
            first_result = result
    return first_result or {}


def _send_part_honouring_rate_limit(
    *,
    bot_token: str,
    payload: dict[str, Any],
    api_url: str,
    timeout_seconds: int,
    part_number: int,
    part_count: int,
    chat_id: str,
) -> dict[str, Any]:
    """One ``sendMessage``, waiting out a 429 the number of seconds Telegram asked for.

    Retrying *at all* is what was missing. The queue above does retry, but immediately and three
    times, so a limit asking for two seconds consumed every attempt inside one second and the row
    was marked failed - a multi-part report lost its later parts and the only record anywhere was
    ``metadata_json`` on that row.

    Waiting is capped: past :data:`MAX_RATE_LIMIT_WAIT_SECONDS` the part is handed back to the
    queue instead, because a chat that is throttled for minutes is a queue problem, and a send
    layer asleep longer than the daemon's cycle looks exactly like a hang.
    """
    where = f"part {part_number}/{part_count} to chat {chat_id}"
    for attempt in range(1, RATE_LIMIT_ATTEMPTS + 1):
        try:
            return call_telegram_api(
                bot_token=bot_token,
                method_name="sendMessage",
                payload=payload,
                api_url=api_url,
                timeout_seconds=timeout_seconds,
            )
        except TelegramRateLimited as exc:
            wait = min(exc.retry_after, float(MAX_RATE_LIMIT_WAIT_SECONDS))
            if attempt == RATE_LIMIT_ATTEMPTS or exc.retry_after > MAX_RATE_LIMIT_WAIT_SECONDS:
                _log_delivery("error", (
                    f"telegram.send_message rate limited and NOT delivered: {where}, "
                    f"Telegram asked for {exc.retry_after:.0f}s after {attempt} attempt(s). "
                    "The message goes back to the queue."))
                raise
            _log_delivery("warning", (
                f"telegram.send_message rate limited: {where}, waiting {wait:.1f}s "
                f"(attempt {attempt} of {RATE_LIMIT_ATTEMPTS})"))
            time.sleep(wait)
    raise RuntimeError(f"Telegram send gave up on {where}.")  # unreachable; the loop returns or raises


def send_document(
    *,
    bot_token: str,
    chat_id: str,
    document_path: str | Path,
    caption: str = "",
    api_url: str = DEFAULT_TELEGRAM_API_URL,
    timeout_seconds: int = 60,
    reply_to_message_id: int | None = None,
) -> dict[str, Any]:
    if not chat_id:
        raise RuntimeError("Telegram chat id is empty.")
    path = Path(document_path)
    if not path.is_file():
        raise FileNotFoundError(f"Telegram document not found: {path}")

    payload: dict[str, Any] = {"chat_id": str(chat_id)}
    if caption:
        payload["caption"] = caption
    if reply_to_message_id is not None:
        payload["reply_to_message_id"] = reply_to_message_id

    return call_telegram_multipart_api(
        bot_token=bot_token,
        method_name="sendDocument",
        payload=payload,
        file_field="document",
        file_path=path,
        api_url=api_url,
        timeout_seconds=timeout_seconds,
    )


def call_telegram_multipart_api(
    *,
    bot_token: str,
    method_name: str,
    payload: dict[str, Any],
    file_field: str,
    file_path: Path,
    api_url: str = DEFAULT_TELEGRAM_API_URL,
    timeout_seconds: int = 60,
) -> dict[str, Any]:
    if not bot_token:
        raise RuntimeError("Telegram bot token is empty.")
    boundary = f"dbops-{uuid.uuid4().hex}"
    body = build_multipart_body(
        boundary=boundary,
        payload=payload,
        file_field=file_field,
        file_path=file_path,
    )
    req = request.Request(
        f"{api_url.rstrip('/')}/bot{bot_token}/{method_name}",
        data=body,
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
        method="POST",
    )
    try:
        with request.urlopen(req, timeout=timeout_seconds) as response:
            response_body = response.read().decode("utf-8")
    except error.HTTPError as exc:
        response_body = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Telegram HTTP {exc.code}: {response_body}") from exc
    except error.URLError as exc:
        raise RuntimeError(f"Telegram request failed: {exc.reason}") from exc

    result = json.loads(response_body)
    if not result.get("ok"):
        raise RuntimeError(f"Telegram returned not ok: {response_body}")
    return result


def build_multipart_body(
    *,
    boundary: str,
    payload: dict[str, Any],
    file_field: str,
    file_path: Path,
) -> bytes:
    chunks: list[bytes] = []
    for key, value in payload.items():
        chunks.extend(
            [
                f"--{boundary}\r\n".encode("utf-8"),
                f'Content-Disposition: form-data; name="{key}"\r\n\r\n'.encode("utf-8"),
                str(value).encode("utf-8"),
                b"\r\n",
            ]
        )
    content_type = mimetypes.guess_type(file_path.name)[0] or "application/octet-stream"
    chunks.extend(
        [
            f"--{boundary}\r\n".encode("utf-8"),
            (
                f'Content-Disposition: form-data; name="{file_field}"; '
                f'filename="{file_path.name}"\r\n'
            ).encode("utf-8"),
            f"Content-Type: {content_type}\r\n\r\n".encode("utf-8"),
            file_path.read_bytes(),
            b"\r\n",
            f"--{boundary}--\r\n".encode("utf-8"),
        ]
    )
    return b"".join(chunks)


#: Telegram's own ceiling on what a bot may *download*. Not ours and not configurable: `getFile`
#: answers "file is too big" above it, on the public api.telegram.org, whatever the sender saw
#: when they attached the file (a user may upload 2 GB; a bot may fetch 20 MB of it). Named here
#: so the failure can say so instead of quoting an arbitrary number.
TELEGRAM_BOT_DOWNLOAD_LIMIT = 20 * 1024 * 1024


def get_file_bytes(
    *,
    bot_token: str,
    file_id: str,
    api_url: str = DEFAULT_TELEGRAM_API_URL,
    timeout_seconds: int = 120,
    max_bytes: int | None = None,
) -> bytes:
    """Download an inbound Telegram file (document) by ``file_id``.

    Two steps per the Bot API: ``getFile`` returns a ``file_path``, then the file
    is fetched from ``<api_url>/file/bot<token>/<file_path>``.

    **No size limit of our own by default** (2026-08-15). There used to be a 1 MB cap, which
    refused the ordinary case this exists for — a 5 MB leave-list workbook — with a number that
    corresponded to nothing. What remains is Telegram's own 20 MB ceiling on bot downloads, which
    no setting here can lift; :data:`TELEGRAM_BOT_DOWNLOAD_LIMIT` is only used to explain it. A
    caller that wants a smaller bound for its own reasons passes ``max_bytes``.

    ``timeout_seconds`` defaults high because it now covers a multi-megabyte body over whatever
    link the worker has, not just a JSON reply.
    """
    if not file_id:
        raise RuntimeError("Telegram file_id is empty.")
    try:
        meta = call_telegram_api(
            bot_token=bot_token, method_name="getFile",
            payload={"file_id": file_id}, api_url=api_url, timeout_seconds=timeout_seconds,
        )
    except RuntimeError as exc:
        # Telegram's own refusal reads "Bad Request: file is too big", which tells the operator
        # nothing about what to do next. Say whose limit it is and what the way around it is.
        if "too big" in str(exc).lower():
            raise RuntimeError(
                f"Telegram refuses to serve this file to a bot: the Bot API caps a bot's own "
                f"download at {TELEGRAM_BOT_DOWNLOAD_LIMIT // (1024 * 1024)} MB, however large "
                f"the file you attached is. Send a smaller extract, or put the file where the "
                f"worker can read it and pass \"file_path\" to the command directly."
            ) from exc
        raise
    result = meta.get("result") or {}
    file_path = str(result.get("file_path") or "")
    if not file_path:
        raise RuntimeError("Telegram getFile returned no file_path.")
    reported_size = int(result.get("file_size") or 0)
    if max_bytes is not None and reported_size and reported_size > max_bytes:
        raise RuntimeError(f"Telegram file too large: {reported_size} > {max_bytes} bytes.")
    url = f"{api_url.rstrip('/')}/file/bot{bot_token}/{file_path}"
    req = request.Request(url, method="GET")
    try:
        with request.urlopen(req, timeout=timeout_seconds) as response:
            data = response.read() if max_bytes is None else response.read(max_bytes + 1)
    except error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Telegram file HTTP {exc.code}: {body}") from exc
    except error.URLError as exc:
        raise RuntimeError(f"Telegram file download failed: {exc.reason}") from exc
    if max_bytes is not None and len(data) > max_bytes:
        raise RuntimeError(f"Telegram file exceeds {max_bytes} bytes.")
    return data


def bot_info(
    *,
    bot_token: str,
    api_url: str = DEFAULT_TELEGRAM_API_URL,
    timeout_seconds: int = 20,
) -> dict[str, Any]:
    """Who this token belongs to, and whether the bot can actually read a group.

    Two facts a new install needs and had no way to get. `data/bot_telegram.json` asks for
    `telegram_bot_id` and `telegram_bot_username`, and until this existed the only way to learn
    them was to call `getMe` by hand with the raw token — measured on 2026-09-10, standing up a
    node from the scaffold.

    The second fact is the one that bites later. Telegram bots default to **privacy mode on**,
    where a bot in a group sees only slash-commands aimed at it and replies to its own messages.
    That is enough for `/spbot_*` and not enough for anything that reads ordinary chat, so a group
    can look correctly configured and quietly deliver half of what it should. `getMe` reports it as
    ``can_read_all_group_messages`` and nothing in this toolkit was surfacing it.
    """
    answer = call_telegram_api(
        bot_token=bot_token, method_name="getMe", payload={},
        api_url=api_url, timeout_seconds=timeout_seconds)
    result = answer.get("result") or {}
    reads_all = bool(result.get("can_read_all_group_messages"))
    return {
        "ok": bool(answer.get("ok")),
        "telegram_bot_id": str(result.get("id") or ""),
        "telegram_bot_username": str(result.get("username") or ""),
        "can_join_groups": bool(result.get("can_join_groups")),
        "can_read_all_group_messages": reads_all,
        "privacy_mode": "off" if reads_all else "on",
        "note": (
            "Privacy mode is ON: in a group this bot sees only commands addressed to it and "
            "replies to its own messages. Slash commands work; anything reading ordinary chat "
            "does not. Turn it off with @BotFather (/setprivacy), or make the bot an admin."
            if not reads_all else
            "Privacy mode is OFF: this bot sees every message in the groups it belongs to."),
        "bot_telegram_json": {
            "telegram_bot_id": str(result.get("id") or ""),
            "telegram_bot_username": str(result.get("username") or ""),
        },
    }


def get_updates(
    *,
    bot_token: str,
    api_url: str = DEFAULT_TELEGRAM_API_URL,
    timeout_seconds: int = 20,
    offset: int | None = None,
    limit: int | None = None,
    allowed_updates: list[str] | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {}
    if offset is not None:
        payload["offset"] = offset
    if limit is not None:
        payload["limit"] = limit
    if allowed_updates is not None:
        payload["allowed_updates"] = json.dumps(allowed_updates, ensure_ascii=False)

    return call_telegram_api(
        bot_token=bot_token,
        method_name="getUpdates",
        payload=payload,
        api_url=api_url,
        timeout_seconds=timeout_seconds,
    )
