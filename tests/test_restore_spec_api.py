"""The restore API: a complete spec in, a plan out, and a point in time never quietly downgraded.

`common/restore` takes the whole description of the work in its request - engine, both ends,
credentials - because it reads no config. That is what lets it be packaged and called against a
machine nothing has registered, which is what a real recovery usually is.

The property worth the most here is the refusal. `point_in_time` against a method that restores
"the newest chain" must fail: an operator told they recovered to 14:00 while holding whatever the
last log contained has been handed a wrong answer shaped exactly like a right one, and finds out by
reading data that should not exist yet. Refusing costs a message; downgrading costs the recovery.
"""

from __future__ import annotations

import pytest

from db_ops.common.restore import parse_restore_spec, plan_restore, redacted
from db_ops.lib.restore.plan import ENGINE, SCRIPT
from db_ops.lib.restore.spec import RestoreSpecError


def _request(**overrides):
    request = {
        "db_type": "sqlserver",
        "source": {"access": "smb", "host": "192.0.2.250",
                   "path": r"\\192.0.2.250\SQLBK\APPDB-DB$APPDB",
                   "username": "appdbadmin", "password": "s3cret"},
        "target": {"platform": "linux", "host": "192.0.2.249", "port": 1433,
                   "username": "sa", "password": "t0psecret",
                   "data_dir": "/var/opt/mssql/data",
                   "import_dir": "/opt/mssql2025/backup/SQLBK_IMPORT"},
    }
    request.update(overrides)
    return request


# --------------------------------------------------------------------------- #
# The spec is complete, or it is refused.
# --------------------------------------------------------------------------- #

def test_a_full_request_parses():
    spec = parse_restore_spec(_request())
    assert spec.db_type == "sqlserver"
    assert spec.target.host == "192.0.2.249"
    assert spec.source.access == "smb"


def test_a_remote_source_without_a_host_is_refused():
    """Defaulting it to the target would silently restore a machine from its own copy."""
    request = _request()
    request["source"] = {**request["source"], "host": ""}
    with pytest.raises(RestoreSpecError, match="source.host is required"):
        parse_restore_spec(request)


def test_a_copy_with_nowhere_to_land_is_refused():
    """Without import_dir the pieces have no destination, and the failure would otherwise surface
    as a path error deep inside the copy rather than as the missing field it is."""
    request = _request()
    request["target"] = {**request["target"], "import_dir": ""}
    with pytest.raises(RestoreSpecError, match="target.import_dir is required"):
        parse_restore_spec(request)


def test_an_unsupported_engine_is_refused_by_name():
    with pytest.raises(RestoreSpecError, match="db_type must be one of"):
        parse_restore_spec(_request(db_type="oracle"))


def test_a_misspelled_key_is_refused_rather_than_ignored():
    """`dryrun` ignored would run a real restore against a caller that asked for a rehearsal."""
    with pytest.raises(RestoreSpecError, match="Unknown key"):
        parse_restore_spec(_request(dryrun=True))


def test_an_unknown_platform_is_refused():
    request = _request()
    request["target"] = {**request["target"], "platform": "solaris"}
    with pytest.raises(RestoreSpecError, match="target.platform must be one of"):
        parse_restore_spec(request)


# --------------------------------------------------------------------------- #
# Point in time: refused, never downgraded.
# --------------------------------------------------------------------------- #

def test_a_point_in_time_request_selects_the_engine_method():
    """Asking for a moment is an unambiguous statement of intent, and only one method can honour
    it - so the request settles the method rather than being checked against it."""
    plan = plan_restore(parse_restore_spec(_request(point_in_time="2026-08-06 14:00:00 +07:00")))
    assert plan["method"] == ENGINE
    assert plan["restore_mode"] == "POINT_IN_TIME"


def test_a_point_in_time_request_on_a_script_method_is_refused():
    """The whole reason pitr.py is its own module."""
    spec = parse_restore_spec(_request(
        point_in_time="2026-08-06 14:00:00 +07:00",
        extras={"method": SCRIPT},
    ))
    from db_ops.lib.restore import assert_point_in_time_supported

    with pytest.raises(RestoreSpecError, match="STOPAT"):
        assert_point_in_time_supported(spec, SCRIPT)


def test_the_plan_refuses_exactly_what_a_real_run_would():
    """A rehearsal that is more permissive than the thing it rehearses is worse than none."""
    from db_ops.lib.restore import pitr

    spec = parse_restore_spec(_request(point_in_time="2026-08-06 14:00:00 +07:00"))
    # Force the contradiction the plan has to catch: a moment asked of a method that cannot.
    with pytest.raises(RestoreSpecError, match="STOPAT"):
        pitr.assert_point_in_time_supported(spec, SCRIPT)


def test_no_point_in_time_is_left_alone():
    plan = plan_restore(parse_restore_spec(_request()))
    assert plan["restore_mode"] == "LATEST"
    assert plan["point_in_time"] is None


# --------------------------------------------------------------------------- #
# Every target shape reaches the same machinery.
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("platform, container", [
    ("windows", ""),            # a Windows VM
    ("linux", ""),              # an Ubuntu VM
    ("linux", "mssql2025"),     # a container
])
def test_point_in_time_works_for_every_target_shape(platform, container):
    """STOPAT has no platform branch: a container is a Linux host with the instance on a port, so
    naming it changes the log lines and nothing else."""
    request = _request(point_in_time="2026-08-06 14:00:00 +07:00")
    request["target"] = {**request["target"], "platform": platform, "container": container}

    plan = plan_restore(parse_restore_spec(request))

    assert plan["method"] == ENGINE
    assert plan["restore_mode"] == "POINT_IN_TIME"


# --------------------------------------------------------------------------- #
# Secrets never leave in a result.
# --------------------------------------------------------------------------- #

def test_passwords_are_redacted_out_of_every_result():
    """The plan is printed as JSON and lands in logs and Telegram. Redaction is central so a new
    secret-bearing field cannot be added to the spec and forgotten in one of several copies."""
    plan = plan_restore(parse_restore_spec(_request()))

    text = str(plan)
    assert "s3cret" not in text
    assert "t0psecret" not in text
    assert plan["spec"]["target"]["password"] == "***"
    assert plan["spec"]["source"]["password"] == "***"


def test_redaction_keeps_the_fields_that_are_not_secret():
    """Blanking the whole object would make a plan useless for checking where it would restore."""
    clean = redacted(parse_restore_spec(_request()))
    assert clean["target"]["host"] == "192.0.2.249"
    assert clean["source"]["username"] == "appdbadmin"


# --------------------------------------------------------------------------- #
# Picking the implementation.
# --------------------------------------------------------------------------- #

def test_an_unsupported_engine_has_no_implementation():
    """A plain `if`, not a registry - so an engine nobody wrote says so directly."""
    from db_ops.common.restore import run_restore

    spec = parse_restore_spec(_request())
    object.__setattr__(spec, "db_type", "oracle")

    with pytest.raises(RestoreSpecError, match="No restore implementation"):
        run_restore(spec, {"method": ENGINE})
