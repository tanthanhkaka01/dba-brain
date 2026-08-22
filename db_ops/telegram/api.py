from __future__ import annotations
from db_ops.lib.telegram_text import TELEGRAM_MESSAGE_LIMIT  # noqa: F401 - one definition, see that module

import json
import mimetypes
import uuid
from pathlib import Path
from typing import Any
from urllib import error, parse, request

from db_ops.lib.telegram_text import split_telegram_message
from db_ops.telegram.severity import decorate_message


DEFAULT_TELEGRAM_API_URL = "https://api.telegram.org"



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

        result = call_telegram_api(
            bot_token=bot_token,
            method_name="sendMessage",
            payload=payload,
            api_url=api_url,
            timeout_seconds=timeout_seconds,
        )
        # The first part's id is the one recorded against the queue row: it is where the output
        # starts, so a reply that quotes it quotes the beginning and not the tail.
        if first_result is None:
            first_result = result
    return first_result or {}


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
