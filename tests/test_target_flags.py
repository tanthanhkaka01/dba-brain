from db_ops.lib.target_flags import is_alerts_enabled, is_metrics_enabled, is_reports_enabled, is_target_enabled


def test_target_flags_default_from_enabled_only():
    target = {"enabled": True}

    assert is_target_enabled(target) is True
    assert is_metrics_enabled(target) is True
    assert is_reports_enabled(target) is True
    assert is_alerts_enabled(target) is True


def test_metrics_enabled_false_cascades_to_reports_and_alerts_when_missing():
    target = {"enabled": True, "metrics": {"enabled": False}}

    assert is_metrics_enabled(target) is False
    assert is_reports_enabled(target) is False
    assert is_alerts_enabled(target) is False


def test_reports_and_alerts_can_be_disabled_independently():
    report_disabled = {"enabled": True, "metrics": {"enabled": True}, "reports": {"enabled": False}}
    alert_disabled = {"enabled": True, "metrics": {"enabled": True}, "reports": {"enabled": True}, "alerts": {"enabled": False}}

    assert is_metrics_enabled(report_disabled) is True
    assert is_reports_enabled(report_disabled) is False
    assert is_alerts_enabled(report_disabled) is False
    assert is_reports_enabled(alert_disabled) is True
    assert is_alerts_enabled(alert_disabled) is False


def test_reports_enabled_can_override_metrics_disabled():
    target = {"enabled": True, "metrics": {"enabled": False}, "reports": {"enabled": True}}

    assert is_metrics_enabled(target) is False
    assert is_reports_enabled(target) is True
    assert is_alerts_enabled(target) is True
