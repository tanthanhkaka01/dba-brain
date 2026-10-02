"""A request's `rules` can raise an operation's confirmation cost, never lower it (review 0.25.0, F11.4).

`rules` arrives with the request, so any caller of `common.cli` - a script, an agent, a compromised
app command - could send `"confirmations": 0` and skip the ladder.
"""

from __future__ import annotations

from db_ops.common import confirm


def test_zero_confirmations_in_the_request_do_not_lower_the_shipped_cost():
    shipped = confirm.load_operation("host-restart")
    effective = confirm.rules_for({"rules": {"level": 1, "confirmations": 0, "challenge": ""}},
                                  "host-restart")

    assert effective["confirmations"] == shipped["confirmations"]
    assert effective["level"] == shipped["level"]
    assert effective["challenge"] == shipped["challenge"]


def test_a_request_can_still_ask_for_more():
    shipped = confirm.load_operation("host-restart")
    effective = confirm.rules_for(
        {"rules": {"level": 100, "confirmations": shipped["confirmations"] + 1, "challenge": "target_id",
                   "effects": ["extra effect"]}}, "host-restart")

    assert effective["confirmations"] == shipped["confirmations"] + 1
    assert "extra effect" in effective["effects"]
