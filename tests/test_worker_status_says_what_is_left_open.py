"""`worker-status` ends with the worker's secret hygiene (0.42.0, 1.413 - plan S6).

The plan of 2026-11-20 closes what a login on the worker host could read: the passphrase in the
container's configuration, private keys open to any login or with no passphrase, copies of `data/`
left by deploys, old images, sshd password logins. Each can slip back with one careless deploy or
one key copied in by hand, and nothing would say so. The section reads them by name, mode and count
- never a value - and a check that could not run is not reported as passed.
"""

from __future__ import annotations

from dbabrain.control import worker_status


def test_every_check_is_said_and_the_verdict_counts_what_is_open():
    listing = ("passphrase in docker inspect|ok|not there\n"
               "keys that open with no passphrase|BAD|2 (seal-ssh-key)\n"
               "sshd password logins|ok|off\n")
    lines = worker_status.report_secret_hygiene(listing)

    assert lines[0].startswith("OK ") and lines[1].startswith("BAD") and "seal-ssh-key" in lines[1]
    assert lines[-1] == "1 of 3 need attention"


def test_a_host_that_printed_nothing_is_not_called_closed():
    assert "could not be read" in worker_status.report_secret_hygiene("")[0]


def test_the_checks_read_names_and_modes_never_a_value():
    script = worker_status.secret_hygiene_script("/opt/dbabrain", "dbabrain")

    assert "cut -d= -f1" in script, "the container's environment is read by name only"
    assert "ssh-keygen -y -P ''" in script and ">/dev/null" in script, "a key is tried, its output dropped"
    assert "cat \"$k\"" not in script and "printenv" not in script
    assert script.startswith("r=/opt/dbabrain; c=dbabrain;")


def test_a_match_block_that_lets_passwords_back_in_keeps_the_check_open():
    """2026-11-21: passwords off for every source but the container's network. A check that read only the
    first PasswordAuthentication said `off` - the exception has to stay visible until it is gone."""
    script = worker_status.secret_hygiene_script("/opt/dbabrain", "dbabrain")

    assert 'tolower($1)=="match"' in script and "off, except:" in script
