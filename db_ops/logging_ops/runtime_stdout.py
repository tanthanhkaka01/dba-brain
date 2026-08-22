# logging_ops/runtime_stdout.py

from __future__ import annotations

import sys
import socket
import os
from datetime import datetime
from pathlib import Path
from collections.abc import Callable

from db_ops.logging_ops.handlers import archive_yesterday_if_missing, ensure_current_log_file
from db_ops.logging_ops.writer import LOG_SCOPE_ENV_VAR, build_log_paths
from db_ops.lib.paths import TOOL_ROOT


DEFAULT_RUNTIME_LOG_PATH = TOOL_ROOT / "logs" / "db_ops_runtime.log"


class TeeStdout:
    def __init__(
        self,
        stream,
        log_path: str | Path = DEFAULT_RUNTIME_LOG_PATH,
        *,
        app_name: str = "db_ops",
        stream_name: str = "stdout",
        sanitizer: Callable[[str], str] | None = None,
    ) -> None:
        self.stream = stream
        self.path = Path(log_path)
        self.app_name = app_name
        self.stream_name = stream_name
        self.sanitizer = sanitizer
        self.hostname = socket.gethostname()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        archive_yesterday_if_missing(self.path)
        ensure_current_log_file(self.path)

    def write(self, message: str) -> None:
        message = self.sanitizer(message) if self.sanitizer else message
        self.stream.write(message)

        if not message.strip():
            return

        archive_yesterday_if_missing(self.path)
        ensure_current_log_file(self.path)
        with self.path.open("a", encoding="utf-8") as file:
            timestamp = f"{datetime.now():%Y-%m-%d %H:%M:%S}"
            for line in message.splitlines():
                if line.strip():
                    file.write(f"{timestamp}|LOGGING|{self.app_name}|{self.hostname}|{self.stream_name}|{line}\n")

    def flush(self) -> None:
        self.stream.flush()


def patch_stdout(
    log_path: str | Path = DEFAULT_RUNTIME_LOG_PATH,
    *,
    app_name: str = "db_ops",
    sanitizer: Callable[[str], str] | None = None,
) -> None:
    path = Path(log_path)
    env_log_scope = os.getenv(LOG_SCOPE_ENV_VAR)
    if env_log_scope:
        path = build_log_paths(path.parent, env_log_scope)[1]
        app_name = env_log_scope
    if not isinstance(sys.stdout, TeeStdout) or sys.stdout.path != path:
        sys.stdout = TeeStdout(sys.stdout, path, app_name=app_name, stream_name="stdout", sanitizer=sanitizer)
    if not isinstance(sys.stderr, TeeStdout) or sys.stderr.path != path:
        sys.stderr = TeeStdout(sys.stderr, path, app_name=app_name, stream_name="stderr", sanitizer=sanitizer)
