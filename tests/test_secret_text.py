import json

import pytest

from db_ops.common import data_sources
from db_ops.lib import secret_text
from db_ops.config import TelegramConfig


KEY = "Ahdsf#hsdf$%238Hfs#"
SECRETS = {"ACME_PWD": "p@ss/w0rd", "TELEGRAM_BOT_TOKEN_x": "8301738507:AA-token"}


def test_encrypt_decrypt_round_trip():
    blob = secret_text.encrypt_secret_text(SECRETS, KEY)
    assert secret_text.is_encrypted_blob(blob)
    assert "ciphertext" in blob and blob["marker"] == "db_ops.secret_text.v1"
    assert secret_text.decrypt_secret_text(blob, KEY) == SECRETS


def test_decrypt_with_wrong_key_raises():
    blob = secret_text.encrypt_secret_text(SECRETS, KEY)
    with pytest.raises(RuntimeError, match="wrong key or corrupted"):
        secret_text.decrypt_secret_text(blob, "not-the-key")


def test_decrypt_without_key_raises():
    blob = secret_text.encrypt_secret_text(SECRETS, KEY)
    with pytest.raises(RuntimeError, match="No decryption key"):
        secret_text.decrypt_secret_text(blob, "")


def test_load_prefers_encrypted_and_uses_env_key(tmp_path, monkeypatch):
    blob = secret_text.encrypt_secret_text(SECRETS, KEY)
    (tmp_path / "encrypted_secret_text.json").write_text(json.dumps(blob), encoding="utf-8")
    # A stale plaintext file must be ignored in favour of the encrypted one.
    (tmp_path / "secret_text.json").write_text(json.dumps({"ACME_PWD": "stale"}), encoding="utf-8")
    monkeypatch.setenv(secret_text.SECRET_KEY_ENV_VAR, KEY)

    assert data_sources.load_secret_text(tmp_path) == SECRETS


def test_no_plaintext_fallback(tmp_path):
    # A plaintext secret_text.json must be ignored: secrets come only from the
    # encrypted file. No silent fallback that could mask a wrong/missing key.
    (tmp_path / "secret_text.json").write_text(json.dumps(SECRETS), encoding="utf-8")
    assert data_sources.load_secret_text(tmp_path) == {}


def test_load_missing_returns_empty(tmp_path):
    assert data_sources.load_secret_text(tmp_path) == {}


def test_wrong_key_raises_even_with_plaintext_present(tmp_path, monkeypatch):
    # Even if a stale plaintext file exists, a wrong key on the encrypted file
    # must fail hard rather than fall back.
    blob = secret_text.encrypt_secret_text(SECRETS, KEY)
    (tmp_path / "encrypted_secret_text.json").write_text(json.dumps(blob), encoding="utf-8")
    (tmp_path / "secret_text.json").write_text(json.dumps(SECRETS), encoding="utf-8")
    with pytest.raises(RuntimeError, match="wrong key or corrupted"):
        data_sources.load_secret_text(tmp_path, key="wrong")


def test_explicit_key_overrides_env(tmp_path, monkeypatch):
    blob = secret_text.encrypt_secret_text(SECRETS, KEY)
    (tmp_path / "encrypted_secret_text.json").write_text(json.dumps(blob), encoding="utf-8")
    monkeypatch.setenv(secret_text.SECRET_KEY_ENV_VAR, "wrong-env-key")
    assert data_sources.load_secret_text(tmp_path, key=KEY) == SECRETS


def test_key_base64_decodes_to_plain_key():
    assert secret_text.decode_key_base64("QWhkc2YjaHNkZiQlMjM4SGZzIw==") == KEY


def test_key_base64_rejects_invalid_text():
    with pytest.raises(ValueError, match="valid base64"):
        secret_text.decode_key_base64("not base64!!!")


def test_set_key_env_accepts_key_base64(monkeypatch):
    monkeypatch.delenv(secret_text.SECRET_KEY_ENV_VAR, raising=False)
    secret_text.set_key_env(None, "QWhkc2YjaHNkZiQlMjM4SGZzIw==")
    assert secret_text.resolve_key() == KEY


def test_plain_key_and_key_base64_are_mutually_exclusive():
    with pytest.raises(ValueError, match="only one"):
        secret_text.resolve_cli_key(KEY, "QWhkc2YjaHNkZiQlMjM4SGZzIw==")


def test_telegram_bot_token_resolves_from_encrypted(tmp_path, monkeypatch):
    secrets = {"TELEGRAM_BOT_TOKEN_x": "8301738507:AA-token"}
    blob = secret_text.encrypt_secret_text(secrets, KEY)
    (tmp_path / "encrypted_secret_text.json").write_text(json.dumps(blob), encoding="utf-8")
    monkeypatch.setenv(secret_text.SECRET_KEY_ENV_VAR, KEY)
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)

    config = TelegramConfig(
        bot_token_env="TELEGRAM_BOT_TOKEN",
        telegram_bot_token_ref="TELEGRAM_BOT_TOKEN_x",
        secret_text_file=tmp_path / "secret_text.json",
    )
    assert config.resolved_bot_token == "8301738507:AA-token"
