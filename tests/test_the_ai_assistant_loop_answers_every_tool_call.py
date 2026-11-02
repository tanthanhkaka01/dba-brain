"""The assistant's loop (0.34.0, 1.259): petweb's ``runAgent`` in Python.

Run against a stand-in for the model, so nothing here costs anything. What is held: every
``tool_use`` gets a ``tool_result`` (the API refuses a conversation that leaves one unanswered), the
limits stop a question before a turn rather than after it, a refusal is answered as the model gave
it, and a failure keeps every message the question reached.
"""

from __future__ import annotations

from dbabrain.lib import ai_assist as A
from dbabrain.telegram import ai_agent

PRICES = A.parse_ai_assist(A.shipped_document()).prices
QUESTION = {"role": "user", "content": [{"type": "text", "text": "how are the backups?"}]}


def _limits(**changes):
    values = dict(max_tokens=200_000, max_cost=1.0, max_tool_calls=5, max_turns=10, max_output_tokens_turn=16000,
                  min_tokens_turn=1500)
    values.update(changes)
    return ai_agent.Limits(**values)


def _response(content, stop="end_turn", usage=(100, 50)):
    return {"id": "msg", "model": "claude-sonnet-5-5", "content": content, "stop_reason": stop,
            "usage": {"input_tokens": usage[0], "output_tokens": usage[1]}, "request_id": "req_1"}


def _tool_use(tool_id, name="store_query"):
    return {"type": "tool_use", "id": tool_id, "name": name, "input": {"sql": "SELECT 1"}}


class Script:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.params = []

    def __call__(self, params):
        self.params.append(params)
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def _run(script, limits=None, history=None):
    calls, tools = [], []

    def record(turn, meta, response, usage, cost, known, duration, error):
        calls.append((turn, response, error))
        return turn

    def run_tool(name, tool_input, tool_use_id, call_id):
        tools.append((name, tool_use_id, call_id))
        return f"result of {tool_use_id}", False

    result = ai_agent.run_agent(call=script, model="claude-sonnet-5-5", effort="medium", system="rules",
                                tools=[{"name": "store_query"}], history=history or [], question=QUESTION,
                                limits=limits or _limits(), prices=PRICES, run_tool=run_tool, record_call=record)
    return result, calls, tools


def test_a_tool_call_is_run_here_and_its_result_sent_back():
    script = Script(_response([_tool_use("t1")], stop="tool_use"), _response([{"type": "text", "text": "all good"}]))
    result, calls, tools = _run(script)
    assert result.text == "all good"
    assert tools == [("store_query", "t1", 1)]
    sent = script.params[1]["messages"][-1]
    assert sent["role"] == "user" and sent["content"][0]["tool_use_id"] == "t1"
    assert [turn for turn, _response, _error in calls] == [1, 2]
    assert result.turns == 2 and result.tool_calls == 1


def test_every_request_states_effort_and_caching():
    script = Script(_response([{"type": "text", "text": "x"}]))
    _run(script)
    assert script.params[0]["output_config"] == {"effort": "medium"}
    assert script.params[0]["cache_control"] == {"type": "ephemeral"}


def test_when_the_tool_calls_run_out_the_model_answers_with_what_it_has():
    script = Script(_response([_tool_use("t1"), _tool_use("t2")], stop="tool_use"),
                    _response([{"type": "text", "text": "done"}]))
    result, _calls, tools = _run(script, _limits(max_tool_calls=1))
    assert len(tools) == 1
    second = script.params[1]
    assert second["tool_choice"] == {"type": "none"}
    refused = second["messages"][-1]["content"][1]
    assert refused["is_error"] and "no tool calls left" in refused["content"]
    assert result.text == "done"


def test_a_refusal_is_answered_as_the_model_gave_it():
    script = Script({**_response([], stop="refusal"), "stop_details": {"category": "cyber", "explanation": "no"}})
    result, _calls, _tools = _run(script)
    assert result.stopped == "refusal"
    assert result.refusal["category"] == "cyber"
    assert len(script.params) == 1, "no second model is asked"


def test_a_tool_call_in_a_cut_off_answer_is_answered_but_not_run():
    script = Script(_response([_tool_use("t1")], stop="max_tokens"))
    result, _calls, tools = _run(script)
    assert tools == []
    assert result.added[-1]["content"][0]["content"] == "Not run: the answer was cut off."
    assert result.stopped == "max_tokens"


def test_a_failed_call_keeps_what_the_question_reached():
    script = Script(_response([_tool_use("t1")], stop="tool_use"), RuntimeError("overloaded"))
    result, calls, _tools = _run(script)
    assert result.stopped == "error" and "overloaded" in result.error
    assert [message["role"] for message in result.added] == ["user", "assistant", "user"]
    assert calls[-1][2].startswith("RuntimeError")


def test_no_turn_starts_without_the_tokens_for_one():
    script = Script()
    result, _calls, _tools = _run(script, _limits(max_tokens=1000))
    assert result.stopped == "tokens" and script.params == []


def test_no_turn_starts_once_the_money_is_spent():
    script = Script(_response([_tool_use("t1")], stop="tool_use", usage=(1_000_000, 10)))
    result, _calls, _tools = _run(script, _limits(max_cost=1.0, max_tokens=10_000_000))
    assert result.stopped == "money"
    assert result.cost >= 1.0


def test_a_conversation_left_on_an_unanswered_tool_call_is_repaired():
    history = [QUESTION, {"role": "assistant", "content": [_tool_use("t9")]}]
    repaired = ai_agent.repair_history(history)
    assert repaired[-1]["role"] == "user"
    assert repaired[-1]["content"][0] == {"type": "tool_result", "tool_use_id": "t9", "is_error": True,
                                          "content": "Not run: the question stopped here."}


def test_cost_and_usage_add_up_over_the_turns():
    script = Script(_response([_tool_use("t1")], stop="tool_use", usage=(1000, 100)),
                    _response([{"type": "text", "text": "x"}], usage=(2000, 200)))
    result, _calls, _tools = _run(script)
    assert result.usage.input == 3000 and result.usage.output == 300
    assert abs(result.cost - (3000 * 2 + 300 * 10) / 1e6) < 1e-12


def test_a_long_cached_conversation_is_held_by_its_money_not_cut_by_a_token_guess():
    """Found by the review of 2026-10-31: a question's tokens were its dollars over the dearest
    token, so 300k cached input tokens - a few cents - stopped it on 'tokens'."""
    big = (2_000, 400)
    script = Script({**_response([_tool_use("t1")], stop="tool_use", usage=big),
                     "usage": {"input_tokens": 2_000, "output_tokens": 400, "cache_read_input_tokens": 120_000}},
                    {**_response([{"type": "text", "text": "done"}]),
                     "usage": {"input_tokens": 500, "output_tokens": 300, "cache_read_input_tokens": 122_000}})
    result, _calls, _tools = _run(script, _limits(max_tokens=300_000, max_cost=0.5))
    assert result.stopped == "" and result.text == "done"
    assert result.cost < 0.5


def test_a_turn_is_cut_to_the_money_left():
    script = Script(_response([{"type": "text", "text": "x"}]))
    _run(script, _limits(max_cost=0.05))
    # $0.05 less the first input at $2/M leaves room for under 5,000 output tokens at $10/M.
    assert script.params[0]["max_tokens"] < 5_000
