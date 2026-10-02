"""A restore must not start a copy that cannot fit.

On 2026-09-17 a drill copied about 115 GB onto the host that also carries the runtime store. ``/``
reached 42 MB free, PostgreSQL could not write, and the daemon died with it — 26 minutes into a soak
that then had to be abandoned. Nothing had asked whether the files fit, because nothing could: the
question had no owner.

The rule the operator set on 2026-09-19 is deliberately blunt, so it can be checked at any hour::

    free >= bytes_to_copy x factor        default 2.0, for every engine (1.5 until 0.26.0)

These tests are that rule (:mod:`db_ops.lib.restore_space`, arithmetic only) and the refusal built
on it (:mod:`db_ops.backup_restore.space`). The case that matters most is the last one: a restore
that **cannot be measured** is refused, because the unmeasured restore is the one that emptied the
disk.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from db_ops.lib import restore_space


# --------------------------------------------------------------------------- #
# The arithmetic
# --------------------------------------------------------------------------- #
GIB = 1024 ** 3


def test_the_incident_would_now_be_refused():
    """115 GB incoming, 92 GB free — the numbers measured on that host, and on this one today."""
    verdict = restore_space.judge(115 * GIB, 92 * GIB, 1.5)

    assert not verdict.ok
    assert verdict.required_bytes == int(115 * GIB * 1.5)
    assert "SHORT BY" in verdict.text


def test_a_restore_that_fits_says_so_with_its_numbers():
    verdict = restore_space.judge(30 * GIB, 92 * GIB, 2.0)

    assert verdict.ok
    assert verdict.text == "30.0 GiB to copy, x2 = 60.0 GiB needed, 92.0 GiB free - fits"


def test_the_margin_is_the_point_so_room_that_merely_fits_is_refused():
    """50 GiB free takes 40 GiB of files and no more. At 1.0 that passes; at 1.5 it does not, and
    the difference between the two is every byte the restore itself writes."""
    assert restore_space.judge(40 * GIB, 50 * GIB, 1.0).ok
    assert not restore_space.judge(40 * GIB, 50 * GIB, 1.5).ok


def test_exactly_the_required_amount_passes():
    """>=, not >: a rule that refuses at the exact number is a different rule from the one written."""
    assert restore_space.judge(10 * GIB, 15 * GIB, 1.5).ok


def test_the_requirement_rounds_up():
    """Rounding down can only ever let something through that the rule says should not pass."""
    assert restore_space.required_bytes(3, 1.5) == 5  # 4.5 -> 5


def test_nothing_to_copy_needs_nothing():
    assert restore_space.judge(0, 0, 2.0).ok


# --------------------------------------------------------------------------- #
# What an entry may say
# --------------------------------------------------------------------------- #
def test_the_check_is_on_for_an_entry_that_says_nothing():
    """A check that has to be switched on protects only the entries somebody remembered, and the
    entry nobody remembered is the one that fills the disk."""
    rule = restore_space.parse_space_check({})

    assert rule.enabled
    assert rule.factor == 2.0, "x2, for every engine, since 0.26.0"
    assert rule.on_unknown == "refuse"
    assert rule.measure_restore is False, "only the copy is measured unless the entry asks"


def test_an_entry_can_ask_for_more_or_for_less():
    assert restore_space.parse_space_check({"space_check": {"factor": 3.0}}).factor == 3.0
    assert restore_space.parse_space_check({"space_check": {"factor": 1.0}}).factor == 1.0


def test_a_factor_below_one_is_refused_and_says_how_to_turn_the_check_off_instead():
    """Below 1.0 the rule asks for less room than the files need: that is not a margin, it is the
    check written off while still appearing to be there."""
    with pytest.raises(restore_space.RestoreSpaceError) as caught:
        restore_space.parse_space_check({"space_check": {"factor": 0.5}})

    assert "enabled" in str(caught.value), "the refusal names the honest way to switch it off"


def test_a_misspelled_field_is_refused_rather_than_ignored():
    """`{"factory": 3.0}` accepted silently is a restore running at 2 while its config says 3."""
    with pytest.raises(restore_space.RestoreSpaceError) as caught:
        restore_space.parse_space_check({"space_check": {"factory": 3.0}})

    assert "factory" in str(caught.value)


def test_on_unknown_takes_only_the_two_answers_that_exist():
    assert restore_space.parse_space_check(
        {"space_check": {"on_unknown": "proceed"}}).on_unknown == "proceed"
    with pytest.raises(restore_space.RestoreSpaceError):
        restore_space.parse_space_check({"space_check": {"on_unknown": "warn"}})


# --------------------------------------------------------------------------- #
# The refusal
# --------------------------------------------------------------------------- #
@pytest.fixture()
def entry(monkeypatch):
    """A restore config stub with the two measurements under the test's control."""
    from db_ops.backup_restore import space

    class Config:
        restore_id = "DRILL"
        target_id = "DRILL"
        is_linux = False
        source_backup_dir = "//source/share"
        vm_import_unc = "//target/import"
        vm_import_local = "/import"
        space_check = restore_space.SpaceCheck()

    return Config()


def _measured(monkeypatch, *, incoming, free):
    from db_ops.backup_restore import space

    monkeypatch.setattr(space, "measure_copy", lambda config, **_: (
        None if incoming is None else space.CopyMeasure(to_write=incoming, staged=0)))
    monkeypatch.setattr(space, "measure_target_free_bytes", lambda config: free)


def test_a_linux_target_is_asked_about_a_posix_path_not_a_windows_one():
    r"""Found on the estate, 2026-09-19. `vm_import_local` is a `Path`, so on the Windows node that
    drives these restores a Linux path renders as `\opt\db_ops\...`. `df` on the target then says
    "no such file", this module can only report "could not measure", and the restore is refused —
    safely, and for entirely the wrong reason. Every Linux-target restore was in that state."""
    from db_ops.backup_restore import space

    class LinuxTarget:
        is_linux = True
        vm_import_unc = Path("/opt/db_ops/backup/SQLBK_IMPORT/ACME-192-0-2-250")
        vm_import_local = Path("/opt/db_ops/backup/SQLBK_IMPORT/ACME-192-0-2-250")

    described = space.target_description(LinuxTarget())

    assert described == "/opt/db_ops/backup/SQLBK_IMPORT/ACME-192-0-2-250"
    assert "\\" not in described


def test_a_copy_that_would_not_fit_is_stopped_before_any_byte_moves(monkeypatch, entry):
    from db_ops.backup_restore import space

    _measured(monkeypatch, incoming=115 * GIB, free=92 * GIB)

    with pytest.raises(space.RestoreSpaceRefused) as caught:
        space.check_free_space(entry, log=lambda _message: None)

    message = str(caught.value)
    assert "SHORT BY" in message
    assert "Nothing was copied" in message
    assert "space_check.factor" in message, "the message says what the operator can change"


def test_a_copy_that_fits_returns_what_it_measured(monkeypatch, entry):
    from db_ops.backup_restore import space

    _measured(monkeypatch, incoming=10 * GIB, free=92 * GIB)

    result = space.check_free_space(entry, log=lambda _message: None)

    assert result["ok"] is True
    assert result["required_bytes"] == 20 * GIB


def test_a_restore_that_cannot_be_measured_is_refused(monkeypatch, entry):
    """The one that matters. An unmeasured restore is exactly the one that filled the disk, and
    'could not read the free space' must not read as 'there is enough'."""
    from db_ops.backup_restore import space

    _measured(monkeypatch, incoming=10 * GIB, free=None)

    with pytest.raises(space.RestoreSpaceRefused) as caught:
        space.check_free_space(entry, log=lambda _message: None)

    assert "could not read the target's free space" in str(caught.value)


def test_an_entry_may_accept_an_unmeasured_restore_but_has_to_say_so(monkeypatch, entry):
    from db_ops.backup_restore import space

    entry.space_check = restore_space.SpaceCheck(on_unknown="proceed")
    _measured(monkeypatch, incoming=None, free=None)
    said: list[str] = []

    result = space.check_free_space(entry, log=said.append)

    assert result["checked"] is False
    assert any("on_unknown=proceed" in line for line in said), "every such run says so in the log"


def test_turning_the_check_off_is_possible_and_visible(monkeypatch, entry):
    from db_ops.backup_restore import space

    entry.space_check = restore_space.SpaceCheck(enabled=False)
    said: list[str] = []

    result = space.check_free_space(entry, log=said.append)

    assert result == {"checked": False, "reason": "disabled"}
    assert any("disabled" in line for line in said)


def test_the_preflight_stops_on_it_the_way_it_stops_on_an_unreachable_share(monkeypatch, entry):
    """Callers already stop on PreflightError. A new exception type would need every one of them
    taught about it, and the one that was not taught is a restore that runs anyway."""
    from db_ops.backup_restore import preflight

    def refuse(config, **kwargs):
        raise preflight.RestoreSpaceRefused("DRILL will not fit: 115.0 GiB to copy")

    monkeypatch.setattr(preflight, "check_free_space", refuse)

    with pytest.raises(preflight.PreflightError) as caught:
        preflight.run_target_preflight(entry, logger=None)

    assert "will not fit" in str(caught.value)


def test_a_linux_target_s_staging_folder_that_does_not_exist_yet_is_measured_on_its_parent():
    """The 0.25.0 soak, 2026-09-29: `.250` was rebuilt, its `SQLBK_IMPORT` folder went with it, and
    `df` on the missing folder made the first restore onto it "could not measure" - refused. The
    copy that makes the folder runs after this check, so the check has to measure where it will be."""
    import shutil
    import subprocess

    from db_ops.backup_restore import space

    bash = shutil.which("bash")
    if bash is None:
        pytest.skip("no bash on this machine to run the target's command")
    import tempfile
    with tempfile.TemporaryDirectory() as root:
        missing = Path(root, "SQLBK_IMPORT", "ACME-192-0-2-250").as_posix()
        answer = subprocess.run([bash, "-c", space.linux_free_space_command(missing)],
                                capture_output=True, text=True, timeout=30)

    lines = [line for line in answer.stdout.splitlines() if line.strip()]
    assert answer.returncode == 0, answer.stderr
    assert len(lines) >= 2 and int(lines[-1].split()[3]) > 0


def test_the_linux_measurement_sends_the_climbing_command(monkeypatch):
    from db_ops.backup_restore import space

    sent: list[str] = []

    class Client:
        def run(self, command):
            sent.append(command)
            return type("Answer", (), {"stdout": "Filesystem 1024-blocks Used Available Capacity Mounted\n"
                                                 "/dev/sda1 100 40 60 40% /\n"})()

        def close(self):
            pass

    class LinuxTarget:
        is_linux = True
        vm_import_unc = Path("/opt/db_ops/backup/SQLBK_IMPORT/ACME-192-0-2-250")
        vm_import_local = vm_import_unc

    monkeypatch.setattr(space, "open_ssh_connection", lambda config: Client())

    assert space._linux_free_bytes(LinuxTarget()) == 60 * 1024
    assert sent == [space.linux_free_space_command("/opt/db_ops/backup/SQLBK_IMPORT/ACME-192-0-2-250")]
    assert "while [ ! -e" in sent[0]
