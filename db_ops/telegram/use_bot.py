r"""Point this node at a Telegram bot — the mirror of ``db-ops db use-store``.

``data/bot_telegram.json`` is catalogued configuration, so it **travels inside a config bundle**:
`import-data` faithfully points a machine that has never run at the bot the bundle came from — in
practice the estate's own. That is right for the file and wrong for a node being proved, and every
procedure that stands one up therefore ended with "now edit that file". `store_config.json` had the
same shape and got a command in v0.14.0; this is the same command for the bot, written on
2026-09-14 after a hand-built node came up on the production bot and nothing said so.

**What it prevents, precisely.** Two pollers on one token is not a soft failure: the 0.16.0 cycle
measured 4,735 Telegram calls refused with HTTP 409 ``terminated by other getUpdates request``, and
a 409 retry can deliver the same message twice.

Three things it does that a hand-edit does not:

* **The id and username are read back from Telegram**, never typed. They are what `getMe` answers
  for the token behind the ref, so the file cannot claim a bot the token does not belong to.
* **It refuses a ref the secret store does not hold**, naming the refs that are there. A ref with
  no secret behind it fails later, inside whichever app command reaches the queue first.
* **It prints the bot that is active afterwards** — for the same reason `use-store` prints the
  resolved connection string. The mistake being prevented is *believing* the node is on the other
  one, and an acknowledgement does not disturb that belief.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from db_ops.lib.json_io import atomic_write_text

#: The file the bot identity lives in, and the key `telegram_config.json` points at it with.
BOT_CONFIG_FILENAME = "bot_telegram.json"


class UseBotError(RuntimeError):
    """The node could not be pointed at that bot. Nothing was written."""


USAGE = """usage: python -m db_ops.telegram.cli use-bot --ref <SECRET_REF> [--dry-run]

Point this node at the Telegram bot whose token is stored under <SECRET_REF>, the way
`db-ops db use-store` points it at a store.

  --ref       required. The name in data/encrypted_secret_text.json holding the bot token,
              e.g. TOKEN_TELEGRAM_TEST_BOT
  --dry-run   call getMe and print what would be written; write nothing

The id and username are read back from Telegram rather than typed, so the file cannot name a bot
the token does not belong to. A node being tested must run its OWN bot: two pollers on one token
cost 4,735 calls refused with HTTP 409 in the 0.16.0 cycle.
"""


def _pin_is_in_the_way(telegram_settings: dict[str, Any]) -> str:
    """`telegram_config.json` may override the bot file, and `init` used to pre-fill that key.

    A value there **wins** over `bot_telegram.json`, so writing the bot file while the settings
    file names a ref changes the node's name and not its token — which is how a node came to
    report the test bot's name while authenticating as something else. Refuse rather than write
    something that will not take effect.
    """
    return str(telegram_settings.get("telegram_bot_token_ref") or "").strip()


def use_bot(ref: str, *, data_dir: str | Path, api_url: str = "https://api.telegram.org",
            timeout_seconds: int = 20, dry_run: bool = False,
            telegram_settings: dict[str, Any] | None = None,
            settings_path: str | Path | None = None) -> dict[str, Any]:
    """Write ``bot_telegram.json`` for ``ref``, with the identity taken from ``getMe``."""
    from db_ops.lib import secret_text
    from db_ops.telegram.api import bot_info

    ref = str(ref or "").strip()
    if not ref:
        raise UseBotError("--ref is required: the secret holding the bot token.")

    root = Path(data_dir)
    try:
        secrets = secret_text.load_secret_text(root)
    except Exception as exc:  # noqa: BLE001 - a wrong passphrase must say so, not write a file
        raise UseBotError(f"the secret store under {root} could not be opened: {exc}") from exc

    if ref not in secrets:
        known = sorted(name for name in secrets
                       if "TOKEN" in name.upper() or "TELEGRAM" in name.upper())
        raise UseBotError(
            f"{ref} is not in the secret store under {root}. Token-looking refs there: "
            f"{known or 'none'}. Add it with `common.cli secret-set -` first; a ref with no "
            "secret behind it fails later, inside whichever app command reaches the queue first.")

    pinned = _pin_is_in_the_way(telegram_settings or {})
    if pinned and pinned != ref:
        raise UseBotError(
            f"telegram_config.json pins telegram_bot_token_ref to {pinned!r}, and a value there "
            f"WINS over {BOT_CONFIG_FILENAME} - so writing {ref!r} here would change the name this "
            "node reports and not the token it authenticates with. Remove that key from "
            f"{settings_path or 'telegram_config.json'} (the master does not set it), or set it "
            "to the same ref.")

    identity = bot_info(bot_token=secrets[ref], api_url=api_url, timeout_seconds=timeout_seconds)
    if not identity.get("ok") or not identity.get("telegram_bot_id"):
        raise UseBotError(
            f"Telegram did not accept the token behind {ref}: getMe returned {identity!r}. "
            "Nothing was written.")

    document = {
        "telegram_bot_token_ref": ref,
        "telegram_bot_id": identity["telegram_bot_id"],
        "telegram_bot_username": identity["telegram_bot_username"],
    }
    path = root / BOT_CONFIG_FILENAME
    before = ""
    if path.exists():
        try:
            before = str(json.loads(path.read_bytes().decode("utf-8-sig"))
                         .get("telegram_bot_username") or "")
        except ValueError:
            before = "(unreadable)"

    if not dry_run:
        atomic_write_text(path, json.dumps(document, ensure_ascii=False, indent=4) + "\n")

    return {
        "ref": ref,
        "was": before,
        "now": document["telegram_bot_username"],
        "telegram_bot_id": document["telegram_bot_id"],
        "privacy_mode": identity.get("privacy_mode"),
        "privacy_note": identity.get("note"),
        "file": str(path),
        "written": not dry_run,
    }
