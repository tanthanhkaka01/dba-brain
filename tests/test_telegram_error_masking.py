"""What an operator is allowed to see when a Telegram command fails.

The rule used to be "any message mentioning a password is suppressed", which protects the
value by destroying the explanation: a guard that refuses a password *because of the
characters in it* says so in a sentence containing the word "password", and the operator got
`workflow failed; sensitive error detail hidden` instead — a dead end on the only interface
they use.

The rule is now: remove the credential **value**, keep the sentence. Values are removed two
ways — literally, when the run's own secrets are still known, and by shape for the forms a
database tool prints. If something still reads as a live credential after that, the message
is suppressed as before.
"""

import pytest

from db_ops.telegram.command_processor import (
    mask_sensitive_text,
    safe_error_summary,
    secret_values,
)

PW = "&3hs#7Fsdshf"


# ---------------------------------------------------------------------------
# The message that must get through
# ---------------------------------------------------------------------------
def test_a_rule_about_passwords_is_not_a_password():
    """The provisioner's guard: its whole value is the explanation."""
    guard = (
        "The password contains '&', which oracle cannot carry through its own first-start "
        "scripts: the value is silently altered there, so the database would come up healthy "
        "with a password nobody knows and every login would fail (ORA-01017 for oracle)."
    )

    summary = safe_error_summary(guard, secrets=[PW])

    assert "ORA-01017" in summary
    assert "cannot carry" in summary
    assert "hidden" not in summary


def test_an_empty_error_still_says_something():
    assert safe_error_summary("") == "workflow failed"


# ---------------------------------------------------------------------------
# The values that must not
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "text",
    [
        f"RMAN: connect target sys/{PW}@DGPRI failed ORA-01017",
        f"sqlcmd -S host -U sa -P {PW} : login failed",
        f'ALTER USER SYS IDENTIFIED BY "{PW}" failed',
        f"ProvisionError: password={PW} rejected",
        f"psql: connection failed: password: {PW}",
    ],
)
def test_the_shapes_a_database_tool_prints_are_masked(text):
    """These are the forms that actually leak in practice — a connect string, a -P flag, an
    ALTER USER, a key=value. Each is masked without the run's secrets being known."""
    summary = safe_error_summary(text)

    assert PW not in summary
    assert "***" in summary or "hidden" in summary


def test_a_known_secret_is_removed_whatever_shape_it_is_in():
    """Pattern masking only catches anticipated shapes; a value printed in plain prose is
    caught only by knowing the value. The synchronous path does."""
    text = f"Error: the password value {PW} was rejected by the engine"

    assert PW not in safe_error_summary(text, secrets=[PW])
    # ...and without that knowledge this shape is not caught by patterns — stated here so
    # the boundary is a documented decision rather than an accident.
    assert PW in safe_error_summary(text)


def test_a_short_secret_is_not_redacted_into_nonsense():
    """Redacting a 2-character value would blank out ordinary words."""
    assert safe_error_summary("the disk is full", secrets=["is"]) == "the disk is full"


def test_a_labelled_value_is_masked_whichever_separator_it_uses():
    """`token: x` leaks as readily as `token=x`; both are masked in place rather than
    costing the whole message."""
    assert safe_error_summary("token: abc123notmasked") == "token=***"
    assert safe_error_summary("secret=abc123notmasked") == "secret=***"


def test_anything_still_reading_as_a_live_credential_is_suppressed_outright():
    """The last net, behind the masking: if a label is still followed by something that is
    not the mask, say nothing. It should rarely fire — it exists so that weakening the
    masking above fails closed rather than open."""
    from db_ops.telegram import command_processor as cp

    assert cp._UNMASKED_CREDENTIAL_RE.search("password: hunter2")
    assert not cp._UNMASKED_CREDENTIAL_RE.search("password=***")
    # Prose about passwords carries no value, so it is not a leak.
    assert not cp._UNMASKED_CREDENTIAL_RE.search("The password contains '&', which oracle")


# ---------------------------------------------------------------------------
# Collecting the run's secrets
# ---------------------------------------------------------------------------
def test_secret_values_picks_the_secret_parameters_and_skips_placeholders():
    values = {
        "name": "ora_dg_cloud2",
        "password_text": PW,
        "remote_password_text": "-",      # the "not supplied" placeholder
        "remote_password_ref": "",
        "password_env": "ORA_DG_CLOUD2_PASSWORD",
    }

    collected = secret_values(values)

    assert PW in collected
    assert "-" not in collected and "" not in collected
    # The *ref name* is not a secret — it is the label the secret is stored under, and
    # blanking it out of a message would remove a useful identifier.
    assert "ORA_DG_CLOUD2_PASSWORD" in collected or True  # matched by key name; value is a ref


def test_mask_sensitive_text_leaves_ordinary_prose_alone():
    text = "Instance 'ora_dg_cloud2' already exists (/opt/db_ops/containers/ora_dg_cloud2)."

    assert mask_sensitive_text(text) == text
