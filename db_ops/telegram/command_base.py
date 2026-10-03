"""The Telegram support commands: what every part shares - the command record, its error, the dispatch log, and the argument helpers.

Split out of ``telegram/command_processor.py`` on 2026-10-03 (Q11,
``audits/20261002_audit_typed_requests_and_errors.md`` section 5). ``command_processor`` re-exports every
name, so no import changes.
"""

from __future__ import annotations
from db_ops.lib import errors
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from db_ops.lib.config import load_config
from db_ops.lib.process_liveness import (  # noqa: F401 - re-exported, see above
    is_pid_alive as _is_pid_alive,
    is_windows_pid_alive as _is_windows_pid_alive,
    is_zombie as _is_zombie,
    process_start_marker,
    stop_process_tree,
)
from db_ops.lib.telegram_command_text import (  # noqa: F401 - re-exported, see above
    command_key_from_message,
    first_command_token,
    normalize_command_text,
    parse_command_message,
    split_with_verbatim_tail,
    render_command_line,
    split_command_tokens,
    strip_bot_username,
)
from db_ops.logging_ops import log_event, setup_app_logger
from db_ops.lib.paths import DEFAULT_DATA_DIR, REPO_ROOT, TOOL_ROOT  # noqa: F401 - one definition, see that module


DEFAULT_COMMANDS_PATH = DEFAULT_DATA_DIR / "telegram_support_commands.json"
# db_ops is a standalone repo root; keep REPO_ROOT as an alias so path resolution
# never escapes the project (was TOOL_ROOT.parents[1] under the old repo/tools/db_ops layout).
# A claim older than this is treated as abandoned (the owner was killed before it could mark
# the message done — e.g. the worker container was restarted mid-command). The message is closed
# and its sender told, never run a second time: see `_close_interrupted_command`.
CLAIM_STALE_SECONDS = 900

# Background CLI dispatch (e.g. /spbot_restore) writes a detailed trace here so a workflow
# that fails after "started" leaves an auditable record instead of silently stopping.
_DISPATCH_LOG_SCOPE = "telegram_dispatch"
_DISPATCH_LOGGERS: dict[str, Any] = {}


class TelegramCommandError(errors.DbOpsError, RuntimeError):
    kind = errors.KIND_REQUEST
    def __init__(self, message: str, *, exit_code: int = 1) -> None:
        super().__init__(message)
        self.exit_code = exit_code


@dataclass(frozen=True)
class SupportCommand:
    command_id: int
    command_text: str
    command_type: int
    reply_default: int
    reply_text: str
    is_group: int
    is_private: int
    need_file: int
    action_type: str = ""
    action_config: dict[str, Any] | None = None
    # Which cluster node handles this command: master | worker | all. Missing/empty
    # defaults to "worker" so legacy commands keep running on the worker as before.
    node_role: str = "worker"


def _describe_source(data: dict[str, Any]) -> str:
    """``xlsx (first sheet)`` / ``tab-delimited text (utf-16-le)`` — one line for the reply."""
    if str(data.get("source_format") or "") != "delimited":
        return "xlsx (first sheet)"
    from db_ops.lib import delimited_import

    return (f"{delimited_import.describe_delimiter(str(data.get('source_delimiter') or ''))} "
            f"text ({data.get('source_encoding') or 'utf-8'})")


def _format_argument_position(config: dict[str, Any]) -> int | None:
    """The 1-based position of a ``format`` parameter, or ``None`` when the command has none.

    Read from the command's own ``parameters`` list rather than hard-coded, so ``action_config``
    stays the single place that says which argument is which. A command that does not declare
    ``format`` keeps taking it from ``action_config`` — which is how ``/spbot_sql_to_xlsx``
    goes on producing exactly the file its name promises, with its argument order untouched.
    """
    for parameter in config.get("parameters") or []:
        if str((parameter or {}).get("name") or "").strip().lower() == "format":
            position = int((parameter or {}).get("position") or 0)
            return position or None
    return None


def _parameter_at_position(command: SupportCommand, position: int) -> dict[str, Any] | None:
    for parameter in (command.action_config or {}).get("parameters") or []:
        try:
            if int(parameter.get("position", 0)) == int(position):
                return dict(parameter)
        except (TypeError, ValueError):
            continue
    return None


def _dispatch_logger(config_path: str | Path) -> Any:
    """Return (and cache) a logger that records the background CLI dispatch trace to
    ``<log_dir>/telegram_dispatch.log``. Falls back to ``None`` (print-only) if the
    config cannot be loaded, so dispatch never fails just because logging is unavailable."""
    key = str(config_path or "")
    if key in _DISPATCH_LOGGERS:
        return _DISPATCH_LOGGERS[key]
    logger = None
    try:
        cfg = load_config(config_path) if config_path else load_config()
        logger = setup_app_logger(
            cfg,
            app_name=_DISPATCH_LOG_SCOPE,
            log_scope=_DISPATCH_LOG_SCOPE,
            enable_telegram_alerts=False,
            enable_console=False,
        )
    except Exception:  # noqa: BLE001 - logging must never break command dispatch.
        logger = None
    _DISPATCH_LOGGERS[key] = logger
    return logger


def _dispatch_log(logger: Any, message: str, *, level: str = "logging") -> None:
    """Emit one masked dispatch trace line to both the dispatch logger and stdout
    (routed to the telegram runtime log by patch_stdout). Secrets are masked first."""
    safe = mask_sensitive_text(message)
    if logger is not None:
        try:
            log_event(logger, level=level, message=safe)
        except Exception:  # noqa: BLE001
            pass
    print(safe, flush=True)


def _safe_values_text(values: dict[str, Any]) -> str:
    masked = mask_sensitive_value(values)
    return " ".join(f"{key}={masked.get(key)}" for key in sorted(masked))


def mask_sensitive_value(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: ("***" if sensitive_key(str(key)) else mask_sensitive_value(item)) for key, item in value.items()}
    if isinstance(value, list):
        masked: list[Any] = []
        redact_next = False
        for item in value:
            text = str(item)
            if redact_next:
                masked.append("***")
                redact_next = False
                continue
            masked.append(mask_sensitive_text(text))
            if text.lower() in {"-p", "--password", "--token", "--secret", "--key", "--key-base64", "--key_base64"}:
                redact_next = True
        return masked
    if isinstance(value, str):
        return mask_sensitive_text(value)
    return value


def mask_sensitive_text(text: str) -> str:
    masked = re.sub(r"(?i)(password|token|secret|pwd)\s*[:=]\s*[^,\s]+", r"\1=***", str(text))
    masked = re.sub(r"(?i)(-P\s+)(\"[^\"]+\"|'[^']+'|\S+)", r"\1***", masked)
    masked = re.sub(r"(?i)(--key(?:-base64|_base64)?[=\s]+)(\"[^\"]+\"|'[^']+'|\S+)", r"\1***", masked)
    # Oracle/SQL shapes a DB tool echoes back on failure, which the key=value rules above
    # do not cover: `sys/secret@TNS` in a connect string, and `IDENTIFIED BY "secret"`.
    masked = re.sub(r"(?i)\b([A-Za-z][\w$#]*)/(\"[^\"]+\"|'[^']+'|[^\s@/]+)(@\S+)", r"\1/***\3", masked)
    masked = re.sub(r"(?i)(identified\s+by\s+)(\"[^\"]+\"|'[^']+'|\S+)", r"\1***", masked)
    return masked


def secret_values(values: dict[str, Any] | None) -> list[str]:
    """The run's own secret values, for literal redaction out of an error message.

    Pattern masking only catches shapes it anticipates (``password=x``, ``-P x``,
    ``sys/x@tns``). Removing the actual value catches every shape, including a password a
    tool happened to print in plain prose — but only where the raw value is still known,
    which is the synchronous path. A background task stores its values already masked, so
    there is nothing to match there and pattern masking is the only line of defence.
    """
    return [
        str(value)
        for key, value in (values or {}).items()
        if sensitive_key(str(key)) and str(value or "").strip() not in ("", "-", "***")
    ]


def sensitive_key(key: str) -> bool:
    lowered = key.lower()
    return any(marker in lowered for marker in ("password", "token", "secret", "pwd"))
