"""A lab password may be a ref, so `sre_config.json` need not hold the secret itself.

The values in this config build a machine that is created, rehearsed on and destroyed, which is
why a literal was defensible and why it stayed literal for a long time. The problem is not the
throwaway lab — it is the lab that gets *kept*. At that point a password typed into a config file
is a stored credential living somewhere no other credential in this toolkit lives, outside the
encrypted store, outside `check-secret`, and inside a file people copy between machines.

So all three forms are read, in the toolkit's usual precedence: a literal, then `_password_env`,
then `_password_ref` against the secret store.

The resolution point matters as much as the resolution. `sre` serialises whole config sections
into a base64 payload for PowerShell and passes them to Ansible, and those consumers cannot look a
ref up — they need the value. Resolve on load and the secret lands in every dump of the config;
resolve never and a ref reaches a script that cannot use it. It happens at the boundary instead.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from db_ops.sre.config import load_sre_operational_config, resolve_password_fields


def test_a_literal_still_works_because_a_throwaway_lab_is_a_real_case():
    assert resolve_password_fields({"sa_password": "typed-in-place"})["sa_password"] == (
        "typed-in-place")


def test_a_ref_resolves_from_the_environment(monkeypatch):
    monkeypatch.setenv("LAB_SA_PASSWORD", "out-of-the-store")
    resolved = resolve_password_fields({"sa_password_ref": "LAB_SA_PASSWORD", "sa_user": "sa"})
    assert resolved["sa_password"] == "out-of-the-store"
    assert resolved["sa_user"] == "sa", "unrelated settings survive untouched"


def test_the_ref_key_does_not_survive_into_the_payload(monkeypatch):
    """What goes to PowerShell is the password, not the name of where it is kept."""
    monkeypatch.setenv("LAB_SA_PASSWORD", "out-of-the-store")
    resolved = resolve_password_fields({"sa_password_ref": "LAB_SA_PASSWORD"})
    assert "sa_password_ref" not in resolved
    assert "sa_password_env" not in resolved


def test_a_literal_beats_a_ref_so_an_override_is_possible(monkeypatch):
    monkeypatch.setenv("LAB_SA_PASSWORD", "from-the-store")
    resolved = resolve_password_fields(
        {"sa_password": "explicit-wins", "sa_password_ref": "LAB_SA_PASSWORD"})
    assert resolved["sa_password"] == "explicit-wins"


def test_a_field_that_is_not_a_password_is_not_touched():
    """`_ref` is a common suffix; only password fields get resolved."""
    section = {"template_ref": "ubuntu-22", "net_ref": "vmnet8"}
    assert resolve_password_fields(section) == section


def test_nothing_is_required_and_an_empty_section_is_fine():
    assert resolve_password_fields(None) == {}
    assert resolve_password_fields({}) == {}


def test_the_shipped_example_carries_no_literal_password(shipped_sre_config):
    """The example is what a reader copies, so it has to model the good shape, not the tolerated one."""
    text = json.dumps(shipped_sre_config)
    literals = [
        key for key, value in _password_fields(shipped_sre_config)
        if key.endswith("_password") and value
    ]
    assert not literals, (
        f"the example writes literal passwords at {literals}; use <name>_password_ref so a reader "
        f"copying it starts with the credential in the store")
    assert "_password_ref" in text, "and it has to show the form it is recommending"


def _password_fields(node, path=""):
    if isinstance(node, dict):
        for key, value in node.items():
            if "password" in key.lower():
                yield key, value
            else:
                yield from _password_fields(value, f"{path}.{key}")
    elif isinstance(node, list):
        for item in node:
            yield from _password_fields(item, path)


@pytest.fixture
def shipped_sre_config():
    """The *example*, deliberately — not `shipped_config`, which prefers the operator's own file.

    The subject here is what a reader copies. An operator's `data/sre_config.json` describes a lab
    that is theirs to configure however they like, and the toolkit still reads a literal there on
    purpose; the file that has to model the recommended shape is the one that ships.
    """
    example = Path(__file__).resolve().parent.parent / "data" / "sre_config.example.json"
    return json.loads(example.read_text(encoding="utf-8"))


def test_the_config_loads_and_resolves_through_the_real_loader(tmp_path, monkeypatch):
    monkeypatch.setenv("LAB_ROOT_PASSWORD", "resolved-at-use")
    config = tmp_path / "config.json"
    config.write_text(json.dumps({"sre": {
        "credentials": {"guest_user": "labuser", "guest_password_ref": "LAB_ROOT_PASSWORD"},
        "database_defaults": {"sqlserver": {"sa_password_ref": "LAB_ROOT_PASSWORD"}},
    }}), encoding="utf-8")

    loaded = load_sre_operational_config(config)
    assert loaded.guest_password() == "resolved-at-use"
    assert loaded.resolved_database_defaults()["sqlserver"]["sa_password"] == "resolved-at-use"
    assert "guest_password" not in loaded.credentials, (
        "the raw section keeps the ref; only the resolved view has the value")
