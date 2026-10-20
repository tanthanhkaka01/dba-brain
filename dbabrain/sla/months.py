"""Availability by calendar month for SLA reporting.

The SLA app measures rolling windows (24h, 30d). For compliance and management reporting,
availability is also reported by calendar month: this calendar month to date and the previous
calendar month.

Month boundaries are determined in the node's display timezone (:func:`dbabrain.lib.timezone.display_zone`),
converted to UTC strings for database queries.

The previous month is closed and never changes. Because older samples move to ``metric_results_archive``
which has 22 million rows and no index except its primary key, scanning the archive on every 5-minute
run would be unacceptable. Instead, the previous month is computed once with ``include_archive=True``
and cached as JSON at ``<runtime_dir>/sla/months/<policy_id>_<YYYY-MM>.json``. Subsequent runs read
the cached file. If the cache file is absent or unreadable, it is recomputed without halting the run.
"""

from __future__ import annotations

import html
import json
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from dbabrain.lib.timezone import display_zone
from dbabrain.sla.compliance import COLLECTION_FAILURE_TYPES, _expected_scope
from dbabrain.sla.models import SlaPolicy
from dbabrain.sla.storage import SlaStore

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class TargetMonthStats:
    target_id: str
    observed_minutes: int
    good_minutes: int
    expected_minutes: int

    @property
    def down_minutes(self) -> int:
        return max(0, self.observed_minutes - self.good_minutes)

    @property
    def availability_percent(self) -> float:
        if self.observed_minutes <= 0:
            return 100.0
        return round((self.good_minutes / self.observed_minutes) * 100.0, 2)

    @property
    def coverage_percent(self) -> float:
        if self.expected_minutes <= 0:
            return 0.0
        return round(min(100.0, (self.observed_minutes / self.expected_minutes) * 100.0), 2)


def month_boundaries(
    reference_time: datetime | str | None = None,
) -> tuple[datetime, datetime, datetime]:
    """Compute (utc_prev_month_start, utc_this_month_start, utc_now) in UTC.

    Boundaries are calculated with respect to the node's display timezone.
    """
    if isinstance(reference_time, str):
        ref_dt = datetime.fromisoformat(reference_time.replace("Z", "+00:00"))
    elif isinstance(reference_time, datetime):
        ref_dt = reference_time
    else:
        ref_dt = datetime.now(timezone.utc)

    if ref_dt.tzinfo is None:
        ref_dt = ref_dt.replace(tzinfo=timezone.utc)

    tz = display_zone()
    local_ref = ref_dt.astimezone(tz)

    local_this_start = datetime(local_ref.year, local_ref.month, 1, 0, 0, 0, tzinfo=tz)
    if local_ref.month == 1:
        local_prev_start = datetime(local_ref.year - 1, 12, 1, 0, 0, 0, tzinfo=tz)
    else:
        local_prev_start = datetime(local_ref.year, local_ref.month - 1, 1, 0, 0, 0, tzinfo=tz)

    utc_prev_start = local_prev_start.astimezone(timezone.utc)
    utc_this_start = local_this_start.astimezone(timezone.utc)
    utc_now = ref_dt.astimezone(timezone.utc)

    return utc_prev_start, utc_this_start, utc_now


def previous_month_cache_path(
    runtime_dir: Path | str | None,
    policy_id: str,
    prev_month_label: str,
) -> Path | None:
    """Path to the cached JSON file for a closed previous month - None when no runtime folder is
    named. There is no default: a caller that names none (a test, a page rendered by hand) must not
    leave a month in the repository's own runtime/ that the node would then read as measured."""
    if runtime_dir is None:
        return None
    return Path(runtime_dir) / "sla" / "months" / f"{policy_id}_{prev_month_label}.json"


def _format_time(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def get_month_availability(
    store: SlaStore,
    policy: SlaPolicy,
    *,
    runtime_dir: Path | str | None = None,
    window_end: datetime | str | None = None,
    data_dir: Path | str | None = None,
) -> dict[str, Any]:
    """Compute or load availability stats for this month to date and the previous month."""
    utc_prev_start, utc_this_start, utc_now = month_boundaries(window_end)
    tz = display_zone()
    local_ref = utc_now.astimezone(tz)
    local_prev_start = utc_prev_start.astimezone(tz)

    this_month_name = local_ref.strftime("%B %Y")
    prev_month_name = local_prev_start.strftime("%B %Y")
    prev_month_label = f"{local_prev_start.year:04d}-{local_prev_start.month:02d}"

    prev_month_minutes = max(1, int((utc_this_start - utc_prev_start).total_seconds() / 60))
    this_elapsed_minutes = max(1, int((utc_now - utc_this_start).total_seconds() / 60))

    expected_targets, expected_servers = _expected_scope(policy, data_dir=data_dir)

    # 1. Previous month: read from cache or compute once with include_archive=True
    cache_path = previous_month_cache_path(runtime_dir, policy.policy_id, prev_month_label)
    prev_targets_data: dict[str, dict[str, int]] = {}
    loaded_from_cache = False
    if cache_path is not None and cache_path.is_file():
        try:
            raw_cache = json.loads(cache_path.read_text(encoding="utf-8"))
            if isinstance(raw_cache, dict):
                prev_targets_data = raw_cache.get("targets", raw_cache)
                loaded_from_cache = True
        except Exception:
            logger.warning("Failed to read month cache %s; recomputing", cache_path, exc_info=True)
            prev_targets_data = {}

    if not loaded_from_cache:
        # The archive has no index; scanning once per month saves millions of row scans.
        prev_counts = store.minute_counts(
            target_ids=policy.target_ids,
            server_ids=tuple(expected_servers),
            db_types=policy.db_types,
            metric_codes=policy.metric_codes,
            good_statuses=policy.good_statuses,
            excluded_error_types=COLLECTION_FAILURE_TYPES,
            window_end=_format_time(utc_this_start),
            window_starts={"prev": _format_time(utc_prev_start)},
            maintenance_windows=policy.maintenance_windows,
            include_archive=True,
        )
        prev_targets_data = {}
        for target_id, t_counts in prev_counts.items():
            obs, good = t_counts.get("prev", (0, 0))
            prev_targets_data[target_id] = {
                "observed": obs,
                "good": good,
                "month_minutes": prev_month_minutes,
            }
        # A month nobody measured is not cached: on a store that holds none of its rows (a new
        # node, the wrong store) the file would say "no minutes" for that month for ever.
        if cache_path is not None and prev_targets_data:
            try:
                cache_path.parent.mkdir(parents=True, exist_ok=True)
                cache_payload = {
                    "policy_id": policy.policy_id,
                    "month": prev_month_label,
                    "month_minutes": prev_month_minutes,
                    "targets": prev_targets_data,
                }
                cache_path.write_text(json.dumps(cache_payload, ensure_ascii=False, indent=2), encoding="utf-8")
            except Exception:  # noqa: BLE001 - an unwritable cache costs a recount, not the page.
                logger.warning("Failed to write month cache %s", cache_path, exc_info=True)

    # 2. This month to date: live metric_results only
    this_counts = store.minute_counts(
        target_ids=policy.target_ids,
        server_ids=tuple(expected_servers),
        db_types=policy.db_types,
        metric_codes=policy.metric_codes,
        good_statuses=policy.good_statuses,
        excluded_error_types=COLLECTION_FAILURE_TYPES,
        window_end=_format_time(utc_now),
        window_starts={"this": _format_time(utc_this_start)},
        maintenance_windows=policy.maintenance_windows,
        include_archive=False,
    )

    all_targets = sorted(set(expected_targets) | set(prev_targets_data.keys()) | set(this_counts.keys()))

    target_stats: dict[str, dict[str, TargetMonthStats]] = {}
    for target_id in all_targets:
        t_this = this_counts.get(target_id, {}).get("this", (0, 0))
        t_prev_entry = prev_targets_data.get(target_id, {})
        if isinstance(t_prev_entry, dict):
            p_obs = int(t_prev_entry.get("observed", 0))
            p_good = int(t_prev_entry.get("good", 0))
            p_min = int(t_prev_entry.get("month_minutes", prev_month_minutes))
        elif isinstance(t_prev_entry, (list, tuple)) and len(t_prev_entry) >= 2:
            p_obs = int(t_prev_entry[0])
            p_good = int(t_prev_entry[1])
            p_min = prev_month_minutes
        else:
            p_obs, p_good, p_min = 0, 0, prev_month_minutes

        target_stats[target_id] = {
            "this_month": TargetMonthStats(
                target_id=target_id,
                observed_minutes=t_this[0],
                good_minutes=t_this[1],
                expected_minutes=this_elapsed_minutes,
            ),
            "prev_month": TargetMonthStats(
                target_id=target_id,
                observed_minutes=p_obs,
                good_minutes=p_good,
                expected_minutes=p_min,
            ),
        }

    return {
        "policy_id": policy.policy_id,
        "this_month_name": this_month_name,
        "prev_month_name": prev_month_name,
        "target_stats": target_stats,
    }


def render_calendar_month_section(
    policies: list[SlaPolicy],
    store: SlaStore,
    *,
    runtime_dir: Path | str | None = None,
    window_end: datetime | str | None = None,
    data_dir: Path | str | None = None,
) -> str:
    """Render the 'Availability by calendar month' HTML section.

    Returns an empty string if no policy in ``policies`` has ``calendar_month_report=True``.
    """
    eligible = [p for p in policies if p.calendar_month_report]
    if not eligible:
        return ""

    sections = []
    for policy in eligible:
        month_info = get_month_availability(
            store,
            policy,
            runtime_dir=runtime_dir,
            window_end=window_end,
            data_dir=data_dir,
        )
        target_stats = month_info["target_stats"]
        this_month_name = month_info["this_month_name"]
        prev_month_name = month_info["prev_month_name"]

        rows = []
        for target_id, stats in sorted(target_stats.items()):
            t_this = stats["this_month"]
            t_prev = stats["prev_month"]

            this_avail = f"{t_this.availability_percent:.2f}%" if t_this.observed_minutes > 0 else "—"
            this_down = f"{t_this.down_minutes} min"
            this_cov = f"{t_this.coverage_percent:.2f}%"

            prev_avail = f"{t_prev.availability_percent:.2f}%" if t_prev.observed_minutes > 0 else "—"
            prev_down = f"{t_prev.down_minutes} min"
            prev_cov = f"{t_prev.coverage_percent:.2f}%"

            rows.append(
                "<tr>"
                f'<td class="srv"><code>{html.escape(target_id)}</code></td>'
                f'<td class="num-cell">{this_avail}</td>'
                f'<td class="num-cell">{this_down}</td>'
                f'<td class="num-cell">{this_cov}</td>'
                f'<td class="num-cell">{prev_avail}</td>'
                f'<td class="num-cell">{prev_down}</td>'
                f'<td class="num-cell">{prev_cov}</td>'
                "</tr>"
            )

        tbody = "".join(rows) if rows else '<tr><td colspan="7" class="muted">No targets evaluated.</td></tr>'
        sections.append(
            '<section>'
            '<div class="sec-head"><h2>Availability by calendar month</h2>'
            f'<span class="hint">{html.escape(policy.name or policy.policy_id)}: '
            'Availability in whole minutes for this calendar month to date and the previous closed month.</span></div>'
            '<div class="tbl-scroll">'
            '<table>'
            '<thead>'
            '<tr>'
            '<th rowspan="2">Instance</th>'
            f'<th colspan="3" class="center">{html.escape(this_month_name)} (to date)</th>'
            f'<th colspan="3" class="center">{html.escape(prev_month_name)}</th>'
            '</tr>'
            '<tr>'
            '<th class="num-cell">Availability</th>'
            '<th class="num-cell">Down</th>'
            '<th class="num-cell">Coverage</th>'
            '<th class="num-cell">Availability</th>'
            '<th class="num-cell">Down</th>'
            '<th class="num-cell">Coverage</th>'
            '</tr>'
            '</thead>'
            f'<tbody>{tbody}</tbody>'
            '</table>'
            '</div>'
            '</section>'
        )

    return "\n".join(sections)
