from io import StringIO
import logging
from pathlib import Path

from db_ops.logging_ops import build_log_paths, log_event, setup_app_logger
from db_ops.logging_ops.handlers import DailyArchiveFileHandler
from db_ops.logging_ops.runtime_stdout import TeeStdout


class FakeTelegramConfig:
    enabled = False


class FakeConfig:
    def __init__(self, log_dir):
        self.app_name = "db_ops"
        self.log_dir = log_dir
        self.console_level = "INFO"
        self.file_level = "INFO"
        self.telegram = FakeTelegramConfig()


def test_tee_stdout_writes_common_log_format(tmp_path):
    stream = StringIO()
    log_path = tmp_path / "app_runtime.log"
    tee = TeeStdout(stream, log_path, app_name="app", stream_name="stdout")

    tee.write("hello\n")

    assert stream.getvalue() == "hello\n"
    lines = log_path.read_text(encoding="utf-8").splitlines()
    assert lines[0] == "DATE|LOGTYPE|APP|HOST|FUNCTION|TEXT"
    assert "|LOGGING|app|" in lines[1]
    assert "|stdout|hello" in lines[1]


def test_build_log_paths_uses_log_scope():
    logs_dir = Path("tools/db_ops/logs")
    main_log, runtime_log = build_log_paths(logs_dir, "sql_tasks")

    assert main_log == logs_dir / "sql_tasks.log"
    assert runtime_log == logs_dir / "sql_tasks_runtime.log"


def test_setup_app_logger_uses_scope_filename_not_app_identifiers(tmp_path):
    config = FakeConfig(tmp_path)

    logger = setup_app_logger(config, app_name="app_name_value", log_scope="sql_tasks", enable_console=False)
    log_event(logger, level="logging", message="hello")

    assert (tmp_path / "sql_tasks.log").exists()
    assert not (tmp_path / "app_name_value.log").exists()
    assert not (tmp_path / "app_name_value_runtime.log").exists()


def test_setup_app_logger_keeps_daily_archive_handler(tmp_path):
    config = FakeConfig(tmp_path)

    logger = setup_app_logger(config, app_name="metrics", log_scope="metrics", enable_console=False)

    assert any(isinstance(handler, DailyArchiveFileHandler) for handler in logger.handlers)
    assert not any(type(handler) is logging.FileHandler for handler in logger.handlers)
