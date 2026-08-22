"""The gate model every runbook-style operation reports through, plus its JSON evidence file.

An operation that touches a production host — restart it, patch it, fail it over — is judged
afterwards by what it recorded, not by what the operator remembers. So each such operation is
written as a list of **gates**: one named check, a verdict (``OK`` / ``WARN`` / ``FAIL``), a
sentence an operator can act on, and whether a failure is *blocking* (stop) or informational
(proceed and note it). The gates are printed as they run and written to a JSON file at the end.

Two rules come out of the 2026-08-03 CU26 run (``docs/reports/20260803_ord129-*.md``) and are
why this is shared code rather than a per-script convention:

* **A gate must test the thing it claims to test.** ``post.registry_build`` compared the SQL
  Server registry ``Version`` (the build originally *installed*, which no CU ever changes)
  against the target build, so it reported FAIL on every successful CU on every instance.
* **An overridable gate is not the same as a non-blocking one.** A stale backup may be accepted
  deliberately (``override``), a warning is accepted always (``blocking=False``). Collapsing the
  two either blocks work that was approved or waves through work that was not.

A clean run exits 0. That matters more than it sounds: the two false-failing gates above made a
fully successful patch exit non-zero, which teaches every reader — and every caller that reads
exit codes — to ignore the result.

Usage::

    report = GateReport("restart", target="ACME-192-0-2-250", echo=print)
    report.add("host.reachable", OK, "192.0.2.250:22 answered in 0.03s")
    report.add("sql.recent_full_backup", FAIL, "stale: APPDB_Prod", override="allow-stale-backup")
    if report.blockers({"allow-stale-backup"}):
        ...
    path = report.write()          # runtime/evidence/<operation>/<run_id>.json
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Iterable

__all__ = [
    "DEFAULT_EVIDENCE_ROOT",
    "FAIL",
    "Gate",
    "GateReport",
    "OK",
    "SKIP",
    "WARN",
    "new_run_id",
    "symbol",
]

OK = "OK"
WARN = "WARN"
FAIL = "FAIL"
SKIP = "SKIP"

# Fixed-width so a run reads as a column, not as ragged prose.
_SYMBOLS = {OK: "[ OK ]", WARN: "[WARN]", FAIL: "[FAIL]", SKIP: "[SKIP]"}

# Generated, never committed — same rule as logs/ and the rest of runtime/.
DEFAULT_EVIDENCE_ROOT = Path("runtime") / "evidence"


def symbol(status: str) -> str:
    return _SYMBOLS.get(str(status).upper(), "[ ?  ]")


def new_run_id(now: datetime | None = None) -> str:
    """``YYYYmmdd_HHMMSS`` in **local** time — the clock the maintenance window is stated in.

    Evidence is read next to a runbook that says "19:00 - 21:00 (+07)"; a UTC run id would
    make the reader do the arithmetic every time. Store rows keep using UTC (see
    ``docs/13_common.md``, *Timezone convention*) — this is a file name, not a stored fact.
    """
    return (now or datetime.now()).strftime("%Y%m%d_%H%M%S")


@dataclass
class Gate:
    """One named check and its verdict.

    ``blocking`` is what the gate means when it fails: True = stop, False = note it and go on.
    ``override`` names the flag that lets an operator accept a blocking failure deliberately —
    an approval, which is not the same as the failure being harmless.
    """

    name: str
    status: str
    detail: str
    blocking: bool = True
    data: Any = None
    override: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "status": self.status,
            "detail": self.detail,
            "blocking": self.blocking,
            "override": self.override,
            "data": self.data,
        }

    def line(self) -> str:
        return f"{symbol(self.status)} {self.name}: {self.detail}"


class GateReport:
    """The gates of one operation, plus the facts they were decided from.

    ``echo`` is called with each gate line as it is added. Long operations (a restart, a patch)
    are watched live during a maintenance window, so a report that only speaks at the end is
    indistinguishable from a hung one. The CLI passes a printer that writes to **stderr**, so
    the JSON result on stdout stays machine-readable.
    """

    def __init__(
        self,
        operation: str,
        *,
        target: str = "",
        run_id: str | None = None,
        started_at: str | None = None,
        echo: Callable[[str], None] | None = None,
    ) -> None:
        self.operation = str(operation)
        self.target = str(target or "")
        self.run_id = run_id or new_run_id()
        self.started_at = started_at or datetime.now().astimezone().isoformat(timespec="seconds")
        self.gates: list[Gate] = []
        self.facts: dict[str, Any] = {}
        self._echo = echo

    # ------------------------------------------------------------------ #
    def add(
        self,
        name: str,
        status: str,
        detail: str,
        *,
        blocking: bool = True,
        data: Any = None,
        override: str = "",
    ) -> Gate:
        gate = Gate(
            name=str(name),
            status=str(status).upper(),
            detail=str(detail),
            blocking=bool(blocking),
            data=data,
            override=str(override or ""),
        )
        self.gates.append(gate)
        if self._echo is not None:
            self._echo(gate.line())
        return gate

    def note(self, key: str, value: Any) -> None:
        """Record a fact the gates were decided from, without asserting anything about it."""
        self.facts[str(key)] = value

    def say(self, message: str) -> None:
        """Progress text that is not a gate ("waiting for the host to come back...")."""
        if self._echo is not None:
            self._echo(str(message))

    # ------------------------------------------------------------------ #
    def blockers(self, overrides: Iterable[str] = ()) -> list[Gate]:
        """Failed gates that must stop the operation, after applying ``overrides``."""
        accepted = {str(flag).strip() for flag in overrides if str(flag).strip()}
        return [
            gate
            for gate in self.gates
            if gate.status == FAIL and gate.blocking and not (gate.override and gate.override in accepted)
        ]

    def passed(self, overrides: Iterable[str] = ()) -> bool:
        return not self.blockers(overrides)

    def status(self, overrides: Iterable[str] = ()) -> str:
        """The run's single verdict: FAIL if anything blocking failed, WARN if anything warned."""
        if self.blockers(overrides):
            return FAIL
        if any(gate.status in (WARN, FAIL) for gate in self.gates):
            return WARN
        return OK

    def counts(self) -> dict[str, int]:
        result = {OK: 0, WARN: 0, FAIL: 0, SKIP: 0}
        for gate in self.gates:
            result[gate.status] = result.get(gate.status, 0) + 1
        return result

    # ------------------------------------------------------------------ #
    def to_dict(self, overrides: Iterable[str] = ()) -> dict[str, Any]:
        """The JSON shape of the whole run — what gets written as evidence and printed by the CLI."""
        overrides = list(overrides)
        blockers = self.blockers(overrides)
        return {
            "ok": not blockers,
            "operation": self.operation,
            "target": self.target,
            "run_id": self.run_id,
            "started_at": self.started_at,
            "status": self.status(overrides),
            "counts": self.counts(),
            "overrides": [str(flag) for flag in overrides],
            "blockers": [gate.name for gate in blockers],
            "gates": [gate.to_dict() for gate in self.gates],
            "facts": self.facts,
        }

    def render(self) -> str:
        return "\n".join(gate.line() for gate in self.gates)

    def write(
        self,
        root: str | Path | None = None,
        *,
        overrides: Iterable[str] = (),
    ) -> Path:
        """Write the evidence file and return its path.

        One file per run under ``runtime/evidence/<operation>/<run_id>.json``. Evidence is
        append-only by construction — a new run never overwrites an older one — because the
        question it answers ("what did the host look like when we touched it") has a different
        answer every time and the old answer is the one an incident review needs.
        """
        directory = Path(root) if root is not None else DEFAULT_EVIDENCE_ROOT
        directory = directory / self.operation
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"{self.run_id}.json"
        with path.open("w", encoding="utf-8") as handle:
            json.dump(self.to_dict(overrides), handle, ensure_ascii=False, indent=2, default=str)
            handle.write("\n")
        return path


@dataclass
class OperationResult:
    """A non-gate outcome carried alongside a report (a command's exit code, a build number).

    Kept separate from :class:`Gate` because "the installer exited 3010" is a *fact*; whether
    3010 is acceptable is a gate's judgement, and the two must not be the same field.
    """

    name: str
    value: Any = None
    detail: str = ""
    data: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {"name": self.name, "value": self.value, "detail": self.detail, "data": self.data}
