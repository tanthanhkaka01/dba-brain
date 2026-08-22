"""Choosing a tool from what the request states, instead of from what the code assumes.

Until 2026-08-19 every entry point in ``common`` knew the engine and the OS family and none of
them knew a *version*, on an estate holding Oracle 8.1.7 next to Oracle 23 and Windows Server 2003
next to 2025. The failure that made it concrete: ``run-sql`` against ``ACME-192-0-2-136`` (Oracle
8i, no ``sql_access`` block) handed the target to python-oracledb, which speaks 12.1 and newer, and
died with ``DPY-3010`` — a driver code naming neither the cause nor the fix.

What these tests protect is therefore two things and in this order: that a *stated* fact reaches
the decision and beats the config, and that a *missing* fact changes nothing. The second matters
more than it looks. This module was added under a live estate whose inventory is half-filled — 10
of 21 SQL Server instances carry no ``major_version`` at all — so any rule that behaved differently
on an unknown version would have changed production behaviour on the day it shipped.
"""

import pytest

from db_ops.lib.target_profile import (
    SOURCE_CONFIG,
    SOURCE_REQUEST,
    TargetProfile,
    ToolSelectionError,
    candidate_variants,
    hostcmd_runtime,
    parse_os_version,
    select_oracle_client_mode,
    select_powershell_dialect,
    select_sqlserver_driver,
    select_variant,
    version_matches,
    windows_management_transport_available,
)


# --------------------------------------------------------------------------- #
# Reading the facts
# --------------------------------------------------------------------------- #
def test_an_instance_record_and_a_request_are_read_by_the_same_parser():
    """One parser, so an inventory field and a typed request cannot disagree about a spelling.
    `db_instances.json` writes `sqlserver_major_version` on SQL Server entries and plain
    `major_version` elsewhere, and a caller should never have to know which."""
    from_record = TargetProfile.from_json(
        {"db_type": "mssql", "sqlserver_major_version": 10, "os": "Windows Server 2008 R2"}
    )
    assert from_record.db_type == "sqlserver"  # the alias is normalized, as everywhere else
    assert from_record.major_version == 10
    assert from_record.platform == "windows"
    assert from_record.os_version == (6, 1)


def test_a_container_target_is_a_docker_runtime_without_saying_so_twice():
    """`container_name` already means "this engine runs in a container"; making the caller also
    write `runtime: docker` would create a state where the two disagree."""
    assert TargetProfile.from_json({"container_name": "pg_ha-primary"}).runtime == "docker"
    assert TargetProfile.from_json({}).runtime == ""


def test_an_unknown_runtime_is_refused_rather_than_ignored():
    """A typo in `runtime` must not silently become "run it on the host" — that is the difference
    between a command inside a container and the same command on the machine hosting it."""
    with pytest.raises(ToolSelectionError, match="runtime must be one of"):
        TargetProfile.from_json({"runtime": "vm"})


@pytest.mark.parametrize(
    "caption, expected",
    [
        ("Windows Server 2025 Datacenter 10.0 (Build 26100, Hypervisor)", (10, 0)),
        ("Windows NT 6.2 (Build 9200)", (6, 2)),
        ("Windows Server 2003", (5, 2)),
        ("Windows Server 2008 R2", (6, 1)),
        # The dotted number wins over the product year when a caption carries both.
        ("Windows Server 2019 Standard 10.0 (Build 17763)", (10, 0)),
    ],
)
def test_a_windows_caption_yields_its_nt_version(caption, expected):
    assert parse_os_version(caption) == expected


def test_a_linux_caption_yields_no_version_because_the_number_would_mean_nothing():
    """`Linux (Ubuntu 22.04.5 LTS)` parses to 22.4 if you let it, and 22.4 answers no question
    this module asks — every rule here is about which PowerShell exists. A number that looks like
    an answer and is not one is worse than a blank."""
    assert parse_os_version("Linux (Ubuntu 22.04.5 LTS)") == (None, None)


def test_an_unrecognised_caption_stays_unknown_rather_than_being_guessed():
    assert parse_os_version("Windows Server Frobnicator Edition") == (None, None)


# --------------------------------------------------------------------------- #
# Precedence: the caller is looking at the server, the file is what someone typed
# --------------------------------------------------------------------------- #
def test_what_the_request_states_wins_over_what_the_inventory_records():
    request = TargetProfile.from_json({"major_version": 8}, source=SOURCE_REQUEST)
    config = TargetProfile.from_json(
        {"db_type": "oracle", "major_version": 19, "os": "Windows Server 2019 10.0"},
        source=SOURCE_CONFIG,
    )
    merged = request.merge(config)

    assert merged.major_version == 8            # the request's
    assert merged.db_type == "oracle"           # only config had one
    assert merged.os_version == (10, 0)         # ... and only config had this


def test_the_merged_profile_says_which_side_supplied_each_fact():
    """`chosen_by` on the tool answers "who picked the driver"; `sources` answers the same for
    the facts behind it. Without it, "why did it say version 8?" needs the inventory reopened."""
    merged = TargetProfile.from_json({"major_version": 8}, source=SOURCE_REQUEST).merge(
        TargetProfile.from_json({"db_type": "oracle"}, source=SOURCE_CONFIG)
    )
    assert merged.to_dict()["sources"] == {"major_version": "request", "db_type": "config"}


# --------------------------------------------------------------------------- #
# Variant selection — the rule that used to live inside metrics
# --------------------------------------------------------------------------- #
class _Variant:
    def __init__(self, name, db_type="", platform="", low=None, high=None, supported=True):
        self.name = name
        self.db_type = db_type
        self.platform = platform
        self.min_major_version = low
        self.max_major_version = high
        self.supported = supported
        self.path = f"{name}.sql"


LEGACY = _Variant("sqlserver_legacy_2008r2", "sqlserver", high=10)
MODERN = _Variant("sqlserver_modern_2012_plus", "sqlserver", low=11)


def test_an_old_server_gets_the_legacy_variant_and_a_new_one_does_not():
    for major, expected in ((10, LEGACY), (8, LEGACY), (11, MODERN), (16, MODERN)):
        profile = TargetProfile(db_type="sqlserver", major_version=major)
        assert select_variant([LEGACY, MODERN], profile) is expected


def test_with_no_version_the_last_supported_variant_answers():
    """The catalog is written oldest-first, so the last entry is the modern one — right for the
    instances that carry no `major_version` today, and the reason `docs/04_metrics_engine.md`
    asks for the field rather than leaning on this."""
    profile = TargetProfile(db_type="sqlserver")
    assert select_variant([LEGACY, MODERN], profile) is MODERN


def test_a_variant_marked_unsupported_is_never_selected():
    """`supported: false` carries a reason and exists to explain a gap, not to be run."""
    broken = _Variant("oracle_8i", "oracle", high=8, supported=False)
    profile = TargetProfile(db_type="oracle", major_version=8)
    assert select_variant([broken], profile) is None


def test_a_version_outside_every_window_selects_nothing_rather_than_the_nearest():
    profile = TargetProfile(db_type="sqlserver", major_version=9)
    assert select_variant([_Variant("only_modern", "sqlserver", low=11)], profile) is None


def test_candidates_are_filtered_by_engine_for_sql_and_by_platform_for_os_metrics():
    """The two collectors ask opposite questions: a SQL variant is chosen by engine, an OS one by
    platform with the engine irrelevant. One function, one flag, so neither grows a second copy."""
    windows = _Variant("os_windows", "", platform="windows")
    linux = _Variant("os_linux", "", platform="linux")
    profile = TargetProfile(db_type="oracle", platform="windows")

    assert candidate_variants([windows, linux], profile, match_platform=True) == [windows]
    # Without the platform flag a wildcard db_type is not a match: a SQL metric names its engine.
    assert candidate_variants([windows, linux], profile) == []


def test_an_unknown_version_does_not_close_a_version_window():
    """Unknown means "do not gate on this". Excluding every gated variant instead would leave the
    32 targets with no recorded version running nothing at all."""
    assert version_matches(MODERN, None) is True
    assert version_matches(MODERN, 10) is False


# --------------------------------------------------------------------------- #
# Which driver
# --------------------------------------------------------------------------- #
def test_oracle_8i_is_refused_with_the_two_ways_out_named():
    """The whole point of the module, in one assertion: replace `DPY-3010` with the sentence that
    says what to do — the bridge that already serves .235/.236, or a thick client."""
    profile = TargetProfile(db_type="oracle", major_version=8)
    with pytest.raises(ToolSelectionError) as raised:
        select_oracle_client_mode(profile)
    message = str(raised.value)
    assert "thin mode" in message
    assert '"method": "api"' in message and "thick" in message


def test_an_oracle_of_unknown_version_still_connects_thin_exactly_as_before():
    """Half the inventory has no version. Refusing on "unknown" would have taken production down
    the day this shipped; refusing on "known to be impossible" is the entire remit."""
    assert select_oracle_client_mode(TargetProfile(db_type="oracle")).tool == "thin"
    assert select_oracle_client_mode(TargetProfile(db_type="oracle", major_version=19)).tool == "thin"


def test_a_requested_thick_client_overrides_the_version_rule():
    """An operator who has installed the client knows something the inventory does not."""
    choice = select_oracle_client_mode(TargetProfile(db_type="oracle", major_version=8), "thick")
    assert choice.tool == "thick" and choice.chosen_by == "request"


def test_a_nonsense_client_mode_is_refused_rather_than_falling_back_to_thin():
    with pytest.raises(ToolSelectionError, match="must be 'thin' or 'thick'"):
        select_oracle_client_mode(TargetProfile(db_type="oracle"), "native")


def test_the_sqlserver_driver_says_who_chose_it():
    """Request beats config beats default — the one precedence, reported so that "why did it use
    pymssql?" is answered by the response instead of by reading three files."""
    profile = TargetProfile(db_type="sqlserver", major_version=10)
    assert select_sqlserver_driver(profile, requested="pymssql").chosen_by == "request"
    assert select_sqlserver_driver(profile, configured="ODBC Driver 17 for SQL Server").chosen_by == "config"
    assert select_sqlserver_driver(TargetProfile(db_type="sqlserver")).chosen_by == "default"


def test_an_old_sqlserver_gets_no_special_driver_rule():
    """Pins the *absence* of the rule, because it is the change a reader will want to make and it
    was already made and reverted. A Driver-17-first order for 2008 R2 was measured against all
    four 10.50 instances here on 2026-08-19: every one completes on Driver 18 / Encrypt=optional
    at the first attempt, so the rule would have saved no round trip and downgraded four
    production connections from encrypted-when-offered to plaintext."""
    old = TargetProfile(db_type="sqlserver", major_version=10)
    new = TargetProfile(db_type="sqlserver", major_version=16)
    assert select_sqlserver_driver(old) == select_sqlserver_driver(new)
    assert select_sqlserver_driver(old).chosen_by == "default"


# --------------------------------------------------------------------------- #
# Which shell, and whether the host can be reached at all
# --------------------------------------------------------------------------- #
def test_a_2012_host_gets_cim_and_a_2008r2_host_gets_wmi():
    """`Get-CimInstance` and `ConvertTo-Json` are PowerShell 3.0 / NT 6.2. Below that the fact
    script fails as an unknown cmdlet, which reads like a permissions problem and is not one."""
    assert select_powershell_dialect(TargetProfile(platform="windows", os_major=6, os_minor=2)).tool == "cim"
    assert select_powershell_dialect(TargetProfile(platform="windows", os_major=6, os_minor=1)).tool == "wmi"


def test_an_unknown_windows_version_keeps_todays_script():
    assert select_powershell_dialect(TargetProfile(platform="windows")).tool == "cim"


def test_windows_server_2003_has_no_management_transport_at_all():
    """NT 5.2 ships no WinRM and cannot run the OpenSSH server, so neither of the two transports
    `lib.cmd_access` supports can exist. 192.0.2.235 and .236 are the two here, and the nine
    hand-listed OS_* codes in their `report_policy` are what this predicate replaces."""
    assert windows_management_transport_available(
        TargetProfile(platform="windows", os_major=5, os_minor=2)) is False
    assert windows_management_transport_available(
        TargetProfile(platform="windows", os_major=6, os_minor=1)) is True
    # Unknown is not "unreachable": a blank inventory field must not disable a working host.
    assert windows_management_transport_available(TargetProfile(platform="windows")) is True
    assert windows_management_transport_available(TargetProfile(platform="linux")) is True


def test_the_hostcmd_vocabulary_is_translated_at_the_boundary_not_stored():
    """`hostcmd.Host.runtime` answers "which OS" and "inside what" with one field, which is why
    run-cmd cannot target a container while backup-database can. The profile keeps them apart and
    collapses only on the way out, so the existing callers keep the string they already parse."""
    assert hostcmd_runtime(TargetProfile(platform="windows")) == "windows"
    assert hostcmd_runtime(TargetProfile(platform="linux", runtime="docker")) == "docker"
    assert hostcmd_runtime(TargetProfile(platform="linux", runtime="host")) == "linux"


# --------------------------------------------------------------------------- #
# The rule this module is allowed to be: pure
# --------------------------------------------------------------------------- #
def test_the_selection_rule_reads_no_file_and_reaches_nothing():
    """`lib` already may not import outside itself (`test_lib_is_pure.py`). This is the other half
    of the same promise and the one that matters for a *decision* layer: the choice of tool must
    be a function of the arguments, or two callers holding the same facts can get two answers
    because one of them happened to be run where a config file was readable.

    It is also the operator's stated constraint for this change, 2026-08-19: the selection layer
    does not read config, and the caller passes what it knows.
    """
    import ast
    from pathlib import Path

    source = Path(__file__).resolve().parents[1] / "db_ops" / "lib" / "target_profile.py"
    tree = ast.parse(source.read_text(encoding="utf-8"), filename=str(source))

    forbidden_calls = {"open", "load_json_file"}
    forbidden_attrs = {"read_text", "read_bytes", "getenv", "urlopen", "connect", "run", "Popen"}
    offenders = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Name) and func.id in forbidden_calls:
                offenders.append(func.id)
            elif isinstance(func, ast.Attribute) and func.attr in forbidden_attrs:
                offenders.append(func.attr)
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            names = [a.name for a in node.names] + ([node.module] if isinstance(node, ast.ImportFrom) else [])
            offenders += [n for n in names if n and n.split(".")[0] in
                          {"os", "io", "socket", "subprocess", "requests", "urllib", "pathlib"}]

    assert not offenders, (
        f"lib/target_profile.py touches {sorted(set(offenders))}. It decides which tool a target "
        "needs and must do so from its arguments alone: the caller reads the inventory, this "
        "module reads nothing."
    )
