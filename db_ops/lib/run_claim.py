"""Who owns a run that is still ``running``, and when that ownership may be taken away.

**The two failures this is between.** A scheduler that waits for every run to finish before looking
at the next one turns one slow task into a stopped estate: on 2026-09-19 a 22-minute SQL task held
every other SQL task for 22 minutes, and earlier the same day a leftover ``running`` row held them
for 30. A scheduler that simply runs things in parallel instead produces the opposite failure, which
is worse: the same task started twice, writing to production twice — eight duplicate production SQL
runs on 2026-09-08, and a second restore that began 47 minutes into the first on 2026-09-14.

So concurrency is not the change. **The claim is.** A ``running`` row is an exclusive claim on its
key, enforced by a unique index, and this module is the rule for the only hard question that
remains: *this row says running — is anybody actually running it?*

| The row | Answer |
| --- | --- |
| Owned by **this host**, pid alive | **Held.** Leave it, whatever its age: a task may legitimately outlive its timeout, and taking the claim away starts a second copy on top of the first — which is the loop |
| Owned by **this host**, pid gone | **Free** once the process is gone. No waiting: a dead process will not come back |
| Owned by **this node under a host name it no longer has** | **Free at once.** The node's identity (``node_identity``) outlives its host name; a container recreated under a new one has no process left from the old one |
| Owned by **another host** | **Free only after a grace**, because this host cannot ask that one whether its pid is alive. The grace is long on purpose |
| **No pid recorded** (written by an older build) | Falls back to age alone, the behaviour before this module existed |

**One reaper asks for more: a SQL task run past its timeout is over** (``at_timeout``, the operator,
2026-10-02, 0.26.0 §1.70). The first row of the table is right for a restore and for an app command,
whose timeout the daemon enforces by stopping the process. A SQL task had no such bound: on
2026-09-30 one target stayed ``running`` for 13 hours behind a live pid, and the row is the claim, so
that target did not run again until the container was stopped. For that reaper a row past its
timeout is closed as an error by timeout whoever owns it, and the caller stops the owner first - the
claim records when its process started (``claim_started``), so the process stopped is the one that
made the claim and the second copy never starts on top of the first.

Pure: no store, no process calls. The liveness reading is :mod:`db_ops.lib.process_liveness` and the
caller passes the answer in, so this rule can be tested without a process to kill.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

#: How long another host's ``running`` row is left alone after its timeout has passed. This host
#: cannot see that host's processes, so the only safe reading is "it has been far too long". An
#: hour is longer than any scheduled run in this estate and shorter than a shift.
FOREIGN_HOST_GRACE_SECONDS: int = 3600

#: Where the claim is written inside a run row's ``metadata_json``.
PID_FIELD = "claim_pid"
HOST_FIELD = "claim_host"
#: The tool root's identity (``node_identity``), when the claiming process was handed one.
NODE_FIELD = "claim_node"
#: When the claiming process started, as ``process_liveness.process_start_marker`` reads it - what
#: tells that process from a later one holding the same pid, before anything stops it.
STARTED_FIELD = "claim_started"


@dataclass(frozen=True)
class ReapVerdict:
    """Whether a ``running`` row may be closed, and the sentence that says why."""

    reap: bool
    reason: str
    #: Closed because it is past its timeout while its owner may still be working - the one verdict
    #: after which the caller has a process to stop.
    timed_out: bool = False


def claim_fields(*, pid: int, host: str, node: str = "", started: str = "") -> dict[str, object]:
    """The metadata a run writes when it claims its key.

    Both halves are needed and neither is enough: a pid without a host is a number that means
    something different on every machine, and a host without a pid cannot be checked at all.
    ``node`` and ``started`` are written only when there is one, so a row from a process nobody
    handed them reads exactly as it did before the fields existed.
    """
    fields: dict[str, object] = {PID_FIELD: int(pid), HOST_FIELD: str(host)}
    if str(node or "").strip():
        fields[NODE_FIELD] = str(node).strip()
    if str(started or "").strip():
        fields[STARTED_FIELD] = str(started).strip()
    return fields


def claim_started(metadata: dict | None) -> str:
    """The claiming process's start marker recorded on a claim, or ``""``."""
    return str((metadata or {}).get(STARTED_FIELD) or "").strip()


def claim_node(metadata: dict | None) -> str:
    """The node identity recorded on a claim, or ``""``."""
    return str((metadata or {}).get(NODE_FIELD) or "").strip()


def _left_by_an_earlier_host_of_this_node(metadata: dict | None, *, host: str, this_host: str,
                                          this_node: str) -> bool:
    """Was this claim made by *this* node, running under a host name it no longer has?

    Both identities must be present and equal. A missing one on either side is not evidence, and
    the row falls through to the other-host rule - never to a sooner reap.
    """
    node = claim_node(metadata)
    return bool(node) and bool(this_node) and node == this_node and bool(host) and host != this_host


def row_metadata(row: object) -> dict:
    """A run row's ``metadata_json`` as a dict, whatever the backend handed back.

    Here rather than beside each caller because both readers of a claim need it and they are in
    different apps: the daemon's startup recovery and the SQL-task reaper had the same nine lines
    under two names until `test_no_duplicate_definitions.py` said so.
    """
    try:
        raw = row["metadata_json"]  # type: ignore[index]
    except (KeyError, IndexError, TypeError):
        return {}
    if isinstance(raw, dict):
        return raw
    try:
        loaded = json.loads(raw or "{}")
    except (TypeError, ValueError):
        return {}
    return loaded if isinstance(loaded, dict) else {}


def row_host(row: object) -> str:
    """A run row's own ``host_name`` column, or ``""`` - what :func:`claim_owner` takes as
    ``host_fallback``.

    Every reader of a claim passes it, not only the daemon's start-up: a row an older build wrote
    keeps its ``pid`` in the metadata and its host only in this column, and a reader that left it
    out judged that row differently from the one that read it (0.27.0 item 1.96).
    """
    try:
        return str(row["host_name"] or "").strip()  # type: ignore[index]
    except (KeyError, IndexError, TypeError):
        return ""


def claim_owner(metadata: dict | None, *, host_fallback: str = "") -> tuple[int | None, str]:
    """The ``(pid, host)`` recorded on a row, as far as it recorded anything.

    **Rows written before this module also name a process**, in a different place: the daemon has
    always put the child's ``pid`` in the run's metadata, and ``job_runs`` has always had a
    ``host_name`` column. Reading that as a fallback means the liveness question can be answered
    exactly for every row this product has ever written, instead of falling back to age for the
    whole of an upgrade — which is the difference between freeing a dead run at once and leaving
    the estate blocked for its timeout.
    """
    data = metadata or {}
    for pid_field, host in ((PID_FIELD, str(data.get(HOST_FIELD) or "")),
                            ("pid", str(data.get("host_name") or host_fallback or ""))):
        raw_pid = data.get(pid_field)
        if raw_pid is None:
            continue
        try:
            return int(raw_pid), host
        except (TypeError, ValueError):
            continue
    return None, str(data.get(HOST_FIELD) or "")


def reap_verdict(
    *,
    metadata: dict | None,
    this_host: str,
    elapsed_seconds: float,
    timeout_seconds: float,
    pid_alive: bool | None,
    foreign_grace_seconds: int = FOREIGN_HOST_GRACE_SECONDS,
    this_node: str = "",
    at_timeout: bool = False,
    host_fallback: str = "",
) -> ReapVerdict:
    """May this ``running`` row be closed and its key released?

    ``host_fallback`` is the row's own ``host_name`` column: a row an older build wrote keeps its
    ``pid`` in the metadata and its host only there, and read without it the row looked like the
    reaper's own (0.27.0 item 1.96).

    ``pid_alive`` is the caller's reading of the recorded pid — ``None`` when there was no pid to
    read, or when the row belongs to another host and the question cannot be asked from here.

    ``timeout_seconds`` of ``0`` means *no timeout*, which is what a long-running service declares.
    Such a row is never reaped on age; only a dead pid frees it.

    ``at_timeout`` is the SQL-task reaper's rule (see the module docstring): a row the rules below
    would keep is closed all the same once it is past its timeout, with ``timed_out`` set.
    """
    verdict = _owner_verdict(
        metadata=metadata, this_host=this_host, elapsed_seconds=elapsed_seconds,
        timeout_seconds=timeout_seconds, pid_alive=pid_alive,
        foreign_grace_seconds=foreign_grace_seconds, this_node=this_node,
        host_fallback=host_fallback)
    if verdict.reap or not at_timeout:
        return verdict
    if timeout_seconds and elapsed_seconds >= timeout_seconds:
        return ReapVerdict(
            True, f"{int(elapsed_seconds)}s is past its timeout of {int(timeout_seconds)}s",
            timed_out=True)
    return verdict


def _owner_verdict(
    *,
    metadata: dict | None,
    this_host: str,
    elapsed_seconds: float,
    timeout_seconds: float,
    pid_alive: bool | None,
    foreign_grace_seconds: int,
    this_node: str,
    host_fallback: str = "",
) -> ReapVerdict:
    """The table of the module docstring: who owns the row, and is that owner still there."""
    pid, host = claim_owner(metadata, host_fallback=host_fallback)

    if pid is not None and host and host == this_host:
        if pid_alive:
            return ReapVerdict(False, f"pid {pid} is alive on {host}: the run is still going")
        return ReapVerdict(True, f"pid {pid} is gone on {host}")

    # The host name moved and the node did not: a recreated container. Its pids are not checked -
    # the new container's pid namespace starts again from 1 and the old number may well be alive
    # there as something else, which would hold the row for ever.
    if pid is not None and _left_by_an_earlier_host_of_this_node(
            metadata, host=host, this_host=this_host, this_node=this_node):
        return ReapVerdict(
            True,
            f"claimed by pid {pid} on {host}, a host name this node ({this_host}) no longer has: "
            f"its processes ended with that host")

    if pid is not None and host and host != this_host:
        if not timeout_seconds:
            # A service - timeout 0, it runs until its own daemon stops - is never reaped on age,
            # as the docstring above says; from another host its pid cannot be read either. Its
            # own host closes it. On 2026-09-30 a daemon started on the master against the
            # worker's store closed the worker's live web host this way (0.27.0 item 1.96); the
            # claim is per host, so leaving another host's row costs this one nothing.
            return ReapVerdict(
                False,
                f"claimed by pid {pid} on {host}: another host's service, closed only by its own host")
        if elapsed_seconds >= max(timeout_seconds, 0) + foreign_grace_seconds:
            return ReapVerdict(
                True,
                f"claimed by pid {pid} on {host}, which cannot be checked from {this_host}; "
                f"{int(elapsed_seconds)}s is past its timeout plus {foreign_grace_seconds}s of grace")
        return ReapVerdict(
            False,
            f"claimed by pid {pid} on {host}: another host's run is left alone until its timeout "
            f"plus {foreign_grace_seconds}s of grace")

    # No claim recorded - a row written by a build older than this rule. Age alone, exactly as
    # before: changing the answer for those rows would reap or keep them differently on the first
    # run after an upgrade, which is not a change anybody asked for.
    if timeout_seconds and elapsed_seconds >= timeout_seconds:
        return ReapVerdict(True, f"no claim recorded and {int(elapsed_seconds)}s is past "
                                 f"{int(timeout_seconds)}s")
    return ReapVerdict(False, "no claim recorded and still within its timeout")


def startup_verdict(
    *,
    metadata: dict | None,
    this_host: str,
    elapsed_seconds: float,
    timeout_seconds: float,
    pid_alive: bool | None,
    foreign_grace_seconds: int = FOREIGN_HOST_GRACE_SECONDS,
    this_node: str = "",
    host_fallback: str = "",
) -> ReapVerdict:
    """The same question asked at daemon startup, where one more fact is known.

    A daemon that has just started **owns no children**. So an open row belonging to this host was
    left by a life that has ended — unless its recorded pid is still alive, which happens when a
    child outlived the daemon that started it. Waiting for such a row's timeout costs exactly what
    the timeout is worth: on 2026-09-19 a restart left ``APP-SQL_TASKS`` blocked for 30 minutes and
    ``APP-METRICS`` for 40, on rows whose processes had been gone the whole time.

    The difference from :func:`reap_verdict` is only the no-claim case. There, a row with no pid is
    judged on age because the sweep cannot know whether something is working on it; here it can.
    Another host's row is still judged on age and grace, because this host learns nothing new about
    that one by having restarted.
    """
    # The row's own host column too: a row an older build wrote names its host only there, and
    # read without it another host's run was "pid N is gone" here - the master's daemon closed two
    # of the worker's runs two seconds after they started, and its web host (0.27.0 item 1.96).
    pid, host = claim_owner(metadata, host_fallback=host_fallback)
    if pid is not None and host and host != this_host:
        return reap_verdict(
            metadata=metadata, this_host=this_host, elapsed_seconds=elapsed_seconds,
            timeout_seconds=timeout_seconds, pid_alive=None,
            foreign_grace_seconds=foreign_grace_seconds, this_node=this_node,
            host_fallback=host_fallback)
    if pid is not None and pid_alive:
        return ReapVerdict(False, f"pid {pid} outlived its daemon and is still working")
    if pid is not None:
        return ReapVerdict(True, f"pid {pid} is gone")
    return ReapVerdict(
        True,
        "left open by a daemon that is no longer running, and this one owns no children yet")


__all__ = [
    "FOREIGN_HOST_GRACE_SECONDS",
    "HOST_FIELD",
    "NODE_FIELD",
    "PID_FIELD",
    "STARTED_FIELD",
    "ReapVerdict",
    "claim_fields",
    "claim_node",
    "claim_owner",
    "claim_started",
    "row_host",
    "row_metadata",
    "reap_verdict",
    "startup_verdict",
]
