"""A store that is coming back is not the daemon's failure, and it used to be treated as one.

On 2026-09-18 the container holding the runtime store was restarted. It answered again two seconds
later. The daemon did not: the scan raised ``FATAL 57P03`` — *the database system is starting up* —
the loop let it out, ``main`` logged one line and returned 1, and nothing restarted the process.
Nineteen hours of silence followed and candidate 0.18.0 was abandoned at hour 21.8.

The rule now lives in :mod:`db_ops.lib.store_outage`: a failure the store itself classifies as
transient is waited out, on a bounded budget, and everything else leaves the loop exactly as before.
These tests are that distinction — the codes, the budget, and the two things that must not change:
a real error still reaches a person, and an outage longer than the budget still ends the process.
"""

from __future__ import annotations

import pytest

from db_ops.lib import store_outage


class FakePgError(Exception):
    """pg8000 raises its errors carrying the server's response fields as a mapping."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__({"S": "FATAL", "C": code, "M": message})


# --------------------------------------------------------------------------- #
# Reading the store's own classification
# --------------------------------------------------------------------------- #
def test_the_code_that_ended_0_18_0_is_read_out_of_the_error():
    error = FakePgError("57P03", "the database system is starting up")

    assert store_outage.sqlstate(error) == "57P03"
    assert store_outage.is_transient(error)


def test_an_attribute_is_preferred_to_the_argument_mapping():
    """A driver that grows a `sqlstate` attribute should be read through it, not by shape."""

    class Driverish(Exception):
        sqlstate = "08006"

    assert store_outage.sqlstate(Driverish("connection failure")) == "08006"


@pytest.mark.parametrize("code", ["08000", "08003", "08006", "57P01", "57P02", "57P03", "53300"])
def test_every_connection_and_shutdown_code_is_waited_out(code):
    assert store_outage.is_transient(FakePgError(code, "…"))


@pytest.mark.parametrize("code", ["53000", "53100", "53200"])
def test_a_store_out_of_disk_or_memory_is_waited_out_too(code):
    """2026-09-27: a full disk under the store raised 53100 on a write, the daemon exited, and a PC
    node with nothing to restart it lost its soak - the store itself was back 62 minutes later."""
    assert store_outage.is_transient(FakePgError(code, 'could not extend file "base/1/2": No space left on device'))


def test_the_budget_outlasts_the_hour_a_person_took_to_free_that_disk():
    waiter = store_outage.OutageWaiter()
    error = FakePgError("53100", "No space left on device")
    waited = 0
    while waited < 62 * 60:
        wait = waiter.wait_for(error)
        assert wait is not None, f"gave up after {waited} s"
        waited += wait


@pytest.mark.parametrize("code", ["42P01", "42703", "23505", "28P01", "3D000"])
def test_a_definite_answer_is_never_retried(code):
    """A missing table, a wrong column, a duplicate key, a rejected password, a missing database.
    Retrying any of these only delays the report by the whole budget."""
    assert not store_outage.is_transient(FakePgError(code, "…"))


def test_a_code_outside_the_set_is_trusted_over_the_words_in_its_message():
    """The message of a permission error may still say 'connection'. The server sent a code, and
    the code is the answer — otherwise the text match quietly widens what gets retried."""
    assert not store_outage.is_transient(FakePgError("42501", "permission denied for connection"))


def test_a_socket_failure_carries_no_code_and_is_read_from_its_text():
    """A connection lost at the socket never reaches the server, so nothing sends a code back."""
    assert store_outage.is_transient(ConnectionResetError("connection reset by peer"))
    assert store_outage.is_transient(OSError("network error"))


def test_an_ordinary_bug_is_not_mistaken_for_an_outage():
    assert not store_outage.is_transient(KeyError("server_id"))
    assert not store_outage.is_transient(ValueError("expected an int"))


# --------------------------------------------------------------------------- #
# The budget
# --------------------------------------------------------------------------- #
def test_the_first_wait_is_short_because_the_outage_was_two_seconds_long():
    waiter = store_outage.OutageWaiter()

    assert waiter.wait_for(FakePgError("57P03", "starting up")) == 2


def test_the_waits_grow_and_are_capped():
    waiter = store_outage.OutageWaiter()
    error = FakePgError("57P03", "starting up")

    waits = [waiter.wait_for(error) for _ in range(6)]

    assert waits == [2, 4, 8, 16, 30, 30]


def test_a_definite_error_gives_no_wait_at_all():
    waiter = store_outage.OutageWaiter()

    assert waiter.wait_for(FakePgError("42P01", "relation does not exist")) is None


def test_an_outage_longer_than_the_budget_ends_the_process():
    """Retrying forever turns a store that has really gone away into a daemon that looks alive and
    schedules nothing — the same silence this module exists to prevent, one level up."""
    waiter = store_outage.OutageWaiter(budget_seconds=20)
    error = FakePgError("57P03", "starting up")

    waits = []
    while (wait := waiter.wait_for(error)) is not None:
        waits.append(wait)

    assert sum(waits) == 20
    assert waiter.wait_for(error) is None


def test_the_budget_is_per_outage_and_a_good_pass_restores_it():
    """Otherwise a daemon up for a month would give up on its first hiccup, having spent its
    budget one second at a time over weeks."""
    waiter = store_outage.OutageWaiter()
    error = FakePgError("57P03", "starting up")
    for _ in range(4):
        waiter.wait_for(error)

    waiter.recovered()

    assert waiter.waited_seconds == 0
    assert waiter.wait_for(error) == 2


# --------------------------------------------------------------------------- #
# The daemon's half: the wait is logged, because a silent wait reads as a stopped daemon
# --------------------------------------------------------------------------- #
def test_the_daemon_logs_every_wait_with_the_code_and_the_budget_left(capsys):
    from db_ops.jobs.daemon import wait_out_store_outage

    logged: list[dict] = []

    class Logger:
        pass

    waiter = store_outage.OutageWaiter()
    wait = wait_out_store_outage(
        FakePgError("57P03", "the database system is starting up"),
        waiter=waiter, logger=None)

    assert wait == 2
    warning = capsys.readouterr().err
    assert "57P03" in warning
    assert "retry 1 in 2s" in warning
    assert "598s of budget left" in warning
    assert logged == []


def test_the_daemon_lets_a_real_error_out_untouched():
    from db_ops.jobs.daemon import wait_out_store_outage

    assert wait_out_store_outage(
        FakePgError("42P01", 'relation "job_runs" does not exist'),
        waiter=store_outage.OutageWaiter(), logger=None) is None


def test_the_scan_loop_consults_the_waiter_and_reraises_what_it_will_not_wait_for():
    """The wiring itself: a loop that classified the error and then raised anyway would pass every
    test above while changing nothing."""
    import inspect

    from db_ops.jobs import daemon

    source = inspect.getsource(daemon.main)

    assert "store_waiter = store_outage.OutageWaiter()" in source
    assert "wait = wait_out_store_outage(exc, waiter=store_waiter, logger=logger)" in source
    assert "if wait is None:\n                    raise" in source
    assert "store_waiter.recovered()" in source


# --------------------------------------------------------------------------- #
# The half that was missing: the code is usually not on the exception a caller sees
# --------------------------------------------------------------------------- #
def test_a_code_one_link_down_the_chain_is_still_found():
    """The gap the 0.21.0 soak found, at the cost of 5 h 45 m of clock.

    Every store CONNECT failure reaches the daemon as ``PostgresStoreError("Could not connect to
    …: <driver error>")``, raised ``from`` the driver's exception. Reading only the top of the chain
    returned "" — so ``57P03``, the very code this module exists for, classified as permanent and
    the daemon exited on its first attempt instead of waiting. There was no log line to say so,
    because the waiter returns before it logs when it decides not to wait.
    """
    driver = FakePgError("57P03", "the database system is in recovery mode")
    try:
        raise RuntimeError("Could not connect to PostgreSQL store postgres@h:5433/db") from driver
    except RuntimeError as wrapped:
        assert store_outage.sqlstate(wrapped) == "57P03"
        assert store_outage.is_transient(wrapped)


def test_an_implicit_context_counts_as_well_as_an_explicit_cause():
    """A wrapper raised inside an `except` block chains through __context__ without `from`."""
    driver = FakePgError("08006", "connection failure")
    try:
        try:
            raise driver
        except FakePgError:
            # noqa is the subject, not an oversight: the missing `from` is what makes this an
            # implicit __context__ chain, which is the case being tested.
            raise RuntimeError("Could not connect to PostgreSQL store")  # noqa: B904
    except RuntimeError as wrapped:
        assert store_outage.sqlstate(wrapped) == "08006"
        assert store_outage.is_transient(wrapped)


def test_walking_the_chain_does_not_make_a_permanent_error_retryable():
    """The negative that matters. Reading deeper must not widen what counts as transient: a missing
    table wrapped in the same connect-shaped message is still a definite answer."""
    driver = FakePgError("42P01", 'relation "job_runs" does not exist')
    try:
        raise RuntimeError("Could not connect to PostgreSQL store postgres@h:5433/db") from driver
    except RuntimeError as wrapped:
        assert store_outage.sqlstate(wrapped) == "42P01"
        assert not store_outage.is_transient(wrapped)


def test_a_self_referential_chain_terminates():
    """`__context__` can point at the exception itself. Bounded by identity, not by depth."""
    error = ValueError("no code here")
    error.__context__ = error

    assert store_outage.sqlstate(error) == ""


def test_a_store_connect_that_timed_out_is_waited_out():
    """The 0.27.0 soak, 2026-10-06T08:40:14Z: one ten-second connect timeout ended the daemon.

    pg8000 wraps every socket failure while connecting in one sentence and raises it ``from`` the
    socket error, and the store wraps that again. The SQLSTATE walk finds no code anywhere in the
    chain - a socket that never connected has no server to send one - so the text decides, and the
    socket's own words ("timed out") are two links down where the text match does not read. The
    sentence itself is what has to be known. Measured on the node: sixteen hours dead for an outage
    the store had recovered from within minutes.
    """
    message = ("Can't create a connection to host 192.0.2.115 and port 5433 "
               "(timeout is 10 and source_address is None).")
    try:
        try:
            try:
                raise TimeoutError("timed out")
            except TimeoutError as socket_error:
                raise ConnectionError(message) from socket_error
        except ConnectionError as driver:
            raise RuntimeError(
                f"Could not connect to PostgreSQL store postgres@192.0.2.115:5433/db_ops: {driver}"
            ) from driver
    except RuntimeError as wrapped:
        assert store_outage.sqlstate(wrapped) == ""
        assert store_outage.is_transient(wrapped)
        assert store_outage.OutageWaiter().wait_for(wrapped) == 2


def test_the_other_wording_of_57P03_is_in_the_phrase_list():
    """Second-line defence, for a driver that carries no mapping at all. The server said "in
    recovery mode" on 2026-09-22; the list only had "starting up"."""
    assert store_outage.is_transient(Exception("FATAL: the database system is in recovery mode"))
