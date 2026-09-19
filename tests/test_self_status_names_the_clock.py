"""`self-status` has to say which clock the node's schedules are read against.

Asked for on 2026-09-16, reading a `/spbot_self_status` reply from a node that had been moved from
`UTC` to `Asia/Shanghai` on purpose. The reply named the version, the host, the role, the store and
two uptimes — and nowhere said which zone the node was on.

It is the one identity field that changes what the node *does* rather than what it is: every
``time_window.from_hour`` is a **local** hour read against this setting, so moving that estate by
eight hours moved 21 heavy overnight collections into the working day, or out of it. A reader of
`self-status` could not tell which, and the value was already computed twice elsewhere — `db
timezone` prints it and `runtime_nodes` stores it at every daemon start.

The `+08` that *was* visible came from `format_display_text` stamping an offset onto the two uptime
timestamps. That is why this is not "the information was already there": an offset on a timestamp
says what that timestamp means, and says nothing about what `from_hour: 1` means.
"""

from __future__ import annotations

from db_ops.common.self_status import _timezone_line, collect, render


def test_the_line_carries_the_zone_the_offset_and_the_abbreviation() -> None:
    """Three things, because each answers a different question: the name is what can be written
    back into a config file, the offset is what a reader converts a timestamp with, and the
    abbreviation is what people say."""
    line = _timezone_line({"timezone": "Asia/Shanghai", "utc_offset": "+08",
                           "tz_abbreviation": "CST"})

    assert line == "timezone  : Asia/Shanghai  +08 (CST)"


def test_an_abbreviation_that_only_repeats_the_offset_is_left_out() -> None:
    """Most zones have no abbreviation of their own and Python answers with the offset again, so
    joining them naively reads `+07 (+07)` — noise in the place a fact should be."""
    line = _timezone_line({"timezone": "Asia/Ho_Chi_Minh", "utc_offset": "+07",
                           "tz_abbreviation": "+07"})

    assert line == "timezone  : Asia/Ho_Chi_Minh  +07"


def test_a_node_that_cannot_answer_says_unknown_rather_than_nothing() -> None:
    """A missing line reads as "no timezone problem here". A named unknown reads as what it is."""
    assert _timezone_line({}) == "timezone  : unknown"


def test_the_facts_carry_the_clock(tmp_path) -> None:
    facts = collect(tool_root=tmp_path, version="0.0.0")

    assert facts["timezone"], "the zone is part of what this installation IS"
    assert facts["utc_offset"]


def test_the_rendered_report_names_the_clock_beside_the_role(tmp_path) -> None:
    """Beside `node_role`, because they are the same kind of fact: both are how this node was
    configured to behave, and both were read off the wrong thing before now."""
    lines = render(collect(tool_root=tmp_path, version="0.0.0")).splitlines()

    role = next(i for i, line in enumerate(lines) if line.startswith("node_role"))
    assert lines[role + 1].startswith("timezone  :")
