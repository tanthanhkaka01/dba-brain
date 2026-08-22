"""Two defaults that named a real machine, and what they do now that they do not.

Both were the same mistake wearing different clothes: a value that belongs to one estate, written
into shared code as the fallback for when configuration is silent. Each leaked an address into the
source, and each handed every other operator a default pointing at a machine they cannot reach.

`DEFAULT_REPORT_BASE_URL` was `http://<a real host>:8080/report_dba/`. Anyone who installed this
without setting `report_base_url` got report links to someone else's server — links that resolve,
look right, and are wrong, which is worse than links that are absent.

`oracle_bridge.SECRET_ENV_VAR` was `TOKEN_<a real ip>_ORACLE_BRIDGE`. Beyond the address, it meant
the environment fallback ignored the ref the configuration actually named, so a second bridge on a
second host had no way to be given its own secret through the environment.

The rule both now follow is the one the rest of the tree already used: **a ref names its own
environment variable**, and an unconfigured value stays empty rather than becoming a wrong one.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from db_ops.common import data_sources
from db_ops.common.oracle_bridge import LegacyOracleError, resolve_secret


# --------------------------------------------------------------------------- #
# The report base URL
# --------------------------------------------------------------------------- #
def _reports_config(tmp_path: Path, payload: dict) -> Path:
    data_dir = tmp_path / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    (data_dir / "reports_config.json").write_text(json.dumps(payload), encoding="utf-8")
    return data_dir


def test_the_built_in_base_url_names_nobody() -> None:
    assert data_sources.DEFAULT_REPORT_BASE_URL == ""


def test_an_unconfigured_base_url_stays_empty(tmp_path: Path, monkeypatch) -> None:
    """Not "/" — a root-relative URL is a different claim, and one nobody made."""
    data_dir = _reports_config(tmp_path, {"reports": []})
    monkeypatch.setattr(data_sources, "DEFAULT_DATA_DIR", data_dir)

    assert data_sources.report_base_url() == ""


def test_a_configured_base_url_always_ends_in_one_slash(tmp_path: Path, monkeypatch) -> None:
    """Callers concatenate a page name onto it, so the separator has to be there exactly once."""
    data_dir = _reports_config(tmp_path, {"report_base_url": "http://reports.example.invalid/pages"})
    monkeypatch.setattr(data_sources, "DEFAULT_DATA_DIR", data_dir)

    assert data_sources.report_base_url() == "http://reports.example.invalid/pages/"


def test_a_trailing_slash_in_config_is_not_doubled(tmp_path: Path, monkeypatch) -> None:
    data_dir = _reports_config(tmp_path, {"report_base_url": "http://reports.example.invalid/pages/"})
    monkeypatch.setattr(data_sources, "DEFAULT_DATA_DIR", data_dir)

    assert data_sources.report_base_url() == "http://reports.example.invalid/pages/"


# --------------------------------------------------------------------------- #
# The Oracle bridge secret
# --------------------------------------------------------------------------- #
def test_the_secret_store_answers_first() -> None:
    secret = resolve_secret({"secret_ref": "BRIDGE_TOKEN_A"}, {"BRIDGE_TOKEN_A": "from-store"})

    assert secret == "from-store"


def test_the_ref_names_its_own_environment_variable(monkeypatch) -> None:
    """The convention the rest of db_ops already uses: refs double as env var names.

    This is what a second bridge on a second host needs — its own ref, its own variable.
    """
    monkeypatch.setenv("BRIDGE_TOKEN_B", "from-environment")

    assert resolve_secret({"secret_ref": "BRIDGE_TOKEN_B"}, {}) == "from-environment"


def test_two_bridges_do_not_share_one_variable(monkeypatch) -> None:
    """The old behaviour read one hardcoded name, so the second bridge could never be configured."""
    monkeypatch.setenv("BRIDGE_TOKEN_HOST_A", "secret-a")
    monkeypatch.setenv("BRIDGE_TOKEN_HOST_B", "secret-b")

    assert resolve_secret({"secret_ref": "BRIDGE_TOKEN_HOST_A"}, {}) == "secret-a"
    assert resolve_secret({"secret_ref": "BRIDGE_TOKEN_HOST_B"}, {}) == "secret-b"


def test_a_missing_secret_says_which_ref_it_wanted(monkeypatch) -> None:
    monkeypatch.delenv("BRIDGE_TOKEN_MISSING", raising=False)

    with pytest.raises(LegacyOracleError) as raised:
        resolve_secret({"secret_ref": "BRIDGE_TOKEN_MISSING"}, {})

    assert "BRIDGE_TOKEN_MISSING" in str(raised.value)


def test_config_that_names_no_ref_fails_saying_so() -> None:
    with pytest.raises(LegacyOracleError) as raised:
        resolve_secret({}, {"SOMETHING_ELSE": "value"})

    assert "secret_ref" in str(raised.value)
