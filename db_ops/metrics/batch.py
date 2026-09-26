"""One target's metric items, run by ``common.cli metric-batch`` - the metrics side of the call.

Running a metric is an operation and belongs to ``common`` (rules R03, R10); deciding what to run
and what the answer means is this app's. So a metric is *prepared* here - the item ``common`` runs,
and how its answer turns back into rows or the same exception the in-process code raised - and a
target's prepared items go out in one process: one per target per pass, not one per metric, which
was measured at 43-138 s of interpreter start-up a pass (``tests/test_app_common_imports.py``).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from db_ops.metrics.models import MetricTarget
from db_ops.transport import common_cli

#: What a PostgreSQL per-database item may cost at most, per database, on top of its timeout.
_CONNECT_ALLOWANCE_SECONDS = 5
#: Headroom for the process itself: interpreter start-up, imports, a slow first connect.
_BATCH_HEADROOM_SECONDS = 120


@dataclass(frozen=True)
class Prepared:
    """A metric ready to run: the item ``common`` executes, and how to read its answer.

    ``interpret`` returns what the in-process runner returned, or raises what it raised - the
    grading downstream (severity, overrides, the stored row) cannot tell the two paths apart, and
    ``tests/test_metric_outcomes_survive_the_batch.py`` holds it to that.
    """

    item: dict[str, Any]
    interpret: Callable[[dict[str, Any]], Any]


def run(target: MetricTarget | None, secrets: dict[str, str], items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """One answer per item, in order. A batch that does not answer at all fails every item in it -
    as data, like any other failure, so the rest of the pass carries on."""
    from db_ops.metrics.executor import connection_block

    items = [{**item, "id": str(item.get("id") or index)} for index, item in enumerate(items)]
    request = {"target": connection_block(target, secrets), "secrets": named_secrets(target, secrets),
               "items": items}
    try:
        data = common_cli.run("metric-batch", request, timeout_seconds=_batch_timeout(items))
    except common_cli.CommonCliError as exc:
        return [_failed(item, f"metric-batch did not answer: {exc}") for item in items]
    answers = data.get("items") if isinstance(data, dict) else None
    if not isinstance(answers, list) or len(answers) != len(items):
        count = len(answers) if isinstance(answers, list) else 0
        return [_failed(item, f"metric-batch answered {count} item(s) for {len(items)}") for item in items]
    return answers


def run_one(target: MetricTarget | None, secrets: dict[str, str], prepared: Prepared) -> Any:
    """A single prepared metric, run and read - for the callers that hold one metric, not a target."""
    [answer] = run(target, secrets, [prepared.item])
    return prepared.interpret(answer)


def named_secrets(target: MetricTarget | None, secrets: dict[str, str]) -> dict[str, str]:
    """The secret values this target's configuration names by ref - and no others.

    ``common`` resolves a ref exactly as it did in-process (the bridge's shared secret, a remote
    login's ``password_ref``), so its messages do not change; the request carries only what it
    names, never the whole store.
    """
    if target is None:
        return {}
    refs: set[str] = set()
    for block in (target.credential, target.cmd_credential, target.cmd_access, target.sql_access):
        if not isinstance(block, dict):
            continue
        for key, value in block.items():
            if str(key).endswith("_ref") and str(value or "").strip():
                refs.add(str(value).strip())
    return {ref: secrets[ref] for ref in sorted(refs) if ref in secrets}


def _batch_timeout(items: list[dict[str, Any]]) -> int:
    """A bound on the whole process, well past what its items may take between them.

    Each item carries its own timeout, as it did in-process. This one is only for a batch that
    stops answering altogether - a driver hung below its own timeout - which in-process held that
    server's worker for the rest of the pass; now it costs that batch, with the reason.
    """
    total = 0
    for item in items:
        per_run = int(item.get("timeout_seconds") or 5) + _CONNECT_ALLOWANCE_SECONDS
        runs = int(item.get("max_databases") or 1) + 1 if item.get("per_database") else 1
        total += per_run * runs
    return _BATCH_HEADROOM_SECONDS + 2 * total


def _failed(item: dict[str, Any], message: str) -> dict[str, Any]:
    return {"id": item.get("id"), "kind": item.get("kind"),
            "error": {"message": message, "failure_phase": "", "kind": "batch"}}


def raise_other(error: dict[str, Any]) -> None:
    """Re-raise a failure no finer class describes, keeping what grades it: the message, and the
    phase when the raiser declared one (``lib.event_policy.resolve_failure_phase`` reads either)."""
    exc = RuntimeError(str(error.get("message") or ""))
    phase = str(error.get("failure_phase") or "")
    if phase:
        exc.failure_phase = phase  # type: ignore[attr-defined]
    raise exc
