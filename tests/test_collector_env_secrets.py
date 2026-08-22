"""A collector script that needs a passphrase must get it, and only from the secret store.

``collector_env`` is committed configuration, so it refuses any name that looks like a secret —
a passphrase written there would live in git forever. That rule is right, but it left
ORACLE_RESTORE_VALIDATION unable to do its job: the RMAN backups are password-encrypted, so
``restore database validate`` without the passphrase fails with ORA-19913 three seconds in, and
the metric reported CRITICAL on a database whose backups were provably restorable. A monitor that
cries wolf for eleven days is worse than no monitor.

``env_secrets`` closes that gap the way ``restore_config.json`` already does: config carries a
*ref*, the value is resolved from the encrypted store at run time. These tests hold both halves —
the value must arrive, and a missing ref must stop the run loudly rather than quietly hand the
script an empty passphrase and let it report a failure that is really a configuration mistake.
"""

from __future__ import annotations

import pytest

from db_ops.metrics import collector


class _Metric:
    metric_code = "ORACLE_RESTORE_VALIDATION"


class _Target:
    target_id = "CLOUD-203-0-113-188-ORA-1521"
    # A real MetricTarget always carries these, and _collector_env reads them to inject
    # DB_OPS_TARGET_HOST. A stub without them is a stub of something that cannot exist.
    ip = "203.0.113.188"
    cmd_access = {"enabled": True, "method": "ssh", "host": "203.0.113.188"}

    def __init__(self, metrics_config):
        self.metrics_config = metrics_config


def test_a_secret_ref_reaches_the_script_as_the_resolved_value():
    target = _Target({"env_secrets": {"BACKUP_ENCRYPTION_PASSWORD": "TOKEN_BACKUP_ENC"}})
    env = collector._collector_env(
        metric=_Metric(), target=target, secrets={"TOKEN_BACKUP_ENC": "s3kr3t-passphrase"},
    )
    assert env["BACKUP_ENCRYPTION_PASSWORD"] == "s3kr3t-passphrase"


def test_only_the_ref_is_ever_written_in_config_never_the_value():
    """The ref name must not leak into the environment as if it were the value — that would hand
    the script a wrong passphrase and produce a decryption failure nobody could explain."""
    target = _Target({"env_secrets": {"BACKUP_ENCRYPTION_PASSWORD": "TOKEN_BACKUP_ENC"}})
    env = collector._collector_env(
        metric=_Metric(), target=target, secrets={"TOKEN_BACKUP_ENC": "real-value"},
    )
    assert "TOKEN_BACKUP_ENC" not in env.values()


def test_a_ref_missing_from_the_store_stops_the_metric_instead_of_running_without_it():
    target = _Target({"env_secrets": {"BACKUP_ENCRYPTION_PASSWORD": "TOKEN_ABSENT"}})
    with pytest.raises(RuntimeError) as excinfo:
        collector._collector_env(metric=_Metric(), target=target, secrets={})
    message = str(excinfo.value)
    assert "TOKEN_ABSENT" in message
    assert "ORACLE_RESTORE_VALIDATION" in message


def test_collector_env_still_refuses_to_carry_a_secret_value_itself():
    """env_secrets exists so that this rule never has to be relaxed."""
    target = _Target({"collector_env": {"BACKUP_ENCRYPTION_PASSWORD": "written-in-git"}})
    with pytest.raises(RuntimeError) as excinfo:
        collector._collector_env(metric=_Metric(), target=target, secrets={})
    assert "env_secrets" in str(excinfo.value)


def test_a_per_metric_env_secret_overrides_the_target_wide_one():
    target = _Target({
        "env_secrets": {"BACKUP_ENCRYPTION_PASSWORD": "TOKEN_DEFAULT"},
        "metric_overrides": {
            "ORACLE_RESTORE_VALIDATION": {"env_secrets": {"BACKUP_ENCRYPTION_PASSWORD": "TOKEN_SPECIFIC"}},
        },
    })
    env = collector._collector_env(
        metric=_Metric(), target=target,
        secrets={"TOKEN_DEFAULT": "wrong-one", "TOKEN_SPECIFIC": "right-one"},
    )
    assert env["BACKUP_ENCRYPTION_PASSWORD"] == "right-one"
