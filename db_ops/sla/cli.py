from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict
from pathlib import Path
from datetime import datetime, timezone

from db_ops.config import load_config, resolve_config_path
from db_ops.db import DbOpsStore
from db_ops.logging_ops import log_event, log_function_call, log_function_error, setup_app_logger
from db_ops.logging_ops.runtime_stdout import patch_stdout
from db_ops.sla.compliance import validate_sla_policies
from db_ops.sla.policies import DEFAULT_POLICIES_PATH, load_sla_policies
from db_ops.sla.publish import (build_transition_message, chat_id_for_level, publish_html,
                                summary_notify_level, transition_status)
from db_ops.sla.storage import SlaStore


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="DB Ops SLA/SLI/SLO compliance (metrics-only, no DB connections).")
    parser.add_argument("--config", default=None, help="Path to config JSON. Defaults to config.sla.json or config.json.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    validate = subparsers.add_parser("validate", help="Compute SLIs from metric_results, evaluate SLOs, and store the run.")
    validate.add_argument("--policies", default=str(DEFAULT_POLICIES_PATH), help="Path to SLA policy JSON.")
    validate.add_argument("--window-end", help="UTC ISO timestamp used as validation window end.")
    validate.add_argument("--format", choices=["json", "text", "markdown"], default="json")
    validate.add_argument("--no-store", action="store_true", help="Compute only; do not write sla_runs/sla_results.")
    validate.add_argument("--notify", action="store_true", help="Queue a Telegram message (via telegram_send_messages) summarizing the run.")
    validate.add_argument("--notify-always", action="store_true", help="With --notify, send even when every policy PASSED (default: only when something needs attention).")
    validate.add_argument("--publish-web", action="store_true", help="Render an HTML page into the webhost root (served at /report_dba/sla.html).")
    validate.add_argument("--web-dir", default=None, help="Directory for the HTML page. Default: <runtime>/reports.")
    validate.add_argument("--allow-fail", action="store_true", help="Always exit 0 even when compliance fails.")

    history = subparsers.add_parser("history", help="Show recently stored SLA validation runs from SQLite.")
    history.add_argument("--limit", type=int, default=10, help="How many recent runs to show. Default: 10.")
    history.add_argument("--run-id", type=int, help="Show per-policy results for one stored run.")
    history.add_argument("--format", choices=["json", "text"], default="text")
    check = subparsers.add_parser("validate-config", help="Validate SLA policy configuration without evaluating data.")
    check.add_argument("--policies", default=str(DEFAULT_POLICIES_PATH))
    listing = subparsers.add_parser("list-definitions", help="List configured SLI/SLO definitions.")
    listing.add_argument("--policies", default=str(DEFAULT_POLICIES_PATH))
    listing.add_argument("--format", choices=["json", "text"], default="text")
    return parser.parse_args(argv)


def main(argv: list[str]) -> int:
    args = parse_args(argv)
    logger = None
    try:
        config = load_config(resolve_config_path("sla", args.config))
        patch_stdout(config.log_dir / "sla_runtime.log", app_name="sla")
        logger = setup_app_logger(config, app_name="sla", enable_telegram_alerts=False, enable_console=False)
        log_function_call(logger, function_name=f"sla.{args.command}")
        if args.command == "validate":
            return _run_validate(args, config, logger)
        if args.command == "history":
            return _run_history(args, config)
        if args.command == "validate-config":
            policies = load_sla_policies(args.policies)
            print(f"Valid SLA configuration: {len(policies)} enabled definitions.")
            return 0
        if args.command == "list-definitions":
            policies = load_sla_policies(args.policies)
            payload = [asdict(policy) for policy in policies]
            if args.format == "json":
                print(json.dumps(payload, ensure_ascii=False, indent=2))
            else:
                for policy in policies:
                    print(f"{policy.sli_code or policy.policy_id}: {policy.category or 'uncategorized'} "
                          f"{policy.aggregation} {policy.comparison_operator} "
                          f"{policy.target_value if policy.target_value is not None else policy.objective_percent}")
            return 0
        raise ValueError(f"Unknown command: {args.command}")
    except Exception as exc:  # noqa: BLE001 - command-line failure path.
        if logger:
            log_function_error(logger, function_name=f"sla.{getattr(args, 'command', 'cli')}", error_text=str(exc))
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


def _run_validate(args: argparse.Namespace, config, logger) -> int:
    started_at = _utc_now()
    policies = load_sla_policies(args.policies)
    # data_dir is where the enabled-target inventory lives: the evaluated population comes from
    # it, not from whichever samples happen to be in the window. Passing it explicitly is what
    # keeps a synthetic store in a test from being measured against the production estate.
    summary = validate_sla_policies(
        sqlite_path=config.store,
        policies=policies,
        window_end=args.window_end,
        data_dir=getattr(config, "data_dir", None),
    )
    finished_at = _utc_now()
    sla_run_id = None
    if not args.no_store:
        sla_run_id = SlaStore.from_config(config).save_summary(
            summary=summary,
            started_at=started_at,
            finished_at=finished_at,
            message=f"status={summary.status} passed={summary.passed_count} at_risk={summary.at_risk_count} "
            f"failed={summary.failed_count} no_data={summary.no_data_count}",
        )
    notified_id = _maybe_notify_telegram(args, config, summary, logger, sla_run_id=sla_run_id) if args.notify else None
    web_path = _maybe_publish_web(args, config, summary, sla_run_id=sla_run_id) if args.publish_web else None

    payload = asdict(summary)
    payload["sla_run_id"] = sla_run_id
    payload["telegram_send_message_id"] = notified_id
    payload["web_page"] = str(web_path) if web_path else None
    if args.format == "json":
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    elif args.format == "markdown":
        print(_format_validate_markdown(payload))
    else:
        print(_format_validate_text(payload))
    log_event(
        logger,
        level="logging",
        message=f"sla.validate finished: sla_run_id={sla_run_id} status={summary.status} "
        f"telegram={notified_id} web={web_path}",
    )
    return 0 if args.allow_fail or summary.status == "PASSED" else 1


def _maybe_notify_telegram(args, config, summary, logger, *, sla_run_id: int | None = None) -> int | None:
    """Notify on state change, not on schedule.

    This ran hourly and sent every hour: the only suppression was "everything passed", which on a
    real estate is never true. 78 messages in 76 hours, averaging 3,852 characters of largely
    unchanged text. Nothing in the queue de-duplicates, so the restraint has to be decided here —
    what a reader needs is the moment something breaks, the moment it recovers, and one daily
    reminder that the rest is still open.
    """
    from db_ops.lib.state_transition import decide_notification, diff_states
    from db_ops.db.telegram_queue import last_queued_at
    from db_ops.sla.models import state_key

    store = SlaStore.from_config(config)
    previous = store.fetch_previous_state(before_run_id=sla_run_id)
    current = {state_key(result.policy_id, result.target_id): result.status for result in summary.results}
    # Worst-first, and every status the evaluator can produce is named: an unranked status would
    # sort as least severe and could hide a genuine escalation.
    diff = diff_states(previous, current, severity_order=SLA_SEVERITY_ORDER, healthy=("PASSED",))
    decision = decide_notification(
        diff,
        last_sent_at=last_queued_at(store=DbOpsStore.from_config(config), source_type="sla", note="sla_validate"),
        reminder_after_seconds=_reminder_seconds(config),
        always=bool(args.notify_always),
    )
    if not decision.send:
        log_event(logger, level="logging", message=f"sla.notify suppressed: {decision.reason}")
        return None
    # Routing (enabled / does this level alert / which chat) is owned by
    # db_ops.common.cli telegram-route; this app applies no rules of its own.
    from db_ops.lib.telegram_route import telegram_route

    level = summary_notify_level(summary)
    route = telegram_route(level)
    if not route["enabled"]:
        log_event(logger, level="logging", message="sla.notify skipped: telegram disabled in config.")
        return None
    chat_id = route["chat_id"] if route["alert"] else ""
    if not chat_id:
        log_event(logger, level="warning", message=f"sla.notify skipped: no telegram group for level={level}.")
        return None

    from db_ops.common.data_sources import report_base_url

    from db_ops.db.queue_message import queue_message, store_block

    return queue_message({
        "store": store_block(config),
        "chat_id": chat_id,
        # No configured base URL means no absolute link to give: a Telegram message cannot
        # follow a relative href, so it says nothing rather than pointing at "sla.html".
        "text": build_transition_message(summary, diff, decision,
                                        report_url=(_base + "sla.html") if (_base := report_base_url()) else ""),
        # The severity of the CHANGE, not of the fleet: with a standing backlog the run status is
        # FAILED forever, which would stamp a failure emoji on a message reporting three
        # recoveries. Tagging itself stays in db_ops.telegram.severity — this only tells it what
        # kind of news the message carries.
        "status": transition_status(diff, decision),
        "level": level,
        "note": "sla_validate",
        "source_type": "sla",
        # Identifies what the message is ABOUT, not when it was produced. The old
        # `sla:<window_end>` was unique every run, so the stored history could not answer "have we
        # already told them about this?" — with a fingerprint of the transition, repeats of the
        # same change are greppable after the fact. Nothing enforces on it; suppression is
        # decide_notification's job.
        "source_id": f"sla:{decision.kind}:{_state_fingerprint(diff)}",
        "metadata": {
            "status": summary.status,
            "passed": summary.passed_count,
            "at_risk": summary.at_risk_count,
            "failed": summary.failed_count,
            "no_data": summary.no_data_count,
            "level": level,
            "notify_kind": decision.kind,
            "notify_reason": decision.reason,
            **diff.counts,
        },
    })


#: Worst first. Used to tell an escalation (AT_RISK -> FAILED) from a de-escalation, so a finding
#: that got worse can interrupt while one that improved does not.
SLA_SEVERITY_ORDER = ("FAILED", "AT_RISK", "NO_DATA", "PASSED")


def _reminder_seconds(config) -> int:
    """How long an unresolved finding stays quiet, from ``data/sla_policies.json``.

    Config, not a literal: how often a standing problem should be re-stated is an operations
    decision that changes with who is on call, and it belongs where the rest of the SLA tuning is.
    """
    from db_ops.lib.state_transition import DEFAULT_REMINDER_SECONDS

    try:
        document = json.loads(
            (Path(getattr(config, "data_dir", None) or "data") / "sla_policies.json").read_bytes().decode("utf-8-sig")
        )
    except (OSError, ValueError):
        return DEFAULT_REMINDER_SECONDS
    settings = document.get("notification") if isinstance(document, dict) else None
    try:
        return int((settings or {}).get("reminder_after_seconds") or DEFAULT_REMINDER_SECONDS)
    except (TypeError, ValueError):
        return DEFAULT_REMINDER_SECONDS


def _state_fingerprint(diff) -> str:
    """A short, stable digest of the transition this message is about.

    Two runs that found exactly the same change produce the same fingerprint and therefore the
    same ``source_id``, which is what lets the queue recognise a repeat at all.
    """
    import hashlib

    payload = "|".join(
        ",".join(sorted(part))
        for part in (diff.new_bad, diff.recovered, [item[0] for item in diff.worsened],
                     [item[0] for item in diff.improved], diff.unchanged_bad)
    )
    return hashlib.sha1(payload.encode("utf-8")).hexdigest()[:16]


#: How many runs the page's history table shows. Stated on the page rather than left implied:
#: on an hourly schedule this is about the last 15 hours, not the whole history.
WEB_HISTORY_LIMIT = 15


def _maybe_publish_web(args: argparse.Namespace, config, summary, *, sla_run_id: int | None = None):
    out_dir = args.web_dir if args.web_dir else (config.runtime_dir / "reports")
    store = SlaStore.from_config(config)
    recent_runs = [dict(row) for row in store.fetch_recent_runs(limit=WEB_HISTORY_LIMIT)]
    # The same previous-run comparison the Telegram routing uses, so the page and the alert can
    # never disagree about what changed.
    return publish_html(summary, recent_runs=recent_runs, out_dir=out_dir,
                        previous_state=store.fetch_previous_state(before_run_id=sla_run_id),
                        history_limit=WEB_HISTORY_LIMIT)


def _run_history(args: argparse.Namespace, config) -> int:
    store = SlaStore.from_config(config)
    if args.run_id is not None:
        results = [dict(row) for row in store.fetch_run_results(sla_run_id=args.run_id)]
        if args.format == "json":
            print(json.dumps({"sla_run_id": args.run_id, "results": results}, ensure_ascii=False, indent=2))
        else:
            print(_format_run_results_text(args.run_id, results))
        return 0
    runs = [dict(row) for row in store.fetch_recent_runs(limit=args.limit)]
    if args.format == "json":
        print(json.dumps({"runs": runs}, ensure_ascii=False, indent=2))
    else:
        print(_format_runs_text(runs))
    return 0


def _format_validate_text(payload: dict) -> str:
    lines = [
        f"SLA/SLO compliance: {payload['status']} (sla_run_id={payload.get('sla_run_id')})",
        f"policies={payload['policy_count']} instances={payload['result_count']} passed={payload['passed_count']} "
        f"at_risk={payload['at_risk_count']} failed={payload['failed_count']} no_data={payload['no_data_count']}",
    ]
    for result in payload["results"]:
        lines.append(
            f"- {_result_label(result)}: {result['status']} "
            f"actual={result['actual_percent']}% objective={result['objective_percent']}% "
            f"budget_left={result['budget_remaining_percent']}% "
            f"good={result['good_count']} bad={result['bad_count']} total={result['total_count']}"
        )
    return "\n".join(lines)


def _format_validate_markdown(payload: dict) -> str:
    lines = [
        "# SLA / SLO compliance report", "", f"Overall status: **{payload['status']}**", "",
        "## Executive summary", "",
        f"Passed: {payload['passed_count']}; at risk: {payload['at_risk_count']}; "
        f"failed: {payload['failed_count']}; no data: {payload['no_data_count']}.", "",
        "## SLI results", "", "| SLI | Target | Domain | Actual | Objective | Status | Coverage | Data quality |",
        "| --- | --- | --- | ---: | ---: | --- | ---: | --- |",
    ]
    for result in payload["results"]:
        lines.append(
            f"| {result['sli_code'] or result['policy_id']} | {result['target_id']} | {result['domain']} | "
            f"{result['actual_value']} | {result['objective_value']} | {result['status']} | "
            f"{result['coverage_percent']}% | {result['data_quality_status']} |"
        )
    lines.extend(["", "## Error budget and burn rate", ""])
    for result in payload["results"]:
        lines.append(f"- {result['sli_code'] or result['policy_id']}: budget remaining "
                     f"{result['error_budget_remaining']}; burn rate {result['burn_rate']}.")
    lines.extend(["", "## Evidence and metric timestamps", "", f"Evaluation ended: `{payload['window_end']}`."])
    return "\n".join(lines)


def _result_label(result: dict) -> str:
    target_id = result.get("target_id") or "*"
    if target_id and target_id != "*":
        return f"{result['policy_id']} @ {target_id}"
    return result["policy_id"]


def _format_runs_text(runs: list[dict]) -> str:
    if not runs:
        return "No SLA runs stored yet."
    lines = ["Recent SLA runs:"]
    for run in runs:
        lines.append(
            f"- #{run['sla_run_id']} {run['finished_at'] or run['started_at']} {run['status']}: "
            f"passed={run['passed_count']} at_risk={run['at_risk_count']} "
            f"failed={run['failed_count']} no_data={run['no_data_count']}"
        )
    return "\n".join(lines)


def _format_run_results_text(sla_run_id: int, results: list[dict]) -> str:
    if not results:
        return f"No stored results for sla_run_id={sla_run_id}."
    lines = [f"SLA run #{sla_run_id} results:"]
    for result in results:
        lines.append(
            f"- {_result_label(result)}: {result['status']} "
            f"actual={result['actual_percent']}% objective={result['objective_percent']}% "
            f"budget_left={result['budget_remaining_percent']}%"
        )
    return "\n".join(lines)


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
