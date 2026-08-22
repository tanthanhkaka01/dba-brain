"""A first-run step must not report success for having done nothing.

Both cases below were found by installing the published `dbabrain 0.1.0` from PyPI into an empty
repository and following the `AGENTS.md` that `db-ops init` writes there. Neither is exotic; both
are the first two commands anybody runs, and each reported success while achieving nothing:

- `encrypt-secret` was given the *wrapped* secret file — ``{"secrets": {...}}`` — which is what the
  guide documented. It stringified the nested object into one secret literally named ``secrets``
  and printed "Encrypted 1 secret(s)". The failure surfaced two commands later as
  ``Password ref not found``, naming a reference the plaintext file plainly appears to define.

- `check-credentials` was given ``--key-base64 <key>``, which every other command in the tool
  accepts. It read the flag as a folder name, walked a directory that does not exist, and printed
  "checked 0 target(s); 0 without a resolvable credential" — which reads as a pass. It is the
  command the guide names as the check to make *before* trusting the configuration.

The rule both encode: **a command that verifies something must fail when it cannot verify it**, and
a count of zero is a result to refuse rather than to report.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from db_ops.cli import _check_credentials_command
from db_ops.lib.secret_text import encrypt_secret_text_file


def test_a_nested_secret_file_is_refused_and_the_message_shows_the_shape(tmp_path: Path) -> None:
    source = tmp_path / "secret_text.json"
    source.write_text(json.dumps({"secrets": {"MSSQL_LAB": "pw"}}), encoding="utf-8")

    with pytest.raises(RuntimeError) as raised:
        encrypt_secret_text_file(source, tmp_path / "encrypted.json", "passphrase")

    message = str(raised.value)
    assert "secrets" in message, "the offending key has to be named"
    assert '{"REF_NAME": "the secret"}' in message, "and the shape it should have has to be shown"


def test_a_flat_secret_file_still_encrypts(tmp_path: Path) -> None:
    """The guard must not cost the shape that is correct."""
    source = tmp_path / "secret_text.json"
    source.write_text(json.dumps({"_notes": ["commentary"], "MSSQL_LAB": "pw"}), encoding="utf-8")

    count = encrypt_secret_text_file(source, tmp_path / "encrypted.json", "passphrase")

    assert count == 1, "one secret, and `_notes` is commentary rather than a secret"


def test_check_credentials_refuses_a_flag_where_a_folder_belongs(capsys) -> None:
    """`--key-base64` is accepted by every other command, so it will be passed to this one.

    The value is deliberately **not** base64, and that is the whole of it. What is asserted is that
    the *flag* is refused where a folder belongs, so the value is irrelevant — while a
    realistic-looking one costs something: the first version passed a base64 placeholder copied out
    of the quickstart (it decodes to an ordinary English word), and `gitleaks` reported the file as
    a leaked `generic-api-key`. A secret scanner that cries wolf is one people learn to switch off,
    so the literal goes rather than the rule — and it is not quoted here either, for the same
    reason.
    """
    code = _check_credentials_command(["--key-base64", "not-a-key"])

    assert code == 2, "a flag in the folder position is a usage error, not a folder"
    assert "not a flag" in capsys.readouterr().err


def test_check_credentials_refuses_a_folder_that_is_not_there(tmp_path: Path, capsys) -> None:
    """Nothing to check and everything fine are different answers."""
    code = _check_credentials_command([str(tmp_path / "no-such-folder")])

    assert code == 2
    assert "no such folder" in capsys.readouterr().err
