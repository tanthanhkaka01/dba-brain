"""Operator data does not live inside a shipped package — and if it must, it is named and withheld.

The mechanism: `PRIVATE_SUBPATHS` keeps one *file* out of the export while the package around it
ships. `PRIVATE_PATHS` cannot express this — it matches a top-level name, which is right for
`audits/` or `data/` and cannot say "this file, three levels down".

It existed for `db_ops/sre/data_folder/`, which held the captured record of one real lab install —
three lab hosts, a workstation name, a Windows account, an SSH key path — inside a shipped package,
because the script that read those files lived there too. Withholding the folder took the script
out with it; withholding the two files by name kept the code shipping.

**On 2026-08-23 the data moved instead, and that is the better answer.** A dated capture of one
real run is exactly what `audits/` is for, and a shipped example took its place as the script's
default. `PRIVATE_SUBPATHS` is empty now, so what these tests guard is that it *stays* honest:
the mechanism still works when something needs it, and nothing quietly reappears beside the code.

Note what does not protect this: `check-identifiers` derives its terms from the inventory, so a
developer workstation or a lab VM that was never a monitored target reads as clean.
"""
from __future__ import annotations

import json
from pathlib import Path

from db_ops.lib.distribution import PRIVATE_SUBPATHS, is_private_subpath

REPO = Path(__file__).resolve().parent.parent
DATA_FOLDER = REPO / "db_ops" / "sre" / "data_folder"


def test_the_mechanism_still_withholds_a_named_file():
    """Empty today; it has to work the day somebody needs it."""
    assert is_private_subpath("db_ops/anything/secret.json") is None
    reason = "invented for this test"
    try:
        PRIVATE_SUBPATHS["db_ops/anything/secret.json"] = reason
        assert is_private_subpath("db_ops/anything/secret.json") == reason
        assert is_private_subpath(r"db_ops\anything\secret.json") == reason, (
            "the export walks Windows paths; a backslash must match too")
        assert is_private_subpath("db_ops/anything/other.json") is None
    finally:
        PRIVATE_SUBPATHS.pop("db_ops/anything/secret.json", None)


def test_every_withheld_subpath_states_why_and_exists():
    """An entry naming a file that is gone is folklore, and hides that the list is stale."""
    for relative, reason in PRIVATE_SUBPATHS.items():
        assert reason.strip(), f"{relative} is withheld without saying why"
        assert (REPO / relative).exists(), f"{relative} is withheld but no longer exists"


def test_the_captured_lab_run_moved_out_of_the_package():
    """The capture belongs in `audits/`, which never ships, not beside the code that read it."""
    strays = sorted(p.name for p in DATA_FOLDER.glob("*.json") if not p.name.endswith(".example.json"))
    assert not strays, (
        f"{strays} sit inside a shipped package. A dated capture of one real run goes in audits/; "
        f"an example that documents the shape goes here, named *.example.json")


def test_the_shipped_example_carries_no_real_host_and_no_literal_password():
    example = DATA_FOLDER / "install_sql_server.example.json"
    assert example.is_file(), "the script's default has to ship, or the default is broken"
    config = json.loads(example.read_text(encoding="utf-8"))

    ssh = config["ssh"]
    assert "sudo_password" not in ssh, (
        "the example models the recommended shape; use sudo_password_ref into the secret store")
    assert ssh.get("sudo_password_ref"), "and it has to show the form it is recommending"

    hosts = [node["host"] for node in config["nodes"]]
    assert all(h.startswith("203.0.113.") for h in hosts), (
        f"example hosts must come from the documentation range (RFC 5737), got {hosts}")


def test_the_script_defaults_to_a_file_that_ships():
    """A default pointing at a withheld file is why the folder could not ship in the first place."""
    source = (DATA_FOLDER / "deploy_sqlserver_ag.py").read_text(encoding="utf-8")
    assert '_DEFAULT_INSTALL_JSON = _HERE / "install_sql_server.example.json"' in source
    assert "20260612" not in source, "the moved capture is still named somewhere in the script"
