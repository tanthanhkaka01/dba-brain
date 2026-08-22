"""Every ``db_ops.common.cli`` command answers in the **one response shape**, on stdout.

The input half of the contract is held by ``test_common_cli_json_contract.py``. This is the
output half, and until 2026-08-16 it was held by nothing — which is why it drifted:
``docs/13_common.md`` states without qualification that every command prints
``success`` / ``operation`` / ``message`` / ``error`` / ``data`` / ``metrics``, and when this file
was written **23 of 43 did not**: 21 answered in an ad-hoc ``{"ok": …}`` and 2 in no JSON at
all.

What that costs a caller is specific, not stylistic:

* ``success`` and ``ok`` are two spellings of one fact, so no caller can write one result handler;
* ``operation`` is absent from the non-conforming ones, so a stored or forwarded answer is no
  longer self-describing once it is separated from the command line that produced it;
* two commands printed **no JSON at all**, and one of those printed a human line *in front of*
  its JSON, which is worse — stdout parses as neither.

**What is measured, and why it cannot be faked.** Each command is handed a payload that is
syntactically valid — so the shared request parser accepts it and the *handler's own* answer path
runs — and safe, so nothing is reached and nothing is written. The assertion is about the shape of
whatever it prints, not about whether it succeeded: a command that refuses is still answering.

An earlier attempt sent every command ``{}`` and asserted on the refusal. That is the trap this
file exists to avoid: ``{}`` is not universally harmless (``check-secret`` reads it as "check every
secret in the store" and went to the network; ``inventory-summary`` wrote a file into
``runtime/reports``), and a shared-parser refusal would have reported the contract as kept by
commands whose own answers are still the wrong shape.

``NOT_YET_ENVELOPE`` is a **shrinking baseline**, like ``REMAINING`` in
``test_app_common_imports.py``: the test fails if the set grows *and* if an entry has been fixed,
so converting a command means deleting its line here in the same commit.
"""

from __future__ import annotations

import json

import pytest

from db_ops.common import cli
from tests.test_common_cli_json_contract import ALL_COMMANDS


#: The six keys `db_ops/lib/response.py` builds, always present even when empty.
ENVELOPE_KEYS = frozenset({"success", "operation", "message", "error", "data", "metrics"})

#: What each command is sent. ``{}`` is valid JSON and satisfies no command's own validation, so
#: almost all of them answer from their refusal path without touching anything. The exceptions are
#: named with the reason, because "an empty request is harmless" is exactly the assumption that
#: made the first version of this file hang and write a file:
SAFE_REQUEST: dict[str, dict] = {
    #: `{}` means *check every secret in the store* — a real pass over the estate, over the
    #: network. A ref that cannot exist selects one secret and resolves nothing.
    "check-secret": {"refs": ["__NO_SUCH_SECRET_REF__"]},
    #: `{}` renders the real inventory and **writes** a dated summary into runtime/reports.
    #: An inventory path that does not exist fails on the read instead.
    "inventory-summary": {"inventory": "__no_such_inventory__.json"},
}


def _payload(command: str) -> str:
    return json.dumps(SAFE_REQUEST.get(command, {}))


#: Commands whose answer is not yet the envelope. **Empty**: all 43 answer in it.
#:
#: It took five passes over three days, and the order was the point — each group was chosen for
#: what it would cost to get wrong, not for how many commands it moved:
#:
#: 1. `add-sql` / `metric-toggle`, because the Telegram app could not call them at all until they
#:    stopped reporting failure as an `ERROR:` line on stderr with exit 2;
#: 2. `list-targets`, `inventory-summary`, `check-credentials` — the three a program could not
#:    consume at all, one of which printed prose *in front of* its JSON;
#: 3. the twelve **gate commands** plus the three file-transfer ones, in one edit each, because
#:    each family answers through a single handler;
#: 4. the four independent ad-hoc handlers, which nothing in the tree reads;
#: 5. `check-secret`, `queue-telegram-message` (with its client), and last `run-sql` — last
#:    because `sql_tasks`, `telegram` and every operator read its answer, so it converted in one
#:    commit with all four of its callers and with `lib/common_cli.run_ok`, the transitional
#:    reader that existed only for this shape and is now deleted.
#:
#: **Two commands keep something that looks like an exception and is not.** `run-cmd` passes
#: through the *remote* command's exit code — `2` from a remote `grep` means "no match", not
#: "db_ops failed" — and `run-sql` / `run-cmd` still answer in `txt` / `csv` / `xml` / `xlsx` /
#: `raw` when the **request asks for one**, which is the contract working: the rendering is chosen
#: inside the request object, so a config file can carry it.
NOT_YET_ENVELOPE: frozenset[str] = frozenset()


def _answer(command: str, capsys) -> tuple[str, object]:
    """Run the command and return ``(raw_stdout, parsed_or_None)``."""
    cli.main([command, _payload(command)])
    text = capsys.readouterr().out.strip()
    try:
        return text, json.loads(text)
    except ValueError:
        return text, None


@pytest.mark.parametrize(
    "command", [c for c in ALL_COMMANDS if c not in NOT_YET_ENVELOPE])
def test_a_command_answers_in_the_response_envelope(command: str, capsys) -> None:
    text, parsed = _answer(command, capsys)

    assert parsed is not None, (
        f"{command} printed something that is not JSON: {text[:200]!r}. The exit code says "
        "whether the process ran; the response says whether the work succeeded, and a caller "
        "must not have to read stderr or parse prose to find out."
    )
    assert isinstance(parsed, dict), f"{command} printed a JSON {type(parsed).__name__}, not an object."
    missing = sorted(ENVELOPE_KEYS - set(parsed))
    assert not missing, (
        f"{command} is missing {missing} from its response. Build the answer with "
        "db_ops.lib.response.ok()/fail() and print it with response.emit(), so every caller "
        "reads one shape. Do not add it to NOT_YET_ENVELOPE: that list only shrinks."
    )


@pytest.mark.parametrize("command", sorted(NOT_YET_ENVELOPE))
def test_a_baseline_command_is_still_unconverted(command: str, capsys) -> None:
    """The half that makes the list shrink: a converted command must leave this file.

    Without it the baseline would quietly become an allowlist — the same way an exemption nobody
    checks becomes decoration.
    """
    _text, parsed = _answer(command, capsys)
    is_envelope = isinstance(parsed, dict) and ENVELOPE_KEYS <= set(parsed)
    assert not is_envelope, (
        f"{command} now answers in the envelope — delete it from NOT_YET_ENVELOPE."
    )


def test_the_baseline_names_only_commands_that_exist() -> None:
    unknown = sorted(NOT_YET_ENVELOPE - set(ALL_COMMANDS))
    assert not unknown, f"NOT_YET_ENVELOPE names commands the dispatcher does not have: {unknown}"


def test_nothing_is_printed_in_front_of_the_json() -> None:
    """One object on stdout, and nothing else.

    ``inventory-summary`` used to print ``Wrote <file>`` and *then* its JSON, so stdout parsed as
    neither — the library function it calls was writing progress to the same stream the contract
    reserves for the answer. A human line belongs in ``message``, which is what the envelope has
    a field for.
    """
    offenders = []
    for command in ALL_COMMANDS:
        import io
        from contextlib import redirect_stdout

        buffer = io.StringIO()
        with redirect_stdout(buffer):
            cli.main([command, _payload(command)])
        text = buffer.getvalue().strip()
        if not text:
            continue
        try:
            json.loads(text)
        except ValueError:
            if text.lstrip().startswith(("{", "[")):
                offenders.append(f"{command}: JSON with something appended")
            elif "{" in text:
                offenders.append(f"{command}: {text.split('{', 1)[0].strip()[:60]!r} before the JSON")
    assert not offenders, (
        "stdout carries the answer and nothing else; these mix prose into it: " + "; ".join(offenders)
    )
