"""Who can reach an instance, on the page that is supposed to say so.

`server-metrics.html` could show that a server was healthy while nobody in the estate could
connect to it. The 2026-08-10 migration onto 192.0.2.11 restored 13 databases whose 55 users
all carried the *source* instance's SIDs, so every one of them resolved to no login at all — and
the only way anyone found out was querying `sys.database_principals` by hand. An orphaned user is
invisible to every other section: the database is ONLINE, its size is normal, its backups are
fine.

Two metrics feed the section because SQL Server keeps the answer in two scopes.
`SECURITY_SERVER_PRINCIPALS` lists the logins — it exists precisely because
`SECURITY_LOGIN_HEALTH` is exception-based and so can never enumerate the healthy ones.
`DATABASE_USER_PERMISSIONS` lists the users.

Most of what is pinned here is parsing, and none of it is incidental: both metrics separate their
fields with ` | ` and their list members with `,`, and the shared message parser reads a value to
the next comma. Every bug this file covers was found rendering the real 192.0.2.248 rows.
"""

from db_ops.reports.server_report import build_access


def _login(name, message, value="SQL_LOGIN"):
    return {"metric_code": "SECURITY_SERVER_PRINCIPALS", "metric_item": name,
            "metric_value": value, "status": "OK", "message": message}


def _user(item, message, value="SQL_USER", status="OK"):
    return {"metric_code": "DATABASE_USER_PERMISSIONS", "metric_item": item,
            "metric_value": value, "status": status, "message": message}


ROLES_MSG = ("type=SQL_LOGIN, disabled=no, default_db=master, created=2023-11-23, "
             "password_last_set=2023-11-23, password_age_days=992, check_policy=off, "
             "server_roles=[sysadmin], server_perms=[] | HIGH_PRIVILEGE")


# --------------------------------------------------------------------------------------
# The thing the section exists for
# --------------------------------------------------------------------------------------

def test_an_orphaned_user_is_named_and_sorted_first():
    """It survives a restore because it lives in the database, but its SID belongs to the instance
    the backup came from. Nothing else on the page can show this."""
    out = build_access([
        _user("APP\\normal_user", "login=normal_user | roles=[db_datareader]"),
        _user("PPG\\Hieu", "login=<orphaned/none> | roles=[db_datareader,db_ddladmin] | HIGH_PRIVILEGE"),
    ])

    assert out["databaseUsers"][0]["name"] == "Hieu"
    assert out["databaseUsers"][0]["orphaned"] is True
    assert out["databaseUsers"][0]["login"] == ""
    assert out["summary"]["orphanedUsers"] == 1


def test_a_user_with_a_login_is_not_reported_as_orphaned():
    out = build_access([_user("APP\\dba_user", "login=dba_user | roles=[db_owner]")])

    assert out["databaseUsers"][0]["orphaned"] is False
    assert out["databaseUsers"][0]["login"] == "dba_user"


# --------------------------------------------------------------------------------------
# Parsing: every case below rendered wrongly against the real rows
# --------------------------------------------------------------------------------------

def test_a_role_list_is_not_truncated_at_its_first_comma():
    """`roles=[db_datareader,db_datawriter,db_ddladmin]` arrived as `[db_datareader` through the
    shared parser, so a user holding db_ddladmin rendered as a plain reader — the one row an
    operator would stop on, shown as the one kind that is harmless."""
    out = build_access([
        _user("PPG\\Hieu", "login=<orphaned/none> | roles=[db_datareader,db_datawriter,db_ddladmin]"),
    ])

    assert out["databaseUsers"][0]["roles"] == ["db_datareader", "db_datawriter", "db_ddladmin"]


def test_a_field_after_a_pipe_is_still_read():
    """The shared parser reads a value to the next COMMA and `re.findall` resumes after the match,
    so `login=` ate the rest of the line and `roles=` was never seen at all."""
    out = build_access([_user("APP\\u", "login=u | roles=[db_owner] | HIGH_PRIVILEGE")])

    assert out["databaseUsers"][0]["login"] == "u"
    assert out["databaseUsers"][0]["roles"] == ["db_owner"]


def test_the_high_privilege_marker_does_not_leak_into_the_permission_list():
    """`server_perms=[] | HIGH_PRIVILEGE` produced a permission literally called
    `| HIGH_PRIVILEGE` on every privileged login."""
    out = build_access([_login("dba", ROLES_MSG)])

    assert out["logins"][0]["permissions"] == []
    assert out["logins"][0]["roles"] == ["sysadmin"]
    assert out["logins"][0]["high"] is True


def test_a_database_name_containing_a_backslash_still_splits_correctly():
    """metric_item is `<database>\\<user>`, so the split has to be from the right."""
    out = build_access([_user("odd\\name\\the_user", "login=x | roles=[]")])

    assert out["databaseUsers"][0]["database"] == "odd\\name"
    assert out["databaseUsers"][0]["name"] == "the_user"


def test_a_windows_login_has_no_password_age_rather_than_zero():
    """Zero would sort it to the top of a table ordered by staleness, which is the opposite of
    true: its password is the domain's business."""
    out = build_access([
        _login("DOM\\svc", "type=WINDOWS_LOGIN, disabled=no, default_db=master, "
                           "created=2022-05-25, server_roles=[], server_perms=[]",
               value="WINDOWS_LOGIN"),
    ])

    assert out["logins"][0]["passwordAgeDays"] is None


# --------------------------------------------------------------------------------------
# Judgement
# --------------------------------------------------------------------------------------

def test_privilege_is_recognised_from_the_roles_even_without_the_marker():
    """So a bundle collected by an older metric build, which set no HIGH_PRIVILEGE marker, still
    sorts and colours correctly."""
    out = build_access([
        _login("a", "type=SQL_LOGIN, disabled=no, server_roles=[securityadmin], server_perms=[]"),
        _login("b", "type=SQL_LOGIN, disabled=no, server_roles=[public], server_perms=[]"),
    ])

    by_name = {r["name"]: r for r in out["logins"]}
    assert by_name["a"]["high"] is True
    assert by_name["b"]["high"] is False
    assert out["logins"][0]["name"] == "a"          # privileged first


def test_a_disabled_login_is_counted_but_not_treated_as_a_risk():
    out = build_access([
        _login("old", "type=SQL_LOGIN, disabled=yes, server_roles=[], server_perms=[]"),
    ])

    assert out["logins"][0]["disabled"] is True
    assert out["logins"][0]["high"] is False
    assert out["summary"]["disabledLogins"] == 1


def test_a_failed_collection_row_is_not_rendered_as_a_principal():
    """The metrics store a failure as a row of the same code with no item."""
    out = build_access([
        {"metric_code": "SECURITY_SERVER_PRINCIPALS", "metric_item": "", "message": "boom"},
        {"metric_code": "DATABASE_USER_PERMISSIONS", "metric_item": None, "message": "boom"},
    ])

    assert out["logins"] == [] and out["databaseUsers"] == []


def test_unrelated_metrics_are_ignored():
    out = build_access([{"metric_code": "OS_CPU_USAGE", "metric_item": "cpu_usage",
                         "message": "value=12"}])

    assert out["summary"]["logins"] == 0 and out["summary"]["databaseUsers"] == 0


def test_an_instance_with_nothing_collected_yields_an_empty_section():
    """The template hides the block entirely rather than drawing two empty tables."""
    out = build_access([])

    assert out["logins"] == [] and out["databaseUsers"] == []
    assert out["summary"]["databases"] == 0
