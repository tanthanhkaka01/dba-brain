"""The backup certificate is fetched from Vault over verified TLS unless an entry says otherwise
(review 0.25.0, B5.4). The request carries the Vault token and returns the certificate's private
key; verification used to be off by default.
"""

from __future__ import annotations

import dataclasses
import json
import ssl

import pytest

from db_ops.backup_restore import certificate
from db_ops.backup_restore.config import BackupRestoreConfig


def _fetch(monkeypatch, **over):
    seen = {}

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def read(self):
            return json.dumps({"data": {"data": {}}}).encode()

    def urlopen(req, timeout=None, context=None):
        seen["context"] = context
        return Response()

    monkeypatch.setattr(certificate.request, "urlopen", urlopen)
    monkeypatch.setattr(certificate, "resolve_password_ref", lambda ref: "token")
    monkeypatch.setattr(certificate, "parse_backup_certificate", lambda data: None)
    fields = {f.name for f in dataclasses.fields(BackupRestoreConfig)}
    config = type("C", (), {"certificate_api_url": "https://vault.internal/x", "certificate_api_token_ref": "T",
                             "certificate_api_verify_tls": True, "certificate_api_ca_file": "", **over})()
    assert "certificate_api_ca_file" in fields
    certificate.fetch_backup_certificate(config)
    return seen["context"]


def test_verification_is_the_default():
    assert BackupRestoreConfig.__dataclass_fields__["certificate_api_verify_tls"].default is True


def test_a_verified_fetch_uses_the_default_context(monkeypatch):
    assert _fetch(monkeypatch) is None


def test_switched_off_it_says_so(monkeypatch, capsys):
    context = _fetch(monkeypatch, certificate_api_verify_tls=False)

    assert context is not None and context.verify_mode == ssl.CERT_NONE
    assert "TLS verification is off" in capsys.readouterr().err


def test_an_internal_ca_file_is_used(monkeypatch, tmp_path):
    with pytest.raises((ssl.SSLError, FileNotFoundError, OSError)):
        _fetch(monkeypatch, certificate_api_ca_file=str(tmp_path / "missing-ca.pem"))
