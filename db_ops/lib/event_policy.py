from __future__ import annotations

import hashlib
import re
from typing import Any


CONNECT_FAILED_PATTERNS = (
    "timeout",
    "timed out",
    "connection timeout",
    "login timeout",
    "host unreachable",
    "network unreachable",
    "connection refused",
    "no valid connections",
    "cannot connect",
    "closed the connection",
)
AUTH_FAILED_PATTERNS = (
    "authentication failed",
    "auth failed",
    "access is denied",
    "negotiate authentication",
    "kerberos",
    "login failed",
)
PERMISSION_DENIED_PATTERNS = (
    "permission was denied",
    "permission denied",
    "execute permission",
    "not authorized",
)


def normalize_error_type(message: object) -> str:
    text = _normalize_text(message).lower()
    if not text:
        return "CHECK_FAILED"
    if "[winerror 2]" in text or "cannot find the file specified" in text:
        return "REMOTE_COMMAND_NOT_AVAILABLE"
    if any(pattern in text for pattern in AUTH_FAILED_PATTERNS):
        return "AUTH_FAILED"
    if any(pattern in text for pattern in CONNECT_FAILED_PATTERNS):
        return "CONNECT_FAILED"
    if any(pattern in text for pattern in PERMISSION_DENIED_PATTERNS):
        return "PERMISSION_DENIED"
    if "metric_data_invalid" in text or "data invalid" in text or "invalid metric data" in text:
        return "METRIC_DATA_INVALID"
    if text.startswith("sql execution failed") or "sql execution failed" in text:
        return "QUERY_FAILED"
    return "CHECK_FAILED"


#: ``error_type`` values that mean **the collector** failed, not the check it was running.
#: ``CHECK_FAILED`` is the odd one out and the reason this list exists: it marks a collector that
#: connected, ran, and did not like the answer — a finding about the target. The rest mark a
#: collector that never got an answer, which is a finding about the monitoring. Reports that treat
#: the two the same either hide an outage behind "WARNING" or report a threshold breach as a
#: broken collector; :meth:`MetricStore.fetch_metric_freshness` needs exactly this split.
COLLECTOR_FAILURE_ERROR_TYPES = (
    "AUTH_FAILED",
    "CONNECT_FAILED",
    "PERMISSION_DENIED",
    "QUERY_FAILED",
    "METRIC_DATA_INVALID",
    "REMOTE_COMMAND_NOT_AVAILABLE",
)


#: Which half of a collection attempt broke. The collector reaches the target, then runs
#: something on it, and the two failures mean different things to an operator: PHASE_CONNECT is
#: "the server did not answer" (the target may be down), PHASE_EXECUTE is "it answered, but the
#: check failed" (usually the monitoring, a permission or a query). Metrics grade them
#: separately via connection_error_severity / execution_error_severity.
PHASE_CONNECT = "connect"
PHASE_EXECUTE = "execute"
#: ``error_type`` values that can only come from never having reached the target.
CONNECT_PHASE_ERROR_TYPES = ("CONNECT_FAILED", "AUTH_FAILED")


def resolve_failure_phase(exc: BaseException) -> str:
    """Whether ``exc`` happened while connecting or while running the check.

    A raiser that *knows* says so by setting ``failure_phase`` on the exception — the SQL
    collector does, because it holds the connect and the execute apart itself, and that verdict
    is always better than reading the message. Everything else is classified from the message
    through :func:`normalize_error_type`, so an error raised deep inside a driver still lands on
    the right side. Unknown means PHASE_EXECUTE: a failure nobody could attribute must not be
    reported as "the server is unreachable", which is the louder and more alarming claim.
    """
    declared = str(getattr(exc, "failure_phase", "") or "").strip().lower()
    if declared in {PHASE_CONNECT, PHASE_EXECUTE}:
        return declared
    return PHASE_CONNECT if normalize_error_type(str(exc)) in CONNECT_PHASE_ERROR_TYPES else PHASE_EXECUTE


def normalize_error_signature(message: object) -> str:
    text = _normalize_text(message).lower()
    if not text:
        return "empty"
    text = re.sub(r"\b\d{1,3}(?:\.\d{1,3}){3}\b", "<ip>", text)
    text = re.sub(r"\b\d+\b", "<n>", text)
    text = re.sub(r"0x[0-9a-f]+", "<hex>", text)
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) > 180:
        digest = hashlib.sha1(text.encode("utf-8")).hexdigest()[:12]
        return f"{text[:160]}...#{digest}"
    return text


def report_event_code(*, collector_type: object, category: object, error_type: object, metric_code: object = "") -> str:
    collector = str(collector_type or "").strip().lower()
    normalized_category = str(category or "").strip().lower()
    normalized_error = str(error_type or "").strip().upper()
    normalized_metric = str(metric_code or "").strip().upper()
    if normalized_error == "PERMISSION_DENIED":
        return "MONITORING_PERMISSION_DENIED"
    if collector == "cmd" and normalized_category == "os":
        return "MONITORING_CMD_UNAVAILABLE"
    if collector == "sql" and normalized_error == "CONNECT_FAILED":
        return "MONITORING_SQL_UNAVAILABLE"
    if normalized_metric == "LOG_RECENT_CRITICAL" and normalized_error == "PERMISSION_DENIED":
        return "MONITORING_PERMISSION_DENIED"
    return "MONITORING_CHECK_FAILED"


def _normalize_text(message: object) -> str:
    if message is None:
        return ""
    if isinstance(message, bytes):
        message = message.decode("utf-8", errors="replace")
    return " ".join(str(message).replace("\r", " ").replace("\n", " ").split())
