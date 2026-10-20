"""Pure arithmetic for latency percentiles across metric collections.

Three latency collectors (Query Store, Oracle wait event histogram, and PostgreSQL
pg_stat_statements) record raw totals or pre-aggregated statistics without grading
them. The arithmetic across collections lives here in ``dbabrain/lib``, evaluated by
the report on demand — the same contract as :mod:`dbabrain.lib.interval_rates`.

Why the arithmetic is split by engine source:

* **SQL Server (Query Store)**: Query Store aggregates runtime statistics into discrete
  time intervals on the server. Because collection runs on a timer, consecutive collections
  frequently overlap by one interval. Deduplicating by ``(target_id, database, interval_start_utc)``
  and keeping the newest collection makes a slight overlap harmless.
* **Oracle (V$EVENT_HISTOGRAM)**: The view records cumulative wait counts across power-of-two
  duration buckets since instance startup. Subtracting older from newer bucket counts gives the
  interval's waits. The percentile returned is the upper bound of the first bucket whose
  cumulative share reaches the quantile — a discrete bucket bound, never an interpolation,
  because interpolating across powers of two implies uniform density within an exponential band
  that the engine never recorded. An instance restart or backwards counter refuses the pair.
* **PostgreSQL (pg_stat_statements)**: The view tracks cumulative execution counts and total time
  per queryid without maintaining a distribution within any individual statement. Interval
  percentiles are computed as calls-weighted percentiles of per-statement interval means
  (delta total time / delta calls) across queryids observed in both collections with positive
  calls deltas. The spread inside a single statement is invisible to the view, so this is
  explicitly a distribution of statement means weighted by call volume.
* **Discrete weighted percentiles**: :func:`weighted_percentiles` computes the value where
  cumulative weight first reaches or exceeds quantile q % of total weight, matching SQL Server's
  server-side discrete percentile calculation by construction.
"""

from __future__ import annotations

import re
from typing import Any, Iterable, Sequence

from dbabrain.lib.coerce import as_epoch, as_float, as_int
from dbabrain.lib.interval_rates import message_fields


def _to_epoch(value: Any) -> float | None:
    """Epoch timestamp in seconds, accepting numeric epochs or ISO strings."""
    if isinstance(value, (int, float)):
        return float(value)
    return as_epoch(value)


def query_store_intervals(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Deduplicate Query Store interval rows across collections, keeping the newest.

    SQL Server Query Store groups runtime statistics into fixed time intervals. Two
    consecutive collections frequently cover the same interval; keeping the row from
    the newest ``collected_at`` ensures that a slight collection overlap is harmless
    and updates any in-flight interval with the final execution counts.

    Entries are returned sorted by ``interval_start_utc``, with all numeric metric
    fields converted to floats.
    """
    newest_by_key: dict[
        tuple[Any, str, str],
        tuple[tuple[float, str], dict[str, Any], dict[str, str], str, str],
    ] = {}

    for row in rows:
        fields = message_fields(row.get("message"))
        target_id = row.get("target_id")

        database = fields.get("database")
        if not database:
            metric_item = str(row.get("metric_item") or "")
            if " @ " in metric_item:
                database = metric_item.split(" @ ", 1)[0].strip()
            else:
                database = metric_item.strip()

        interval_start_utc = fields.get("interval_start_utc") or fields.get("interval_start")
        if not interval_start_utc:
            metric_item = str(row.get("metric_item") or "")
            if " @ " in metric_item:
                interval_start_utc = metric_item.split(" @ ", 1)[1].strip()
            else:
                interval_start_utc = ""

        if not interval_start_utc or not database:
            continue

        key = (target_id, database, interval_start_utc)
        epoch = _to_epoch(row.get("collected_at"))
        stamp_key = (
            epoch if epoch is not None else float("-inf"),
            str(row.get("collected_at") or ""),
        )

        if key not in newest_by_key or stamp_key > newest_by_key[key][0]:
            newest_by_key[key] = (stamp_key, row, fields, database, interval_start_utc)

    results: list[dict[str, Any]] = []
    for _, row, fields, database, start_utc in newest_by_key.values():
        end_utc = fields.get("interval_end_utc") or fields.get("interval_end") or ""
        results.append({
            "target_id": row.get("target_id"),
            "database": database,
            "interval_start_utc": start_utc,
            "interval_start": start_utc,
            "interval_end_utc": end_utc,
            "interval_end": end_utc,
            "executions": as_float(fields.get("executions")),
            "plans": as_float(fields.get("plans")),
            "p50_ms": as_float(fields.get("p50_ms")),
            "p95_ms": as_float(fields.get("p95_ms")),
            "p99_ms": as_float(fields.get("p99_ms")),
            "avg_ms": as_float(fields.get("avg_ms")),
            "max_ms": as_float(fields.get("max_ms")),
            "measured_as": fields.get("measured_as") or "",
            "collected_at": row.get("collected_at"),
        })

    results.sort(
        key=lambda item: (
            _to_epoch(item["interval_start_utc"]) or 0.0,
            str(item["interval_start_utc"]),
            str(item["database"]),
            str(item["target_id"]),
        )
    )
    return results


def _parse_buckets(buckets_input: Any) -> dict[str, int]:
    """Parse pipe- or comma-separated bucket counts (e.g. 'le_1ms:100|le_2ms:200')."""
    if isinstance(buckets_input, dict):
        return {str(k): as_int(v, default=0) for k, v in buckets_input.items()}
    text = str(buckets_input or "").strip()
    if not text:
        return {}
    parsed: dict[str, int] = {}
    parts = re.split(r"[|,]", text)
    for part in parts:
        part = part.strip()
        if not part:
            continue
        if ":" in part:
            name, val = part.split(":", 1)
        elif "=" in part:
            name, val = part.split("=", 1)
        else:
            continue
        parsed[name.strip()] = as_int(val.strip(), default=0)
    return parsed


def _parse_bound_ms(name: str) -> float | int:
    """Extract numeric milliseconds from a bucket name like 'le_1ms', '<=8ms', or 'le_64ms'."""
    m = re.search(r"(\d+(?:\.\d+)?)\s*(ms|s|us)?", name.lower())
    if not m:
        return 0.0
    val = float(m.group(1))
    unit = m.group(2)
    if unit == "s":
        val *= 1000.0
    elif unit == "us":
        val /= 1000.0
    return int(val) if val.is_integer() else val


def histogram_interval(
    older_row: dict[str, Any],
    newer_row: dict[str, Any],
    quantiles: Sequence[int] = (50, 95, 99),
) -> dict[str, Any] | None:
    """Difference two Oracle wait event histogram collections into interval percentiles.

    Oracle V$EVENT_HISTOGRAM provides cumulative wait counts since instance startup
    partitioned into power-of-two duration buckets (<=1ms, <=2ms, <=4ms, etc.).
    Differencing newer and older collections yields per-bucket wait deltas.

    Returns None if counters_since differ (indicating the instance restarted between
    collections) or if any per-bucket delta is negative.

    The returned pXX_ms_le values are discrete upper bounds of the first bucket whose
    cumulative share of the interval's waits reaches or exceeds XX %, rather than an
    interpolation across buckets. Interpolating inside an exponential power-of-two band
    invents a distribution that Oracle did not record.

    If total waits in the interval is 0, all percentiles return None.
    """
    older_fields = message_fields(older_row.get("message"))
    newer_fields = message_fields(newer_row.get("message"))

    older_cs = older_fields.get("counters_since") or str(older_row.get("counters_since") or "")
    newer_cs = newer_fields.get("counters_since") or str(newer_row.get("counters_since") or "")
    if older_cs and newer_cs and older_cs != newer_cs:
        return None

    older_buckets = _parse_buckets(older_fields.get("buckets") or older_row.get("buckets"))
    newer_buckets = _parse_buckets(newer_fields.get("buckets") or newer_row.get("buckets"))

    all_bucket_names = sorted(
        set(older_buckets) | set(newer_buckets),
        key=lambda name: (float(_parse_bound_ms(name)), name),
    )

    buckets_delta: dict[str, int] = {}
    for name in all_bucket_names:
        delta = newer_buckets.get(name, 0) - older_buckets.get(name, 0)
        if delta < 0:
            return None
        buckets_delta[name] = delta

    total_waits = sum(buckets_delta.values())

    older_epoch = _to_epoch(older_row.get("collected_at"))
    newer_epoch = _to_epoch(newer_row.get("collected_at"))
    seconds = 0.0
    if older_epoch is not None and newer_epoch is not None:
        seconds = round(newer_epoch - older_epoch, 1)
        if seconds < 0:
            return None

    event = (
        newer_fields.get("event")
        or older_fields.get("event")
        or str(newer_row.get("metric_item") or older_row.get("metric_item") or "")
    )

    result: dict[str, Any] = {
        "event": event,
        "seconds": seconds,
        "waits": int(total_waits),
        "buckets_delta": buckets_delta,
    }

    if total_waits == 0:
        for q in quantiles:
            result[f"p{q}_ms_le"] = None
    else:
        pairs = [
            (float(_parse_bound_ms(name)), float(delta))
            for name, delta in buckets_delta.items()
            if delta > 0
        ]
        pcts = weighted_percentiles(pairs, quantiles)
        for q in quantiles:
            val = pcts.get(q)
            if val is not None and isinstance(val, (int, float)) and float(val).is_integer():
                result[f"p{q}_ms_le"] = int(val)
            else:
                result[f"p{q}_ms_le"] = val

    return result


def histogram_series(
    rows: list[dict[str, Any]],
    quantiles: Sequence[int] = (50, 95, 99),
) -> list[dict[str, Any]]:
    """Difference consecutive pairs of wait event histogram rows, sorted by time.

    Consecutive pairs tile time exactly with no gaps and no overlaps. Any pair that
    spans an instance restart or records backwards counters is refused and skipped.
    """
    if len(rows) < 2:
        return []

    sorted_rows = sorted(
        rows,
        key=lambda r: (_to_epoch(r.get("collected_at")) or 0.0, str(r.get("collected_at") or "")),
    )

    results: list[dict[str, Any]] = []
    for i in range(len(sorted_rows) - 1):
        interval = histogram_interval(sorted_rows[i], sorted_rows[i + 1], quantiles=quantiles)
        if interval is not None:
            results.append(interval)
    return results


def is_not_installed(rows: list[dict[str, Any]]) -> bool:
    """Whether pg_stat_statements was reported as uninstalled."""
    for r in rows:
        val = str(r.get("metric_value") or "")
        item = str(r.get("metric_item") or "")
        msg = str(r.get("message") or "")
        if val == "NOT_INSTALLED" or "NOT_INSTALLED" in (item, msg):
            return True
        if item == "pg_stat_statements" and val == "NOT_INSTALLED":
            return True
    return False


def _find_all_statements(rows: list[dict[str, Any]]) -> tuple[dict[str, Any], dict[str, str]] | None:
    """Find the cluster/instance-wide summary row from pg_stat_statements."""
    for r in rows:
        item = str(r.get("metric_item") or "")
        fields = message_fields(r.get("message"))
        if item == "all_statements" or fields.get("queryid") == "all_statements":
            return r, fields
    return None


def statement_interval(
    older_rows: list[dict[str, Any]],
    newer_rows: list[dict[str, Any]],
    quantiles: Sequence[int] = (50, 95, 99),
) -> dict[str, Any] | None:
    """Difference two consecutive PostgreSQL statement collections into latency percentiles.

    pg_stat_statements tracks execution counts and cumulative execution time per query,
    but does not record an intra-query duration distribution. The latency percentiles
    computed here are calls-weighted percentiles of per-statement interval means
    (delta total_exec_time / delta calls) across queryids observed in both collections
    with a positive calls delta. The spread of execution times inside a single statement
    is not preserved by the extension and is therefore not represented here.

    Returns None if:
    * pg_stat_statements is reported as NOT_INSTALLED in either collection;
    * the 'all_statements' row is missing;
    * counters_since differ on 'all_statements' (indicating an extension reset or restart);
    * the calls delta on 'all_statements' is negative.
    """
    if not older_rows or not newer_rows:
        return None

    if is_not_installed(older_rows) or is_not_installed(newer_rows):
        return None

    older_all = _find_all_statements(older_rows)
    newer_all = _find_all_statements(newer_rows)
    if older_all is None or newer_all is None:
        return None

    older_all_row, older_all_fields = older_all
    newer_all_row, newer_all_fields = newer_all

    older_cs = older_all_fields.get("counters_since") or str(older_all_row.get("counters_since") or "")
    newer_cs = newer_all_fields.get("counters_since") or str(newer_all_row.get("counters_since") or "")
    if older_cs and newer_cs and older_cs != newer_cs:
        return None

    older_calls = as_float(older_all_fields.get("calls") or older_all_row.get("calls"))
    newer_calls = as_float(newer_all_fields.get("calls") or newer_all_row.get("calls"))
    if older_calls is None or newer_calls is None:
        return None

    delta_calls = newer_calls - older_calls
    if delta_calls < 0:
        return None

    older_time = as_float(
        older_all_fields.get("total_exec_time_ms") or older_all_row.get("total_exec_time_ms"),
        default=0.0,
    )
    newer_time = as_float(
        newer_all_fields.get("total_exec_time_ms") or newer_all_row.get("total_exec_time_ms"),
        default=0.0,
    )
    delta_time = (newer_time or 0.0) - (older_time or 0.0)
    if delta_time < 0:
        return None

    mean_ms: float | None = (delta_time / delta_calls) if delta_calls > 0 else None

    # Map individual statement rows by queryid
    def _statement_map(rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
        st_map: dict[str, dict[str, Any]] = {}
        for r in rows:
            item = str(r.get("metric_item") or "")
            fields = message_fields(r.get("message"))
            qid = fields.get("queryid") or (
                item.split(":", 1)[1] if item.startswith("queryid:") else None
            )
            if qid and qid != "all_statements" and item != "all_statements" and item != "pg_stat_statements":
                st_map[qid] = {
                    "calls": as_float(fields.get("calls") or r.get("calls"), default=0.0),
                    "total_exec_time_ms": as_float(
                        fields.get("total_exec_time_ms") or r.get("total_exec_time_ms"),
                        default=0.0,
                    ),
                }
        return st_map

    older_stmts = _statement_map(older_rows)
    newer_stmts = _statement_map(newer_rows)

    pairs: list[tuple[float, float]] = []
    common_qids = sorted(set(older_stmts) & set(newer_stmts))
    for qid in common_qids:
        s_old = older_stmts[qid]
        s_new = newer_stmts[qid]
        s_delta_calls = (s_new["calls"] or 0.0) - (s_old["calls"] or 0.0)
        if s_delta_calls > 0:
            s_delta_time = (s_new["total_exec_time_ms"] or 0.0) - (s_old["total_exec_time_ms"] or 0.0)
            if s_delta_time >= 0:
                stmt_mean = s_delta_time / s_delta_calls
                pairs.append((stmt_mean, s_delta_calls))

    pcts = weighted_percentiles(pairs, quantiles)

    older_epoch = _to_epoch(older_all_row.get("collected_at") or older_rows[0].get("collected_at"))
    newer_epoch = _to_epoch(newer_all_row.get("collected_at") or newer_rows[0].get("collected_at"))
    seconds = 0.0
    if older_epoch is not None and newer_epoch is not None:
        seconds = round(newer_epoch - older_epoch, 1)
        if seconds < 0:
            return None

    result: dict[str, Any] = {
        "seconds": seconds,
        "calls": int(delta_calls),
        "mean_ms": mean_ms,
        "statements": len(pairs),
        # How much of the interval the percentiles describe. A statement only in the newer
        # collection is left out - its counters run back to the reset, not to the older collection,
        # so counting it from zero could credit weeks of calls to minutes - and so are the
        # statements outside both top-25 lists. On the lab the one new statement was the slowest:
        # mean 19.93 ms over 5 calls against a p99 of 0.03 ms over the 3 calls covered.
        "covered_calls": int(sum(weight for _mean, weight in pairs)),
    }
    for q in quantiles:
        result[f"p{q}_ms"] = pcts.get(q)

    return result


def weighted_percentiles(
    pairs: Iterable[tuple[float, float]],
    quantiles: Sequence[int] = (50, 95, 99),
) -> dict[int, float | None]:
    """Calculate discrete weighted percentiles for pairs of (value, weight).

    Only pairs with weight > 0 are considered. The percentile for quantile q is the
    smallest value at which the cumulative weight REACHES (q / 100) * total_weight - the
    PERCENTILE_DISC definition: p99 is the value 99 % of the weight is at or below. With 900
    waits <= 1 ms, 90 <= 8 ms and 10 <= 64 ms, 990 of 1000 are <= 8 ms, so p99 is 8, not 64. If the sequence is empty or the total weight is non-positive, returns None for
    all requested quantiles.

    This matches the discrete percentile arithmetic computed server-side by SQL Server
    Query Store (e.g. PERCENTILE_DISC), ensuring the two implementations agree by construction.
    """
    cleaned: list[tuple[float, float]] = []
    for item in pairs:
        if not isinstance(item, (tuple, list)) or len(item) < 2:
            continue
        val, w = item[0], item[1]
        v_flt = as_float(val)
        w_flt = as_float(w)
        if v_flt is not None and w_flt is not None and w_flt > 0:
            cleaned.append((v_flt, w_flt))

    if not cleaned:
        return {q: None for q in quantiles}

    cleaned.sort(key=lambda item: item[0])
    total_weight = sum(w for _, w in cleaned)
    if total_weight <= 0:
        return {q: None for q in quantiles}

    result: dict[int, float | None] = {}
    for q in quantiles:
        target = (q / 100.0) * total_weight
        val: float | None = cleaned[-1][0]
        cum = 0.0
        for v, w in cleaned:
            cum += w
            # A tolerance, so 0.99 * 1000 computed as 989.9999999 still counts as reached.
            if cum >= target - 1e-9 * total_weight:
                val = v
                break
        result[q] = val
    return result
