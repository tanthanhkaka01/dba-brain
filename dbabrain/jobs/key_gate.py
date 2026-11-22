"""The scheduler's half of the dbabrain key gate (1.0.0, plan K4).

The rules are :mod:`dbabrain.lib.dbabrain_key`'s (``may_run``); this is where the daemon asks them,
once per scan, before it starts a due command:

* a command whose module needs a feature (:data:`lib.dbabrain_key.COMMAND_FEATURES` - reports,
  backup / restore, SRE, the MCP connector) is **not started** while the key does not allow it -
  logged as a skip like a closed window, and **said on Telegram once** per process for each
  reason, never every tick (the house rule: a state, not a failure). The gate says only what it
  refuses: the term ending and the grace are the watch's (``key_watch``), said once for the node -
  on 2.87.111 the gate said the watch's warning again for every gated command, four at once;
* metrics, alerts, SLA, SQL tasks, the console and everything else are not asked about at all;
* a run a person **requested** of backup / restore is work already asked for - it runs after the
  grace too (``new_work=False``), said CRITICAL; a requested report is still new work;
* a warning (the term ending), the grace and a run let through after it are the watch's to say.
"""

from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from dbabrain.lib import dbabrain_key as dk
from dbabrain.lib.secret_text import SECRET_KEY_ENV_VAR

#: Features whose requested run is work already asked for (a restore must never be stranded).
_ASKED_FOR_RUNS = frozenset({"backup_restore"})

#: What this process has said already: (state, feature, level). A restart says it again, once.
_SAID: set[tuple[str, str, str]] = set()


class KeyGate:
    """One standing for one scan of the scheduler."""

    def __init__(self, standing: dk.KeyStanding) -> None:
        self.standing = standing

    @classmethod
    def for_scan(cls, data_dir: str | Path, *, now: datetime | None = None,
                 public_keys: Any = None) -> "KeyGate":
        return cls(dk.node_standing(data_dir, key=os.environ.get(SECRET_KEY_ENV_VAR) or None,
                                    now=now or datetime.now(timezone.utc), public_keys=public_keys))

    def decide(self, command_text: str, *, requested: bool) -> tuple[str, dk.Decision] | None:
        """``(feature, decision)`` for a gated command; None for one that needs no key."""
        feature = dk.feature_of_command(command_text)
        if feature is None:
            return None
        new_work = not (requested and feature in _ASKED_FOR_RUNS)
        return feature, dk.may_run(self.standing, feature, new_work=new_work)

    def say_once(self, feature: str, decision: dk.Decision, push: Callable[[str, str], None]) -> bool:
        """Push a refusal's sentence once per process for its (state, feature, level)."""
        if decision.allowed or not decision.sentence:
            return False
        mark = (self.standing.state, feature, decision.level)
        if mark in _SAID:
            return False
        _SAID.add(mark)
        push(decision.level, decision.sentence)
        return True


def forget_what_was_said() -> None:
    """For the tests: a new process, as far as :meth:`KeyGate.say_once` can tell."""
    _SAID.clear()
