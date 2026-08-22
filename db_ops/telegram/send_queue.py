from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from db_ops.lib.rows import row_value
from db_ops.db import DbOpsStore
from db_ops.telegram.api import send_document, send_message


def send_pending_messages(
    *,
    sqlite_path: str | Path,
    bot_token: str,
    api_url: str = "https://api.telegram.org",
    timeout_seconds: int = 20,
    limit: int = 50,
    retry_count: int = 3,
) -> dict[str, int]:
    store = DbOpsStore(sqlite_path)
    rows = store.fetch_pending_telegram_send_messages(limit=limit)
    counts = {
        "read": len(rows),
        "sent": 0,
        "failed": 0,
    }

    for row in rows:
        result = send_one_message(
            sqlite_path=sqlite_path,
            send_tlgmsg_id=int(row["send_tlgmsg_id"]),
            bot_token=bot_token,
            api_url=api_url,
            timeout_seconds=timeout_seconds,
            retry_count=retry_count,
        )
        if result["sent"] == 1:
            counts["sent"] += 1
        elif result["failed"] == 1:
            counts["failed"] += 1

    return counts


def send_one_message(
    *,
    sqlite_path: str | Path,
    send_tlgmsg_id: int,
    bot_token: str,
    api_url: str = "https://api.telegram.org",
    timeout_seconds: int = 20,
    retry_count: int = 3,
) -> dict[str, int | str]:
    store = DbOpsStore(sqlite_path)
    row = store.fetch_telegram_send_message(send_tlgmsg_id=send_tlgmsg_id)
    if row is None:
        return {"send_tlgmsg_id": send_tlgmsg_id, "sent": 0, "failed": 1, "status": "not_found"}

    if int(row["send_status"]) != 0:
        return {"send_tlgmsg_id": send_tlgmsg_id, "sent": 0, "failed": 0, "status": "not_pending"}

    store.mark_telegram_send_message_processing(send_tlgmsg_id=send_tlgmsg_id)
    # Rows written before the column existed, and any producer that has not adopted it, simply
    # have nothing here — the send layer then falls back to reading the message header.
    message_type = row_value(row, "message_type")
    last_error = ""
    for _ in range(max(1, retry_count)):
        try:
            metadata = metadata_from_json(str(row["metadata_json"] or "{}"))
            document_path = str(metadata.get("document_path") or "").strip()
            if document_path:
                result = send_document(
                    bot_token=bot_token,
                    chat_id=str(row["tlgchat_id"] or ""),
                    document_path=document_path,
                    caption=str(row["message_text"] or ""),
                    api_url=api_url,
                    timeout_seconds=max(timeout_seconds, int(metadata.get("timeout_seconds") or 60)),
                    reply_to_message_id=int(row["reply_message_id"]) if row["reply_message_id"] is not None else None,
                )
            else:
                result = send_message(
                    bot_token=bot_token,
                    chat_id=str(row["tlgchat_id"] or ""),
                    text=str(row["message_text"] or ""),
                    api_url=api_url,
                    timeout_seconds=timeout_seconds,
                    reply_to_message_id=int(row["reply_message_id"]) if row["reply_message_id"] is not None else None,
                    reply_markup=reply_markup_from_metadata(metadata),
                    message_type=message_type,
                )
            message_id = extract_sent_message_id(result)
            store.mark_telegram_send_message_sent(
                send_tlgmsg_id=send_tlgmsg_id,
                message_id=message_id,
            )
            return {"send_tlgmsg_id": send_tlgmsg_id, "sent": 1, "failed": 0, "status": "sent"}
        except Exception as exc:  # noqa: BLE001 - retry path.
            last_error = str(exc)

    store.mark_telegram_send_message_failed(
        send_tlgmsg_id=send_tlgmsg_id,
        fail_text=last_error,
    )
    return {"send_tlgmsg_id": send_tlgmsg_id, "sent": 0, "failed": 1, "status": "failed"}


# `row_value` is `db_ops.lib.rows.row_value` since 2026-08-16. It was the third near-copy of
# 'read a column the row may not have' and the only one that did not catch TypeError, so a
# tuple row raised here and nowhere else.


def extract_sent_message_id(result: dict[str, Any]) -> int | None:
    message = result.get("result") or {}
    message_id = message.get("message_id")
    return int(message_id) if message_id is not None else None


def metadata_from_json(metadata_json: str) -> dict[str, Any]:
    try:
        metadata = json.loads(metadata_json or "{}")
    except json.JSONDecodeError:
        return {}
    return metadata if isinstance(metadata, dict) else {}


def reply_markup_from_metadata(metadata: dict[str, Any]) -> dict[str, Any] | None:
    if metadata.get("force_reply") is True:
        return {"force_reply": True, "selective": True}
    reply_markup = metadata.get("reply_markup")
    return reply_markup if isinstance(reply_markup, dict) else None
