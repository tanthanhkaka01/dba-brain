"""Why `telegram route` refuses the JSON-request shapes instead of looking them up.

Every `common.cli` command takes one JSON object — inline, `@file`, or `-`. `route` does not: it
is a config lookup that answers before this app sets up logging, so a caller can parse its single
line of stdout. An operator reaching for the contract they use everywhere else typed
`route '{"level":"critical"}'`, and the command looked up a notify level literally named
`{"level":"critical"}`, found none, and answered `chat_id: ""`.

That answer is indistinguishable from genuinely broken routing, which is what makes it expensive:
the routing was fine. It was written down as a trap in the 0.17.0 sheet *before* that run started
and still caught nobody during it — a note two screens up does not stop anyone typing. A refusal
does.
"""

import pytest

from db_ops.telegram import cli


@pytest.mark.parametrize("argument", [
    '{"level": "critical"}',
    '{"level":"critical"}',
    '[{"level": "critical"}]',
    "@data/route_request.json",
    "-",
])
def test_a_json_request_is_refused_rather_than_looked_up_as_a_level(argument, capsys):
    assert cli.main(["route", argument]) == 2
    captured = capsys.readouterr()
    assert captured.out == ""                      # nothing a caller could parse as an answer
    assert "takes a notify level" in captured.err


def test_the_refusal_names_the_form_that_works(capsys):
    """A refusal that only says no costs a second round trip to the docs."""
    cli.main(["route", '{"level": "critical"}'])

    message = capsys.readouterr().err
    assert "route <level>" in message
    assert "critical" in message and "groups" in message


def test_an_ordinary_level_is_still_answered(monkeypatch, capsys):
    """The lookup itself is untouched, including a level nobody configured: an unmapped level
    legitimately answers a blank chat, and that is not what this refusal is about."""
    from db_ops.telegram import routing

    monkeypatch.setattr(routing, "telegram_settings",
                        lambda: (True, {"critical": "-100"}, list(routing.STANDARD_LEVELS)))

    assert cli.main(["route", "critical"]) == 0
    assert '"chat_id": "-100"' in capsys.readouterr().out


def test_a_level_that_merely_starts_with_a_letter_is_not_mistaken_for_a_request(monkeypatch, capsys):
    """Only the three shapes of the JSON contract are refused. A deployment defines its own
    levels — `sla` is one — and none of them may be caught by this."""
    from db_ops.telegram import routing

    monkeypatch.setattr(routing, "telegram_settings",
                        lambda: (True, {"sla": "-200"}, [*routing.STANDARD_LEVELS, "sla"]))

    assert cli.main(["route", "sla"]) == 0
    assert '"chat_id": "-200"' in capsys.readouterr().out
