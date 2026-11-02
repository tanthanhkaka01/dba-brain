"""One question to the assistant: the model answers, asks for tools, reads what they give, and
answers again, until it is done or a limit stops it (0.34.0, 1.259).

petweb's ``src/modules/ai/agent.ts`` (``runAgent``) in Python. The call to the model comes in as a
function returning a plain response dict, so the tests run the loop against a stand-in that costs
nothing; :func:`dbabrain.telegram.ai_assist.sdk_call` is the real one.

**Tools run here.** The model never reaches dbabrain: it returns ``tool_use`` blocks in its answer
to our request, the tool runs on this node, and the result goes up in our next request. Every
exchange is opened from this side - the worker reaches the internet and nothing reaches it.

The limits are checked before every turn: the tokens (each turn resends the whole conversation, so
they add up), the money, the turns, the run's own time. **A tool call always gets a result** - run,
refused, or not run - so the conversation stays one the API accepts next time.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from dbabrain.lib import ai_assist as A

#: One call to the model: the request parameters in, a response dict out - ``id``, ``model``,
#: ``content`` (blocks as dicts), ``stop_reason``, ``stop_details``, ``usage``, ``request_id``.
ModelCall = Callable[[dict[str, Any]], dict[str, Any]]
#: One tool: (name, input, tool_use_id, ai_api_call_id) -> (content, is_error).
ToolRun = Callable[[str, Any, str, "int | None"], "tuple[str, bool]"]
#: Told of every call, whatever happened: (turn_no, request meta, response or None, usage, cost,
#: price_known, duration_ms, error text) -> the ai_api_call_id kept for it.
CallRecorder = Callable[[int, dict[str, Any], "dict[str, Any] | None", A.Usage, float, bool, int, str], "int | None"]


@dataclass(frozen=True)
class Limits:
    max_tokens: int
    max_cost: float
    max_tool_calls: int
    max_turns: int
    max_output_tokens_turn: int
    min_tokens_turn: int
    deadline: float | None = None  # time.monotonic() after which no turn starts


@dataclass
class AgentResult:
    added: list[dict[str, Any]] = field(default_factory=list)
    usage: A.Usage = field(default_factory=A.Usage)
    cost: float = 0.0
    turns: int = 0
    tool_calls: int = 0
    tools_used: list[str] = field(default_factory=list)
    stop_reason: str = "end_turn"
    stopped: str = ""  # tokens | money | turns | time | tool_calls | max_tokens | refusal | error | ''
    refusal: dict[str, Any] = field(default_factory=dict)
    price_known: bool = True
    error: str = ""

    @property
    def text(self) -> str:
        """The answer: the text of the last assistant message that has any."""
        for message in reversed(self.added):
            if message.get("role") != "assistant":
                continue
            parts = [block.get("text", "") for block in message.get("content") or []
                     if isinstance(block, dict) and block.get("type") == "text"]
            joined = "\n".join(part for part in parts if part).strip()
            if joined:
                return joined
        return ""


def _blocks(message: dict[str, Any]) -> list[dict[str, Any]]:
    content = message.get("content")
    return content if isinstance(content, list) else []


def repair_history(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """A kept conversation may end on a tool call with no result (the job died in between). The API
    refuses such a conversation, so the missing results are put back as errors before it is sent."""
    repaired: list[dict[str, Any]] = []
    for index, message in enumerate(messages):
        repaired.append(message)
        if message.get("role") != "assistant":
            continue
        uses = [block for block in _blocks(message) if block.get("type") == "tool_use"]
        if not uses:
            continue
        following = messages[index + 1] if index + 1 < len(messages) else None
        answered = {block.get("tool_use_id") for block in _blocks(following or {})
                    if following and following.get("role") == "user" and block.get("type") == "tool_result"}
        missing = [use for use in uses if use.get("id") not in answered]
        if missing and len(missing) == len(uses):
            repaired.append({"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": use.get("id"), "is_error": True,
                 "content": "Not run: the question stopped here."} for use in missing]})
    return repaired


def _usage_of(response: dict[str, Any]) -> A.Usage:
    usage = response.get("usage") or {}
    return A.Usage(input=int(usage.get("input_tokens") or 0), output=int(usage.get("output_tokens") or 0),
                   cache_read=int(usage.get("cache_read_input_tokens") or 0),
                   cache_write=int(usage.get("cache_creation_input_tokens") or 0))


def estimate_tokens(*parts: Any) -> int:
    """A count before the call, for planning the first turn (JSON runs about 3.5 characters a token)."""
    return int(len(json.dumps(parts, ensure_ascii=False, default=str)) / 3.5) + 1


def run_agent(*, call: ModelCall, model: str, effort: str, system: str, tools: list[dict[str, Any]],
              history: list[dict[str, Any]], question: dict[str, Any], limits: Limits,
              prices: dict[str, A.Price], run_tool: ToolRun, record_call: CallRecorder,
              first_input: int | None = None, clock: Callable[[], float] = time.monotonic) -> AgentResult:
    messages = [*history, question]
    result = AgentResult(added=[question])
    next_input = first_input if first_input is not None else estimate_tokens(system, tools, messages)
    tools_left = True

    price, _known = A.price_for(model, prices)
    while True:
        left = limits.max_tokens - result.usage.tokens
        max_output = min(limits.max_output_tokens_turn, left - next_input)
        if max_output < limits.min_tokens_turn:
            result.stopped = "tokens"
            break
        # The money a turn may spend, at its worst: the next input uncached, every output token
        # used. The output is cut to what is left, so the question never runs past its cap.
        money_left = limits.max_cost - result.cost - next_input * max(price.input, price.long_input or 0) / 1e6
        output_rate = max(price.output, price.long_output or 0) / 1e6
        if output_rate:
            max_output = min(max_output, int(money_left / output_rate))
        if result.cost >= limits.max_cost or max_output < limits.min_tokens_turn:
            result.stopped = "money"
            break
        if result.turns >= limits.max_turns:
            result.stopped = "turns"
            break
        if limits.deadline is not None and clock() >= limits.deadline:
            result.stopped = "time"
            break
        result.turns += 1
        params: dict[str, Any] = {
            "model": model,
            "max_tokens": int(max_output),
            # Set, not left to each model's default, so that a model change does not change the spend.
            "output_config": {"effort": effort},
            "system": system,
            # A copy: the list grows after the call, and what was sent must stay what was sent.
            "messages": list(messages),
            # The tools, the system prompt and the conversation so far are cached.
            "cache_control": {"type": "ephemeral"},
        }
        if tools:
            params["tools"] = tools
            if not tools_left:
                # The tool calls are used up: the model answers with what it has.
                params["tool_choice"] = {"type": "none"}
        meta = {"model": model, "max_tokens": int(max_output), "effort": effort, "messages": len(messages),
                "tools": [tool.get("name") for tool in tools], "tool_choice": params.get("tool_choice")}
        started = clock()
        try:
            response = call(params)
        except Exception as exc:  # noqa: BLE001 - kept, and the question ends on it with what it reached.
            result.error = f"{type(exc).__name__}: {exc}"
            record_call(result.turns, meta, None, A.Usage(), 0.0, True, int((clock() - started) * 1000),
                        result.error)
            result.stopped = "error"
            break
        duration = int((clock() - started) * 1000)
        turn_usage = _usage_of(response)
        served = str(response.get("model") or model)
        cost, known = A.cost_of(served, turn_usage, prices)
        result.price_known = result.price_known and known
        result.usage.add(turn_usage)
        result.cost += cost
        result.stop_reason = str(response.get("stop_reason") or "end_turn")
        call_id = record_call(result.turns, meta, response, turn_usage, cost, known, duration, "")

        answer = {"role": "assistant", "content": list(response.get("content") or [])}
        messages.append(answer)
        result.added.append(answer)
        if result.stop_reason == "refusal":
            # Answered as the model declined it - the operator's word: no fallback to another model.
            result.stopped = "refusal"
            result.refusal = dict(response.get("stop_details") or {})
        uses = [block for block in answer["content"] if isinstance(block, dict) and block.get("type") == "tool_use"]
        if result.stop_reason == "pause_turn" and not uses:
            next_input = turn_usage.input + turn_usage.cache_read + turn_usage.cache_write + turn_usage.output
            continue
        if not uses:
            if result.stop_reason == "max_tokens":
                result.stopped = "max_tokens"
            break

        # A turn cut off (max_tokens) or refused may hold half a tool call: it is not run, but
        # answered, so that the conversation stays valid.
        runnable = result.stop_reason == "tool_use"
        results: list[dict[str, Any]] = []
        for use in uses:
            tool_use_id = str(use.get("id"))
            if not runnable:
                results.append({"type": "tool_result", "tool_use_id": tool_use_id, "is_error": True,
                                "content": "Not run: the answer was cut off."})
                continue
            if result.tool_calls >= limits.max_tool_calls:
                results.append({"type": "tool_result", "tool_use_id": tool_use_id, "is_error": True,
                                "content": "Not run: no tool calls left for this question. Answer with the data you have."})
                continue
            result.tool_calls += 1
            name = str(use.get("name"))
            result.tools_used.append(name)
            content, is_error = run_tool(name, use.get("input"), tool_use_id, call_id)
            entry: dict[str, Any] = {"type": "tool_result", "tool_use_id": tool_use_id, "content": content}
            if is_error:
                entry["is_error"] = True
            results.append(entry)
        reply = {"role": "user", "content": results}
        messages.append(reply)
        result.added.append(reply)
        if not runnable:
            result.stopped = result.stopped or "max_tokens"
            break
        if result.tool_calls >= limits.max_tool_calls:
            if not tools_left:
                result.stopped = "tool_calls"
                break
            tools_left = False
        # The next turn resends all of this, with the results.
        next_input = (turn_usage.input + turn_usage.cache_read + turn_usage.cache_write + turn_usage.output
                      + estimate_tokens(results))
    return result
