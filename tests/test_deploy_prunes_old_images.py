"""A deploy that never removes what it replaced is a disk leak with a release cadence.

`start_daemon` ends in `docker load`, which adds `db_ops:<version>` and repoints `db_ops:latest`.
Nothing removed the version it superseded. Measured on the worker on 2026-08-18: **383 `db_ops`
tags and 526 dangling images**, on a host whose root volume had already been extended once from
293 GB to 589 GB. Deploy frequency is what turned it from a one-off into a daily cost — 2.85.32
through 2.85.49 is seventeen versions in two days — because each build changes the layer holding
the project source, so every version carries its own delta even where the base layers are shared.

Two things this must not do, and both are tests below:

- **Never remove `latest`.** It is what the running container was created from; removing the tag
  the daemon runs on is how a cleanup becomes an outage.
- **Keep enough versions to roll back.** A bad release is rolled back by starting the one before
  it, and that only works while it is still tagged.

The prune is deliberately best-effort (`check=False`): by the time it runs the daemon is up and
verified, and a deploy that worked must not be reported as failed because the tidying afterwards
did not.
"""

import pytest

from db_ops.control.deploy import KEEP_IMAGE_VERSIONS, _prune_old_images


class _FakeClient:
    """Records the shell it is handed, the way the other deploy tests do."""

    def __init__(self):
        self.commands: list[str] = []


@pytest.fixture
def run_calls(monkeypatch):
    calls: list[tuple[str, bool]] = []

    def fake_ssh_run(client, command, *, check=True, **_kwargs):
        calls.append((command, check))
        return 0

    monkeypatch.setattr("db_ops.control.deploy.ssh_run", fake_ssh_run)
    return calls


def test_the_prune_runs_and_is_allowed_to_fail(run_calls):
    _prune_old_images(_FakeClient())

    assert len(run_calls) == 1
    command, check = run_calls[0]
    assert check is False, "a deploy that worked must not fail on its own cleanup"
    assert "docker rmi" in command and "docker image prune -f" in command


def test_latest_is_excluded_from_every_list_the_prune_builds(run_calls):
    """It is the tag the running container was created from."""
    _prune_old_images(_FakeClient())
    command = run_calls[0][0]

    # Both the "what to keep" list and the "what to consider removing" list filter it out.
    assert command.count("grep -v '^latest$'") == 2


def test_the_newest_versions_are_kept_so_a_rollback_needs_no_rebuild(run_calls):
    _prune_old_images(_FakeClient(), keep=3)
    command = run_calls[0][0]

    assert "tail -3" in command
    # sort -V, not sort: a lexical sort puts 2.85.10 before 2.85.9, so the versions that survived
    # would be an arbitrary set rather than the newest ones.
    assert "sort -V" in command


def test_the_default_keeps_a_rollback_window_without_letting_the_pile_grow():
    assert KEEP_IMAGE_VERSIONS == 5


def test_a_tag_is_removed_only_when_it_is_not_in_the_keep_list(run_calls):
    """`grep -qx` — a whole-line match. `2.85.4` must not keep `2.85.49` alive by prefix."""
    _prune_old_images(_FakeClient())
    command = run_calls[0][0]

    assert "grep -qx" in command


def test_the_script_is_not_wrapped_in_another_layer_of_quoting(run_calls):
    """`sh -c '<script>'` would break on the script's own single quotes.

    `--format '{{.Tag}}'` and `grep -v '^latest$'` each close the wrapper's quote and reopen it,
    so those arguments arrive at docker unquoted. It survived by accident — `{{.Tag}}` has no
    comma for brace expansion and `^latest$` no variable to expand — but the first quoted argument
    that did would break the prune with no error, and this step is best-effort, so nothing would
    report it. The remote shell runs the command already; there is no second shell to ask for.
    """
    _prune_old_images(_FakeClient())
    command = run_calls[0][0]

    assert not command.startswith("sh -c")
    assert command.startswith("keep=$(docker images db_ops")
