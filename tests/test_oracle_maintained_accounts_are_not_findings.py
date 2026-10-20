"""Accounts Oracle creates and owns are not permission findings (1.146).

`DATABASE_USER_PERMISSIONS` on Oracle left out Oracle's own accounts by a list of names written for
releases before 12c. Every account added since was read as a customer account: on the `.251` 26ai
lab on 2026-10-15, `GGSHAREDCAP` (23ai) and `SYSBACKUP` (12c) came back WARNING - locked accounts
nobody granted anything, reported beside the accounts a DBA should look at. From 12c `dba_users`
says which accounts are Oracle's (`oracle_maintained`), so that variant reads the flag; 9i - 11g
have no such column and keep the list, unchanged.
"""

from __future__ import annotations

import json
from pathlib import Path
import re

import pytest

from conftest import present_copies

REPO_ROOT = Path(__file__).resolve().parents[1]
ORACLE = REPO_ROOT / "dbabrain" / "metrics" / "collectors" / "oracle"
MODERN = ORACLE / "061_oracle_user_permissions.sql"
OLDER = ORACLE / "061_oracle_user_permissions_11g.sql"
CATALOGUES = present_copies(
    "data/metric_definitions.json",
    "data/metric_definitions.example.json",
    "dbabrain/metrics/catalogue/metric_definitions.json",
)


def _code(path: Path) -> str:
    lines = [line for line in path.read_text(encoding="utf-8").splitlines()
             if not line.lstrip().startswith("--")]
    return re.sub(r"\s+", " ", " ".join(lines))


def test_from_12c_the_accounts_oracle_maintains_are_left_out_by_its_own_flag():
    code = _code(MODERN)
    assert "WHERE u.oracle_maintained = 'N'" in code
    assert "NOT IN ( 'SYS'" not in code


def test_before_12c_the_name_list_still_leaves_them_out():
    """The column does not exist there; a reference to it would fail every 11g collection."""
    code = _code(OLDER)
    assert "oracle_maintained" not in code
    assert "WHERE u.username NOT IN ( 'SYS','SYSTEM','DBSNMP'" in code


def test_both_variants_grade_and_report_alike():
    def body(path: Path) -> str:
        code = _code(path)
        return code[:code.index("FROM dba_users u")]

    assert body(MODERN) == body(OLDER)


@pytest.mark.parametrize("relative", CATALOGUES)
def test_each_oracle_version_runs_the_variant_its_dictionary_supports(relative):
    data = json.loads((REPO_ROOT / relative).read_text(encoding="utf-8"))
    metric = next(m for m in data["metrics"] if m["metric_code"] == "DATABASE_USER_PERMISSIONS")
    oracle = {v["name"]: v for v in metric["variants"] if v["db_type"] == "oracle"}
    assert oracle["oracle_12c_plus"]["file"] == "oracle/061_oracle_user_permissions.sql"
    assert oracle["oracle_12c_plus"]["min_major_version"] == 12
    assert "max_major_version" not in oracle["oracle_12c_plus"]
    assert oracle["oracle_9i_to_11g"]["file"] == "oracle/061_oracle_user_permissions_11g.sql"
    assert (oracle["oracle_9i_to_11g"]["min_major_version"], oracle["oracle_9i_to_11g"]["max_major_version"]) == (9, 11)
