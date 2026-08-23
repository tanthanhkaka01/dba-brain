"""The gate a deploy passes before it ships ``data/`` — does the store still agree with it?

A deploy copies the master's ``data/`` over the worker's. That was safe while files were the only
way to change config. It stopped being safe the moment the web console could edit a record,
because the console writes to the **store**, which master and worker share, and to the *worker's*
files. The master's copy is not touched, so the next deploy from that master ships the old values
straight back over the operator's change — silently, and with a success message.

It is not hypothetical: on 2026-08-21 an operator set
``APP-REPORTS-INVENTORY-WORKFLOW.repeat_interval`` to 3600 in the console, and the master's
``app_commands.json`` still said 7200. A deploy at that moment would have reverted it.

So the deploy stops and asks, and the two answers are the two things an operator can actually
mean:

* **adopt** — the console's edits are the truth. The master's files are rebuilt from the store
  (:func:`db_ops.db.config_sync.export`) and the deploy ships those.
* **keep** — the master's files are the truth. The store is re-synced from them, so the console's
  value is *replaced* rather than left to reappear as drift on the next deploy. The discarded
  value stays in ``config_item_revisions``; nothing is lost, it is superseded on the record.

There is no "ignore and carry on". Leaving the two disagreeing is what produces the next silent
revert, and a warning an operator can walk past is a warning that stops being read.

**Formatting-only drift never stops a deploy.** A file that was hand-formatted and has since been
normalised differs byte for byte and not in meaning; halting for that would train people to answer
the prompt without reading it, which is the failure mode the prompt exists to avoid.

Unattended runs declare their answer with ``--on-config-drift``; with no terminal and no
declaration the deploy **aborts**, because guessing which side is right is the one thing this gate
must not do.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

#: What may be decided about drift. ``ask`` is the interactive default.
DRIFT_CHOICES = ("ask", "adopt", "keep", "abort")

_RULE = "-" * 78


class ConfigDriftAbort(RuntimeError):
    """The deploy was stopped because the store and ``data/`` disagree."""


def describe(drifted: list[dict[str, Any]]) -> str:
    """The report an operator reads before deciding. Names the files and what changed in them."""
    # ASCII only in the banner. This prints to a Windows console whose code page is cp1252,
    # where a character outside it does not merely look wrong: it can raise UnicodeEncodeError
    # and turn the gate itself into the crash it exists to prevent.
    lines = [_RULE,
             "  CONFIG DRIFT - the runtime store and this master's data/ disagree",
             _RULE,
             "  The store is shared with the worker and is what the web console writes to.",
             "  Deploying now would ship these files over the worker's copies.",
             ""]
    for item in drifted:
        lines.append(f"    {item['file']}")
        lines.append(f"        {item.get('detail', 'content differs')}")
    lines.extend([
        "",
        "  adopt : rebuild this master's data/ from the store, then deploy that",
        "          (keeps what was changed in the console)",
        "  keep  : deploy this master's data/ as it is, and re-sync the store from it",
        "          (discards the console's values; the old ones stay in the revision history)",
        "  abort : change nothing and stop",
        _RULE,
    ])
    return "\n".join(lines)


def check(store: Any, *, data_dir: str | Path | None = None) -> list[dict[str, Any]]:
    """The drift that matters — content, not formatting. Empty list means the gate is open."""
    from db_ops.db import config_sync

    return config_sync.content_drift(store, data_dir=data_dir)


def resolve(
    store: Any,
    *,
    data_dir: str | Path | None = None,
    decision: str = "ask",
    actor: str = "deploy",
    interactive: bool | None = None,
    ask: Any = None,
    out: Any = None,
) -> dict[str, Any]:
    """Run the gate. Returns what was found and what was done; raises to stop the deploy.

    ``ask`` and ``interactive`` are injectable so the whole gate is testable without a terminal —
    the same reason the console is a request -> response function.
    """
    from db_ops.common import confirm
    from db_ops.db import config_sync

    stream = out or sys.stderr
    if decision not in DRIFT_CHOICES:
        raise ConfigDriftAbort(
            f"--on-config-drift must be one of {', '.join(DRIFT_CHOICES)}; got '{decision}'.")

    drifted = check(store, data_dir=data_dir)
    if not drifted:
        return {"drifted": [], "decision": "none", "applied": False}

    print(describe(drifted), file=stream, flush=True)
    chosen = decision
    if chosen == "ask":
        can_ask = confirm.is_interactive() if interactive is None else bool(interactive)
        if not can_ask:
            # No terminal and no declared answer. Refusing is the only safe reading: both
            # alternatives destroy somebody's change, and neither is a default.
            raise ConfigDriftAbort(
                "Config drift found and there is no terminal to ask on. Re-run with "
                "--on-config-drift adopt (take the store's values) or "
                "--on-config-drift keep (ship this master's files and re-sync the store).")
        reader = ask or (lambda prompt: confirm.read_answer(prompt, stream=stream))
        chosen = _read_choice(reader)

    if chosen == "abort":
        raise ConfigDriftAbort("Deploy stopped: config drift was not resolved.")

    if chosen == "adopt":
        result = config_sync.export(store, data_dir=data_dir)
        print(f"  adopted the store's values: {result['totals']['written']} file(s) rewritten.",
              file=stream, flush=True)
    else:
        result = config_sync.sync(store, data_dir=data_dir, actor=actor)
        print(f"  kept this master's files: {result['totals']['updated']} record(s) replaced in "
              "the store; the previous values stay in config_item_revisions.",
              file=stream, flush=True)

    remaining = check(store, data_dir=data_dir)
    if remaining:
        # The resolution has to actually resolve. Shipping after a half-applied fix would put the
        # deploy back in exactly the state the gate exists to catch.
        raise ConfigDriftAbort(
            "Config drift is still present after resolving it: "
            + ", ".join(item["file"] for item in remaining))
    return {"drifted": drifted, "decision": chosen, "applied": True, "result": result}


def _read_choice(reader: Any) -> str:
    """Ask until the answer is one of the three. An empty answer is abort, never a default action.

    Three tries, then abort: a prompt that loops forever is a deploy that hangs a CI job, and the
    safe end of that is stopping.
    """
    for _ in range(3):
        answer = str(reader("  adopt / keep / abort ? ") or "").strip().lower()
        if answer in {"adopt", "keep", "abort"}:
            return answer
        if answer == "":
            return "abort"
    return "abort"
