from __future__ import annotations

import logging
import os
from pathlib import Path

from db_ops.config import DbOpsConfig
from db_ops.levels import LEVEL_TO_PYTHON, normalize_level
from db_ops.logging_ops.formatter import LOG_HEADER, format_function_message
from db_ops.logging_ops.handlers import DailyArchiveFileHandler, HostNameFilter

LOG_SCOPE_ENV_VAR = "DB_OPS_LOG_SCOPE"


def build_log_paths(logs_dir: Path, log_scope: str) -> tuple[Path, Path]:
    clean_scope = validate_log_scope(log_scope)
    return logs_dir / f"{clean_scope}.log", logs_dir / f"{clean_scope}_runtime.log"


def validate_log_scope(log_scope: str | None) -> str:
    clean_scope = str(log_scope or "").strip()
    if not clean_scope:
        raise RuntimeError("log_scope is required for db_ops logging.")
    invalid_chars = {"/", "\\", ":", "*", "?", '"', "<", ">", "|"}
    if any(char in clean_scope for char in invalid_chars):
        raise RuntimeError(f"log_scope contains invalid filename character(s): {clean_scope}")
    return clean_scope

def setup_app_logger(
    config: DbOpsConfig,
    *,
    app_name: str | None = None,
    log_scope: str | None = None,
    enable_telegram_alerts: bool = True,
    enable_console: bool = True,
) -> logging.Logger:
    logger_name = app_name or config.app_name
    resolved_log_scope = validate_log_scope(log_scope or os.getenv(LOG_SCOPE_ENV_VAR) or logger_name)
    logger = logging.getLogger(logger_name)
    logger.setLevel(logging.DEBUG)
    logger.handlers.clear()
    logger.propagate = False

    config.log_dir.mkdir(parents=True, exist_ok=True)

    formatter = logging.Formatter(
        fmt="%(asctime)s|%(logtype)s|%(name)s|%(hostname)s|%(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    if enable_console:
        console = logging.StreamHandler()
        console.setLevel(parse_python_level(config.console_level))
        console.setFormatter(formatter)
        console.addFilter(HostNameFilter())
        logger.addHandler(console)

    main_log_path, _runtime_log_path = build_log_paths(config.log_dir, resolved_log_scope)
    main_handler = DailyArchiveFileHandler(
        main_log_path,
        encoding="utf-8",
    )
    ensure_log_header(main_log_path)
    main_handler.setLevel(parse_python_level(config.file_level))
    main_handler.setFormatter(formatter)
    main_handler.addFilter(HostNameFilter())
    logger.addHandler(main_handler)

    error_log_path = config.log_dir / "errors.log"
    error_handler = DailyArchiveFileHandler(
        error_log_path,
        encoding="utf-8",
    )
    ensure_log_header(error_log_path)
    error_handler.setLevel(logging.ERROR)
    error_handler.setFormatter(formatter)
    error_handler.addFilter(HostNameFilter())
    logger.addHandler(error_handler)

    return logger


def log_event(logger: logging.Logger, *, level: str, message: str) -> None:
    logical_level = normalize_level(level)
    logger.log(LEVEL_TO_PYTHON[logical_level], message, extra={"logtype": logical_level.upper()})


def log_function_call(logger: logging.Logger, *, function_name: str, text: str = "") -> None:
    log_event(logger, level="logging", message=format_function_message(function_name, text))


def log_function_error(logger: logging.Logger, *, function_name: str, error_text: str) -> None:
    log_event(logger, level="error", message=format_function_message(function_name, error_text))


def log_function_warning(logger: logging.Logger, *, function_name: str, warning_text: str = "") -> None:
    log_event(logger, level="warning", message=format_function_message(function_name, warning_text))


def log_function_critical(logger: logging.Logger, *, function_name: str, critical_text: str = "") -> None:
    log_event(logger, level="critical", message=format_function_message(function_name, critical_text))


def ensure_log_header(path: Path) -> None:
    if path.exists() and path.stat().st_size > 0:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as file:
        file.write(f"{LOG_HEADER}\n")


def parse_python_level(level_name: str) -> int:
    return int(getattr(logging, level_name.upper(), logging.INFO))
