"""Tests for resolve_config_path() priority chain and per-app config selection."""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from db_ops.config import (
    APP_CONFIG_ENV_VARS,
    APP_CONFIG_NAMES,
    DEFAULT_CONFIG_PATH,
    resolve_config_path,
)
from conftest import shipped_config


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _write_config(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('{"app_name": "test"}', encoding="utf-8")


# ---------------------------------------------------------------------------
# Priority 1: explicit CLI argument always wins
# ---------------------------------------------------------------------------

def test_resolve_returns_cli_path_when_given(tmp_path, capsys):
    explicit = tmp_path / "my_config.json"
    _write_config(explicit)
    result = resolve_config_path("metrics", str(explicit))
    assert result == explicit
    captured = capsys.readouterr()
    assert "source=cli" in captured.err


def test_resolve_cli_path_does_not_require_file_to_exist(tmp_path):
    """CLI override is trusted as-is; existence check is deferred to load_config."""
    nonexistent = tmp_path / "missing.json"
    result = resolve_config_path("metrics", str(nonexistent))
    assert result == nonexistent


def test_resolve_cli_overrides_env_var(tmp_path, monkeypatch, capsys):
    explicit = tmp_path / "cli.json"
    env_path = tmp_path / "env.json"
    _write_config(explicit)
    _write_config(env_path)
    monkeypatch.setenv(APP_CONFIG_ENV_VARS["metrics"], str(env_path))
    result = resolve_config_path("metrics", str(explicit))
    assert result == explicit
    assert "source=cli" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# Priority 2: environment variable
# ---------------------------------------------------------------------------

def test_resolve_uses_env_var_when_no_cli(tmp_path, monkeypatch, capsys):
    env_path = tmp_path / "env_config.json"
    _write_config(env_path)
    monkeypatch.setenv(APP_CONFIG_ENV_VARS["metrics"], str(env_path))
    result = resolve_config_path("metrics", None)
    assert result == env_path
    assert "env:" in capsys.readouterr().err


def test_resolve_env_var_overrides_app_specific_file(tmp_path, monkeypatch, capsys):
    env_path = tmp_path / "env_config.json"
    _write_config(env_path)
    # Place an app-specific file next to DEFAULT_CONFIG_PATH — it should lose to env.
    app_file = DEFAULT_CONFIG_PATH.parent / APP_CONFIG_NAMES["metrics"]
    created = False
    if not app_file.exists():
        _write_config(app_file)
        created = True
    try:
        monkeypatch.setenv(APP_CONFIG_ENV_VARS["metrics"], str(env_path))
        result = resolve_config_path("metrics", None)
        assert result == env_path
    finally:
        if created:
            app_file.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# Priority 3: app-specific config file (next to shared config)
# ---------------------------------------------------------------------------

def test_resolve_finds_app_specific_file_next_to_shared(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv(APP_CONFIG_ENV_VARS["metrics"], raising=False)
    app_file = DEFAULT_CONFIG_PATH.parent / APP_CONFIG_NAMES["metrics"]
    created = False
    if not app_file.exists():
        _write_config(app_file)
        created = True
    try:
        result = resolve_config_path("metrics", None)
        assert result == app_file
        assert "app_config" in capsys.readouterr().err
    finally:
        if created:
            app_file.unlink(missing_ok=True)


def test_resolve_finds_app_specific_file_in_cwd(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv(APP_CONFIG_ENV_VARS["jobs"], raising=False)
    # Ensure the file does NOT exist next to shared config.
    shared_candidate = DEFAULT_CONFIG_PATH.parent / APP_CONFIG_NAMES["jobs"]
    assert not shared_candidate.exists(), (
        f"Cannot test cwd fallback while {shared_candidate} exists in repo; "
        "remove it or run this test from a clean directory."
    )
    cwd_file = tmp_path / APP_CONFIG_NAMES["jobs"]
    _write_config(cwd_file)
    monkeypatch.chdir(tmp_path)
    result = resolve_config_path("jobs", None)
    assert result == cwd_file
    assert "app_config_cwd" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# Priority 4: shared config.json fallback
# ---------------------------------------------------------------------------

def test_resolve_falls_back_to_shared_config(monkeypatch, capsys):
    monkeypatch.delenv(APP_CONFIG_ENV_VARS["sla"], raising=False)
    app_file = DEFAULT_CONFIG_PATH.parent / APP_CONFIG_NAMES["sla"]
    # Only run this part of the test if the app-specific file is absent.
    if app_file.exists():
        pytest.skip(f"{app_file} exists; shared-fallback test cannot run while it is present.")
    result = resolve_config_path("sla", None)
    assert result == DEFAULT_CONFIG_PATH
    assert "shared_fallback" in capsys.readouterr().err


def test_resolve_shared_fallback_for_unknown_app(monkeypatch, capsys):
    result = resolve_config_path("unknown_app", None)
    assert result == DEFAULT_CONFIG_PATH
    assert "shared_fallback" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# All known apps have entries in both dicts
# ---------------------------------------------------------------------------

def test_all_apps_have_config_name_and_env_var():
    # The full set: `APP_CONFIG_NAMES` is the product's map and stays complete in every build,
    # the same way `cli.APPS` does. What must agree is the two tables with each other.
    expected_apps = {
        "backup_restore",
        "jobs",
        "metrics",
        "reports",
        "sla",
        "sql_tasks",
        "sre",
        "telegram",
    }
    assert set(APP_CONFIG_NAMES.keys()) == expected_apps
    assert set(APP_CONFIG_ENV_VARS.keys()) == expected_apps


def test_app_config_names_follow_naming_convention():
    # sre uses data/sre_config.json (dedicated config file, not the default pattern)
    exceptions = {"sre": "data/sre_config.json"}
    for app_name, filename in APP_CONFIG_NAMES.items():
        if app_name in exceptions:
            assert filename == exceptions[app_name], (
                f"Expected {exceptions[app_name]} for {app_name} but got {filename}"
            )
        else:
            assert filename == f"config.{app_name}.json", (
                f"Expected config.{app_name}.json but got {filename}"
            )


def test_env_var_names_follow_naming_convention():
    for app_name, env_var in APP_CONFIG_ENV_VARS.items():
        expected = f"DB_OPS_{app_name.upper()}_CONFIG"
        assert env_var == expected, f"Expected {expected} but got {env_var}"


# ---------------------------------------------------------------------------
# No cross-app CLI imports (import boundary enforcement)
# ---------------------------------------------------------------------------

def test_no_direct_import_of_reports_from_telegram():
    """telegram.metrics_reports must call reports via subprocess, not import it."""
    import ast
    import importlib.util

    spec = importlib.util.find_spec("db_ops.telegram.metrics_reports")
    assert spec is not None
    source = Path(spec.origin).read_text(encoding="utf-8")
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            names = (
                [alias.name for alias in node.names]
                if isinstance(node, ast.Import)
                else ([node.module] if node.module else [])
            )
            for name in names:
                assert not (name or "").startswith("db_ops.reports"), (
                    f"telegram.metrics_reports must not import from db_ops.reports directly; found: {name}"
                )


def test_no_direct_import_of_telegram_from_sql_tasks():
    """sql_tasks.runner must not import telegram modules directly.

    Skipped where `sql_tasks` is not in the distribution. The rule it enforces is about the source
    repository, and a boundary cannot be crossed by software that is not there — but the check must
    not *fail* in that tree, because a red test for an absent component says nothing true.
    """
    import ast
    import importlib.util

    try:
        spec = importlib.util.find_spec("db_ops.sql_tasks.runner")
    except ModuleNotFoundError:
        spec = None
    if spec is None:
        pytest.skip("sql_tasks is not in this distribution")
    source = Path(spec.origin).read_text(encoding="utf-8")
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            names = (
                [alias.name for alias in node.names]
                if isinstance(node, ast.Import)
                else ([node.module] if node.module else [])
            )
            for name in names:
                assert not (name or "").startswith("db_ops.telegram"), (
                    f"sql_tasks.runner must not import from db_ops.telegram directly; found: {name}"
                )


# ---------------------------------------------------------------------------
# Telegram file paths resolve for configs that do not sit at the tool root
# ---------------------------------------------------------------------------

def test_nested_config_finds_telegram_settings_via_tool_root_fallback(tmp_path):
    """A config living under data/ must still find shipped_config("telegram_config.json").

    Regression: base_dir is the config's own folder, so data/restore_config.json resolved
    the path to data/data/telegram_config.json, found nothing, and silently produced empty
    telegram settings — the reason that file used to restate the whole telegram block inline.
    """
    from db_ops.config import load_config, resolve_telegram_config_path

    root = tmp_path
    data = root / "data"
    data.mkdir()
    (data / "telegram_config.json").write_text(
        '{"enabled": true, "groups_file": "data/telegram_groups.json"}',
        encoding="utf-8",
    )
    (data / "telegram_groups.json").write_text(
        '{"level_chat_map": {"critical": "-100200"}}', encoding="utf-8"
    )
    nested = data / "restore_config.json"
    nested.write_text('{"app_name": "nested"}', encoding="utf-8")

    import db_ops.config as config_module

    original = config_module.TOOL_ROOT
    config_module.TOOL_ROOT = root
    try:
        config = load_config(nested)
        assert config.telegram.enabled is True
        assert config.telegram.level_chat_map.get("critical") == "-100200"
        # Reads and update_offset writes must agree on one file.
        assert resolve_telegram_config_path(nested) == (data / "telegram_config.json").resolve()
    finally:
        config_module.TOOL_ROOT = original


def test_config_at_tool_root_still_uses_its_own_folder_first(tmp_path):
    """The fallback must not shadow a path that already resolves next to the config."""
    from db_ops.config import load_config

    root = tmp_path
    data = root / "data"
    data.mkdir()
    (data / "telegram_config.json").write_text(
        '{"enabled": true, "level_chat_map": {"error": "-100300"}}', encoding="utf-8"
    )
    main = root / "config.json"
    main.write_text('{"app_name": "root"}', encoding="utf-8")

    import db_ops.config as config_module

    original = config_module.TOOL_ROOT
    config_module.TOOL_ROOT = root
    try:
        assert load_config(main).telegram.level_chat_map == {"error": "-100300"}
    finally:
        config_module.TOOL_ROOT = original


# ---------------------------------------------------------------------------
# The routing table: one map, no second allow-list
# ---------------------------------------------------------------------------
def _telegram_root(tmp_path, settings: str, groups: str = "") -> "Path":
    data = tmp_path / "data"
    data.mkdir()
    (data / "telegram_config.json").write_text(settings, encoding="utf-8")
    if groups:
        (data / "telegram_groups.json").write_text(groups, encoding="utf-8")
    (tmp_path / "config.json").write_text('{"app_name": "root"}', encoding="utf-8")
    return tmp_path


def _load_at(root):
    from db_ops.config import load_config
    import db_ops.config as config_module

    original = config_module.TOOL_ROOT
    config_module.TOOL_ROOT = root
    try:
        return load_config(root / "config.json")
    finally:
        config_module.TOOL_ROOT = original


def test_settings_file_overrides_and_extends_the_groups_file(tmp_path):
    """A private DM has a positive chat_id so it cannot be declared as a group — the settings
    file is where such a route lives, and it wins over the groups file."""
    root = _telegram_root(
        tmp_path,
        '{"enabled": true, "groups_file": "data/telegram_groups.json",'
        ' "level_chat_map": {"private": "100000001", "error": "-999"}}',
        '{"telegram_groups": [{"group_id": "-100", "notify_level": "error", "status": "active"}]}',
    )
    level_chat_map = _load_at(root).telegram.level_chat_map
    assert level_chat_map["private"] == "100000001"  # only in the settings file
    assert level_chat_map["error"] == "-999"  # settings file wins over the groups file


def test_the_old_groups_spelling_is_still_read(tmp_path):
    """A settings file written before the rename must keep its routes, not silently lose
    them — a dropped route is a muted level, and muting is exactly what must never be silent."""
    root = _telegram_root(tmp_path, '{"enabled": true, "groups": {"private": "100000001"}}')
    assert _load_at(root).telegram.level_chat_map == {"private": "100000001"}


def test_a_blank_chat_is_not_a_route(tmp_path):
    """Clearing a chat is how a level is muted now, so a blank value must not become a key."""
    root = _telegram_root(
        tmp_path, '{"enabled": true, "level_chat_map": {"error": "-1", "sre": "", "sql": "  "}}'
    )
    level_chat_map = _load_at(root).telegram.level_chat_map
    assert level_chat_map == {"error": "-1"}
    assert "sre" not in level_chat_map


def test_an_obsolete_alert_levels_key_no_longer_gates_anything(tmp_path):
    """It used to be a second allow-list every level name had to appear in; a typo there
    ("pritvate") muted a level with no error anywhere. The map alone decides now."""
    root = _telegram_root(
        tmp_path,
        '{"enabled": true, "alert_levels": ["warning"], "level_chat_map": {"error": "-1"}}',
    )
    telegram = _load_at(root).telegram
    assert not hasattr(telegram, "alert_levels")
    assert telegram.level_chat_map == {"error": "-1"}
