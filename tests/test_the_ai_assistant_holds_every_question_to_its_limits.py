"""The AI assistant's settings and limits (0.34.0, 1.256 / 1.258).

The operator, 2026-10-31: *like petweb - set for the whole system and per Telegram user, a JSON
config, every field mandatory, $10 a month for the system and $5 for a user by default; refuse
duplicates; refuse questions that come too fast; a user is off unless switched on in the list.*
Money is the one thing a wrong default spends without asking, so every rule is held here without a
store or a network: a missing field is refused by name, a price is computed from the four token
counts, and a question is refused before it costs anything.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from dbabrain.lib import ai_assist as A

NOW = datetime(2026, 10, 31, 6, 0, tzinfo=timezone.utc)
ZONE = timezone(timedelta(hours=7))


def _settings(**changes):
    document = A.shipped_document()
    document.update(changes)
    document["users"] = [A.shipped_user("100", "Op", active=True)]
    return A.parse_ai_assist(document)


def _decide(settings, *, system=None, user=None, recent=None):
    return A.decide(settings, settings.user("100"), system_used=system or A.Used(), user_used=user or A.Used(),
                    recent=recent or A.Recent(), now=NOW, period=A.periods(NOW, ZONE))


def test_the_shipped_file_parses_and_its_defaults_are_the_operators():
    settings = _settings()
    assert settings.budget_usd_month == 10.0
    assert settings.user("100").budget_usd_month == 5.0
    assert settings.model == "claude-sonnet-5-5"
    assert settings.tools_mode == "read_only"
    assert settings.enabled is False


def test_a_missing_field_is_refused_by_name():
    document = A.shipped_document()
    del document["cooldown_seconds"]
    with pytest.raises(A.AiAssistConfigError, match="cooldown_seconds"):
        A.parse_ai_assist(document)


def test_a_user_row_missing_a_field_is_refused_by_name():
    document = A.shipped_document()
    row = A.shipped_user("1", "x", active=True)
    del row["budget_usd_month"]
    document["users"] = [row]
    with pytest.raises(A.AiAssistConfigError, match=r"users\[0\]\.budget_usd_month"):
        A.parse_ai_assist(document)


def test_a_user_is_off_unless_the_list_says_active_true():
    assert A.shipped_user("1", "x", active=False)["active"] is False
    spec = next(item for item in A.USER_FIELDS if item.name == "active")
    assert spec.shipped is False, "an upgrade that fills a missing 'active' must not switch anybody on"


def test_upgrade_fills_missing_fields_and_never_switches_a_user_on():
    document = {"enabled": True, "users": [{"telegram_user_id": "5", "name": "x"}]}
    written = A.state_ai_assist(document)
    assert "cooldown_seconds" in written and "users[0].active" in written
    assert document["users"][0]["active"] is False
    assert document["enabled"] is True, "what a node states is left as it is"
    A.parse_ai_assist(document)


def test_a_model_without_a_price_is_refused_so_no_limit_is_held_blind():
    with pytest.raises(A.AiAssistConfigError, match="no row in prices"):
        _settings(model="claude-unknown-9")


def test_a_user_listed_twice_is_refused():
    document = A.shipped_document()
    document["users"] = [A.shipped_user("1", "a", active=True), A.shipped_user("1", "b", active=False)]
    with pytest.raises(A.AiAssistConfigError, match="listed twice"):
        A.parse_ai_assist(document)


def test_a_call_is_priced_from_its_four_token_counts():
    prices = _settings().prices
    cost, known = A.cost_of("claude-sonnet-5-5", A.Usage(input=1_000_000, output=100_000, cache_read=1_000_000,
                                                        cache_write=100_000), prices)
    assert known
    assert cost == pytest.approx(2.0 + 1.0 + 0.2 + 0.25)


def test_a_long_prompt_is_priced_at_the_long_rate():
    prices = _settings().prices
    short, _ = A.cost_of("claude-haiku-5-5", A.Usage(input=50_000, output=1_000), prices)
    long, _ = A.cost_of("claude-haiku-5-5", A.Usage(input=150_000, output=1_000), prices)
    assert short == pytest.approx((50_000 * 0.1 + 1_000 * 0.5) / 1e6)
    assert long == pytest.approx((150_000 * 0.5 + 1_000 * 2.5) / 1e6)


def test_a_served_model_with_no_price_is_priced_at_the_dearest_row_and_said():
    cost, known = A.cost_of("claude-mystery", A.Usage(output=1_000_000), _settings().prices)
    assert not known
    assert cost == pytest.approx(20.0)


def test_a_question_inside_every_limit_goes_with_its_caps():
    decision = _decide(_settings())
    assert decision.ok
    assert decision.cost_cap == pytest.approx(0.5)
    assert decision.tokens <= 300_000


def test_the_users_month_is_held_before_the_systems():
    decision = _decide(_settings(), user=A.Used(month_cost=4.9999))
    assert not decision.ok and decision.key == "budget_month" and decision.level == "user"
    assert decision.until == A.periods(NOW, ZONE).next_month


def test_the_systems_month_refuses_every_user():
    decision = _decide(_settings(), system=A.Used(month_cost=10.0))
    assert not decision.ok and decision.level == "system" and decision.key == "budget_month"


def test_what_is_left_of_a_budget_caps_the_question():
    decision = _decide(_settings(), user=A.Used(month_cost=4.9))
    assert decision.ok
    assert decision.cost_cap == pytest.approx(0.1)


def test_a_duplicate_is_not_run_twice():
    decision = _decide(_settings(), recent=A.Recent(duplicate_of=7))
    assert not decision.ok and decision.key == "duplicate"


def test_a_question_inside_the_cooldown_waits():
    decision = _decide(_settings(cooldown_seconds=30), recent=A.Recent(last_started_at=NOW - timedelta(seconds=10)))
    assert not decision.ok and decision.key == "cooldown"
    assert decision.until == NOW + timedelta(seconds=20)


def test_too_many_questions_in_the_window_are_refused():
    decision = _decide(_settings(rate_window_max_requests=3), recent=A.Recent(window_requests=3))
    assert not decision.ok and decision.key == "rate"


def test_the_days_question_count_is_held_at_both_levels():
    settings = _settings(requests_day=2)
    assert _decide(settings, system=A.Used(day_requests=2)).key == "requests_day"


def test_the_day_and_the_month_are_the_config_timezones():
    period = A.periods(datetime(2026, 10, 31, 18, 30, tzinfo=timezone.utc), ZONE)
    # 18:30 UTC on the 31st is 01:30 on 1 November at +07:00: a new day and a new month.
    assert period.day_start == datetime(2026, 10, 31, 17, 0, tzinfo=timezone.utc)
    assert period.month_start == datetime(2026, 10, 31, 17, 0, tzinfo=timezone.utc)
    assert period.next_month == datetime(2026, 11, 30, 17, 0, tzinfo=timezone.utc)


def test_the_duplicate_key_ignores_case_and_spacing_but_not_the_user():
    assert A.question_fingerprint("1", "Backup  status?") == A.question_fingerprint("1", "backup status?")
    assert A.question_fingerprint("1", "x") != A.question_fingerprint("2", "x")
