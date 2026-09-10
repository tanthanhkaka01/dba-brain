"""Back, Skip and Cancel are one state machine, not a word each command checks for itself.

The report that produced this module: a 14-step `/spbot_create_db_docker` run in which two of the
three credential answers were `-` — after the operator had already given the secret ref the first
question asked for — and in which a mistyped answer at step 4 could not be taken back at all.

What these tests hold down is the part that is easy to get subtly wrong:

* **Back walks the ask history, not `position - 1`.** With branching, some positions are never
  asked; counting backwards re-asks a question this run excluded and then treats its answer as
  meaningful.
* **Skip appears only where skipping is real.** A Skip button on a step that must hold a value is
  the `|-` defect with a nicer surface.
* **A branch that is re-chosen forgets the branch it replaced.** Answer `password`, give one, go
  back, choose `key_file`, and the password must not still be in the args the CLI receives.
"""

from __future__ import annotations

import pytest

from db_ops.lib import workflow_steps as ws


def step(**overrides) -> dict:
    base = {"name": "thing", "position": 1, "required": True, "prompt_text": "Thing?"}
    base.update(overrides)
    return base


# --------------------------------------------------------------------------- #
# Control words
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("text", ["back", "Back", "  BACK  ", "/back", "⬅️ Back"])
def test_back_is_recognised_however_it_arrives(text) -> None:
    """Typed, shouted, slash-prefixed, or tapped as the button — one vocabulary."""
    control = ws.resolve_control(text)
    assert control is not None and control.is_back


def test_the_button_labels_and_the_typed_words_are_the_same_table() -> None:
    for label, expected in ((ws.BACK_LABEL, "back"), (ws.SKIP_LABEL, "skip"),
                            (ws.CANCEL_LABEL, "cancel")):
        control = ws.resolve_control(label)
        assert control is not None and control.kind == expected, label


def test_an_ordinary_answer_is_not_a_control_word() -> None:
    assert ws.resolve_control("REMOTE_192_0_2_115_DEV") is None
    assert ws.resolve_control("") is None


def test_a_step_that_offers_cancel_as_an_answer_keeps_it_as_an_answer() -> None:
    """Otherwise a workflow could never ask "cancel or continue?" — the word would be eaten."""
    asking = step(options=[{"label": "Cancel it", "value": "cancel"},
                           {"label": "Continue", "value": "continue"}])
    assert ws.resolve_control("cancel", asking) is None
    assert ws.resolve_control("cancel") is not None


# --------------------------------------------------------------------------- #
# Skip
# --------------------------------------------------------------------------- #
def test_a_required_step_is_not_skippable(step_=None) -> None:
    assert ws.is_skippable(step(required=True)) is False


def test_an_optional_step_is_skippable() -> None:
    assert ws.is_skippable(step(required=False)) is True


def test_allow_skip_overrides_required_in_both_directions() -> None:
    """`required` means two things at once; `allow_skip` is how a branch separates them."""
    assert ws.is_skippable(step(required=True, allow_skip=True)) is True
    assert ws.is_skippable(step(required=False, allow_skip=False)) is False


def test_a_skipped_step_stores_the_dash_every_cli_already_understands() -> None:
    assert ws.skip_value(step()) == "-"
    assert ws.skip_value(step(skip_value="")) == ""


# --------------------------------------------------------------------------- #
# Options
# --------------------------------------------------------------------------- #
def test_options_accept_both_the_long_and_the_short_spelling() -> None:
    assert ws.option_list(step(options=["yes", "no"])) == [
        {"label": "yes", "value": "yes"}, {"label": "no", "value": "no"}]
    assert ws.option_list(step(options=[{"label": "Yes", "value": "yes"}])) == [
        {"label": "Yes", "value": "yes"}]


def test_an_option_with_no_value_is_dropped_rather_than_rendered() -> None:
    """A button that cannot answer the question is worse than no button."""
    assert ws.option_list(step(options=[{"label": "Yes"}, {"value": "no"}])) == [
        {"label": "no", "value": "no"}]


# --------------------------------------------------------------------------- #
# Branching
# --------------------------------------------------------------------------- #
SSH_STEPS = [
    {"name": "deploy_target", "position": 1, "required": True},
    {"name": "remote_auth", "position": 2, "required": True,
     "ask_when": {"parameter": "deploy_target", "not_equals": "worker"},
     "options": [{"label": "Secret ref", "value": "secret_ref"},
                 {"label": "Password", "value": "password"},
                 {"label": "SSH key", "value": "key_file"}]},
    {"name": "remote_password_ref", "position": 3, "required": True,
     "ask_when": {"parameter": "remote_auth", "equals": "secret_ref"}},
    {"name": "remote_password_text", "position": 4, "required": True, "secret": True,
     "ask_when": {"parameter": "remote_auth", "equals": "password"}},
    {"name": "remote_key_name", "position": 5, "required": True,
     "ask_when": {"parameter": "remote_auth", "equals": "key_file"}},
]


def asked(args: list[str]) -> list[str]:
    answers = ws.answers_by_name(SSH_STEPS, args)
    return [str(item["name"]) for item in SSH_STEPS if ws.ask_when_holds(item, answers)]


def test_deploying_to_the_worker_asks_no_ssh_question_at_all() -> None:
    assert asked(["worker", "", "", "", ""]) == ["deploy_target"]


def test_choosing_a_secret_ref_ends_the_credential_question() -> None:
    """The report in the plan: having given the ref, the operator was asked for a password too."""
    assert asked(["192.0.2.115", "secret_ref", "", "", ""]) == [
        "deploy_target", "remote_auth", "remote_password_ref"]


def test_choosing_a_key_file_asks_for_the_key_and_nothing_else() -> None:
    assert asked(["192.0.2.115", "key_file", "", "", ""]) == [
        "deploy_target", "remote_auth", "remote_key_name"]


def test_a_branch_whose_condition_is_not_answered_yet_is_not_asked_yet() -> None:
    """Asking a branch's questions before the branch is chosen is the bug ask_when removes."""
    assert asked(["192.0.2.115", "", "", "", ""]) == ["deploy_target", "remote_auth"]


def test_a_rule_this_build_does_not_understand_asks_the_question() -> None:
    """Silently not asking hides a required step; the run then fails at the CLI instead."""
    odd = {"name": "x", "position": 1, "ask_when": {"parameter": "y", "matches": "^a"}}
    assert ws.ask_when_holds(odd, {"y": "anything"}) is True


def test_re_choosing_a_branch_forgets_the_branch_it_replaced() -> None:
    """Back, then a different credential type: the abandoned password must not reach the CLI."""
    args = ["192.0.2.115", "key_file", "", "hunter2-was-typed-earlier", ""]
    assert ws.clear_unreachable_answers(SSH_STEPS, args) == [
        "192.0.2.115", "key_file", "", "", ""]


# --------------------------------------------------------------------------- #
# Keyboard
# --------------------------------------------------------------------------- #
def test_the_first_step_offers_cancel_but_not_back() -> None:
    keyboard = ws.keyboard_for(step(), can_go_back=False)
    assert keyboard["keyboard"] == [[{"text": ws.CANCEL_LABEL}]]
    assert keyboard["selective"] is True and keyboard["one_time_keyboard"] is True


def test_a_required_choice_step_offers_its_options_back_and_cancel_but_no_skip() -> None:
    keyboard = ws.keyboard_for(
        step(required=True, options=["yes", "no"]), can_go_back=True)
    assert keyboard["keyboard"] == [
        [{"text": "yes"}, {"text": "no"}],
        [{"text": ws.BACK_LABEL}, {"text": ws.CANCEL_LABEL}],
    ]


def test_an_optional_step_offers_skip() -> None:
    keyboard = ws.keyboard_for(step(required=False), can_go_back=True)
    assert keyboard["keyboard"] == [
        [{"text": ws.BACK_LABEL}, {"text": ws.SKIP_LABEL}, {"text": ws.CANCEL_LABEL}]]


def test_options_are_laid_out_two_to_a_row() -> None:
    """An IP or a secret ref three-up is unreadable on a phone."""
    keyboard = ws.keyboard_for(step(options=["a", "b", "c"]), can_go_back=False)
    assert keyboard["keyboard"][:2] == [[{"text": "a"}, {"text": "b"}], [{"text": "c"}]]


def test_a_secret_step_never_puts_its_values_on_a_button() -> None:
    """The buttons stay in the chat; a password on one is a password in the transcript."""
    keyboard = ws.keyboard_for(step(secret=True, options=["hunter2"]), can_go_back=True)
    assert keyboard["keyboard"] == [[{"text": ws.BACK_LABEL}, {"text": ws.CANCEL_LABEL}]]


def test_the_hint_names_exactly_the_words_that_work_right_now() -> None:
    assert ws.control_hint(step(required=True), can_go_back=False) == "Type cancel at any point."
    assert ws.control_hint(step(required=False), can_go_back=True) == (
        "Type back / skip / cancel at any point.")
