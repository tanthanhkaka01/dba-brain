"""The assistant answers through dbabrain.dev with no Anthropic key on the node (1.0.0, plan A2).

D-36.1 (the operator, 2026-11-06): no Claude key is handed to a user; a user's own dbabrain key calls
dbabrain.dev, which calls the AI. So `provider: dbabrain` points the same SDK client at the relay with
the node's dbabrain key - and three things are held here:

* the file states the relay's terms (the cheapest model, effort medium, 8,000 tokens a turn) or it is
  refused: the node never sends what dbabrain.dev would refuse, and nothing is changed quietly;
* a refusal from dbabrain.dev - this month's dollar spent, a key ended - reaches the asker in its own
  words, not as an HTTP error's repr;
* without a valid dbabrain key every question is answered with how to get one (D3: the own-key
  assistant needs a free key too) - never a queue left unread, never a skip nobody sees.
"""

from __future__ import annotations

import json
import types
from datetime import datetime, timedelta, timezone

import pytest

from dbabrain.lib import ai_assist as A
from dbabrain.lib import dbabrain_key as dk
from dbabrain.lib import secret_text
from dbabrain.telegram import ai_assist as assistant

from tests.test_dbabrain_key import Signer

NOW = datetime(2026, 11, 21, tzinfo=timezone.utc)
PASSPHRASE = "a-throwaway-passphrase-for-this-test-only"


def _document(**changes):
    """A node on its own Anthropic key (the shipped file answers through the relay since 1.0.0)."""
    own = {"provider": "anthropic", "model": "claude-sonnet-5-5", "max_output_tokens_turn": 16000}
    return {**A.shipped_document(), "users": [], **own, **changes}


def _relay_document(**changes):
    terms = {"provider": "dbabrain", "model": A.RELAY_MODEL, "effort": A.RELAY_EFFORT,
             "max_output_tokens_turn": A.RELAY_MAX_OUTPUT_TOKENS}
    return _document(**{**terms, **changes})


def test_a_file_that_states_the_relays_terms_reads():
    settings = A.parse_ai_assist(_relay_document())

    assert settings.provider == "dbabrain" and settings.model == "claude-haiku-5-5"


@pytest.mark.parametrize(("field", "value"), [("model", "claude-sonnet-5-5"), ("effort", "high"),
                                              ("max_output_tokens_turn", 16000)])
def test_a_file_asking_the_relay_for_more_is_refused_by_name(field, value):
    with pytest.raises(A.AiAssistConfigError, match=f"provider dbabrain.*{field}"):
        A.parse_ai_assist(_relay_document(**{field: value}))


def test_a_new_install_answers_through_dbabrain_dev_and_an_old_one_keeps_its_own_key():
    shipped = A.parse_ai_assist({**A.shipped_document(), "users": []})
    old = {key: value for key, value in _document().items() if key != "provider"}
    A.state_ai_assist(old)

    assert shipped.provider == "dbabrain" and shipped.enabled is True
    assert old["provider"] == "anthropic" and A.parse_ai_assist(old).model == "claude-sonnet-5-5"


def test_another_provider_is_refused():
    with pytest.raises(A.AiAssistConfigError, match="provider must be one of"):
        A.parse_ai_assist(_document(provider="openai"))


class _FakeClient:
    """The SDK client, as far as sdk_call uses it: what it was built with, and one answer or one error."""

    built: dict = {}
    raise_with: Exception | None = None

    def __init__(self, **options):
        _FakeClient.built = options
        self.beta = types.SimpleNamespace(messages=types.SimpleNamespace(create=self._create))

    def _create(self, **params):
        if _FakeClient.raise_with is not None:
            raise _FakeClient.raise_with
        return types.SimpleNamespace(to_dict=lambda: {"content": [], "model": params["model"]}, _request_id="r1")


@pytest.fixture
def fake_sdk(monkeypatch):
    anthropic = pytest.importorskip("anthropic")

    monkeypatch.setattr(anthropic, "Anthropic", _FakeClient)
    _FakeClient.raise_with = None
    return anthropic


def test_the_relay_provider_talks_to_dbabrain_dev_with_the_dbabrain_key(fake_sdk):
    call = assistant.sdk_call(A.parse_ai_assist(_relay_document()), "dbbk1.the.key")

    call({"model": A.RELAY_MODEL, "max_tokens": 100, "messages": []})

    assert _FakeClient.built["base_url"] == A.RELAY_BASE_URL
    assert _FakeClient.built["api_key"] == "dbbk1.the.key"


def test_a_node_set_to_opus_at_high_on_the_relay_is_refused_both_named():
    """The operator's question, 2026-11-21: a node on the free plan set to Opus 5.5 at effort high is
    refused - by the node first (here), and by dbabrain.dev whatever a patched node sends (ai-rules.ts)."""
    with pytest.raises(A.AiAssistConfigError) as caught:
        A.parse_ai_assist(_relay_document(model="claude-opus-5-5", effort="high"))

    assert "claude-opus-5-5" in str(caught.value) and "'high'" in str(caught.value)


def test_the_own_key_provider_talks_to_anthropic(fake_sdk):
    assistant.sdk_call(A.parse_ai_assist(_document()), "sk-ant-own")

    assert "base_url" not in _FakeClient.built


def _status_error(anthropic, status, message):
    import httpx2 as httpx

    response = httpx.Response(status, request=httpx.Request("POST", "https://mcp.dbabrain.dev/ai/v1/messages"),
                              json={"type": "error", "error": {"type": "permission_error", "message": message}})
    return anthropic.APIStatusError(message, response=response,
                                    body={"type": "error", "error": {"type": "permission_error", "message": message}})


def test_a_refusal_from_dbabrain_dev_is_said_in_its_own_words(fake_sdk):
    said = "this month's $1.00 of free AI is spent ($1.02) - it resets on the 1st (UTC)"
    _FakeClient.raise_with = _status_error(fake_sdk, 403, said)
    call = assistant.sdk_call(A.parse_ai_assist(_relay_document()), "dbbk1.the.key")

    with pytest.raises(assistant.FromDbabrainDev) as caught:
        call({"model": A.RELAY_MODEL, "max_tokens": 100, "messages": []})

    assert str(caught.value) == said


@pytest.fixture
def node(tmp_path, monkeypatch):
    monkeypatch.setenv(secret_text.SECRET_KEY_ENV_VAR, PASSPHRASE)
    monkeypatch.setenv("DBABRAIN_DATA_DIR", str(tmp_path / "data"))
    (tmp_path / "data").mkdir()
    return tmp_path / "data"


@pytest.mark.real_key_gate
def test_without_a_key_each_question_is_answered_with_how_to_get_one(node):
    key, call, missing = assistant._key_and_call(A.parse_ai_assist(_document(api_key_ref="OWN")), {"OWN": "sk"}, None)

    assert missing == "" and key == ""
    with pytest.raises(assistant.FromDbabrainDev, match="dbabrain key-request"):
        call({})


@pytest.mark.real_key_gate
def test_with_a_key_the_own_anthropic_key_is_used(node, monkeypatch):
    signer = Signer()
    monkeypatch.setattr(dk, "SIGNING_PUBLIC_KEYS", signer.keys)
    secret_text.set_secret_text(node, dk.KEY_REF, signer.token(issued=datetime.now(timezone.utc)), key=PASSPHRASE)

    key, call, missing = assistant._key_and_call(A.parse_ai_assist(_document(api_key_ref="OWN")), {"OWN": "sk-own"}, None)

    assert (key, call, missing) == ("sk-own", None, "")


@pytest.mark.real_key_gate
def test_the_relay_provider_takes_the_dbabrain_key_from_the_store(node, monkeypatch):
    signer = Signer()
    monkeypatch.setattr(dk, "SIGNING_PUBLIC_KEYS", signer.keys)
    token = signer.token(issued=datetime.now(timezone.utc))
    secret_text.set_secret_text(node, dk.KEY_REF, token, key=PASSPHRASE)

    key, call, missing = assistant._key_and_call(A.parse_ai_assist(_relay_document()), {dk.KEY_REF: token}, None)

    assert key == token and call is None and missing == ""


@pytest.mark.real_key_gate
def test_an_ended_key_answers_with_renew(node, monkeypatch):
    signer = Signer()
    monkeypatch.setattr(dk, "SIGNING_PUBLIC_KEYS", signer.keys)
    token = signer.token(issued=datetime.now(timezone.utc) - timedelta(days=40))
    secret_text.set_secret_text(node, dk.KEY_REF, token, key=PASSPHRASE)

    _key, call, _missing = assistant._key_and_call(A.parse_ai_assist(_relay_document()), {dk.KEY_REF: token}, None)

    with pytest.raises(assistant.FromDbabrainDev, match="key-renew"):
        call({})


def test_the_relay_takes_no_api_key_ref(tmp_path):
    raw = _relay_document(enabled=True, api_key_ref="", private_chat_enabled=True)
    (tmp_path / A.FILE_NAME).write_text(json.dumps(raw), encoding="utf-8")

    settings, missing = assistant.load_settings(tmp_path)

    assert settings is not None and missing == ""
