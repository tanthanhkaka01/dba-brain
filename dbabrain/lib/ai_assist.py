"""The AI assistant's settings and rules: what ``data/ai_assist.json`` must state, what a token
costs, and whether a question may go and with how much (0.34.0, 1.256 / 1.258).

The operator, 2026-10-31: *like petweb - set for the whole system and per (Telegram) user, a JSON
config, in the shared object reference, every field mandatory; by default a month is $10 for the
system and $5 for a user; refuse duplicates; refuse questions that come too fast.* petweb's
``src/modules/ai/policy.ts`` is the model: limits per level checked against what was used, a
question's place held before it runs, the price of each model a table.

Pure - values and rules, no store and no network (``lib``'s contract), so the tests decide a
question without a database. The store sums what was used (``db.ai_assist_store``); the Telegram
app asks :func:`decide` before the first call to the model.

**Every field is mandatory (R50).** A missing field is refused by name where the file is read, and
``upgrade-config`` writes :func:`shipped_document`'s value into a node's file, so a node never runs
on a number nobody stated. No limit, price or timeout lives in code.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

from dbabrain.lib import errors

FILE_NAME = "ai_assist.json"
#: The level the shipped file names: the chat the assistant answers in is a group whose
#: notify_level is this (telegram_groups.json), as every other app names its chat - never a list of
#: ids here that a supergroup move leaves pointing at a chat nobody writes in (the operator, 2026-10-31).
AI_ASSIST_LEVEL = "ai_assist"
#: The field this file had until then; upgrade-config gives its chats the level instead.
RETIRED_CHAT_IDS = "chat_ids"

#: ``output_config.effort`` of the Messages API.
EFFORTS = ("low", "medium", "high", "xhigh", "max")
#: What the assistant may do with dbabrain's tools. ``read_only`` is all 0.34.0 builds: the
#: operator, 2026-10-31 - *can be switched; read only for now; the AI proposes a Telegram command and
#: the user runs it (later: the user confirms and it runs)*. A mode that runs a command after a
#: confirmation is added with the code that does it, never before.
TOOLS_MODES = ("off", "read_only")


class AiAssistConfigError(errors.InvalidConfig):
    """``ai_assist.json`` is missing a field or states one the assistant cannot use."""


# --------------------------------------------------------------------------- #
# The fields
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class FieldSpec:
    name: str
    kind: str  # bool | str | str_list | int | int_or_null | number | number_or_null | enum
    shipped: Any
    minimum: float | None = None
    maximum: float | None = None
    choices: tuple[str, ...] = ()
    purpose: str = ""


def _f(name: str, kind: str, shipped: Any, purpose: str, *, minimum: float | None = None,
       maximum: float | None = None, choices: tuple[str, ...] = ()) -> FieldSpec:
    return FieldSpec(name, kind, shipped, minimum, maximum, choices, purpose)


SETTINGS_FIELDS: tuple[FieldSpec, ...] = (
    _f("enabled", "bool", False, "Whether the assistant answers at all; off, the job says not configured and skips."),
    _f("api_key_ref", "str", "", "The name of the Anthropic API key in the encrypted secret store; empty = not configured."),
    _f("telegram_chat", "str", AI_ASSIST_LEVEL,
       "The notify level whose chat the assistant answers in: the active group in telegram_groups.json "
       "with this notify_level (or level_chat_map's entry), as every app's telegram_chat names its chat."),
    _f("model", "str", "claude-sonnet-5-5", "The model asked. It must have a row in prices, or no limit could be held."),
    _f("effort", "enum", "medium", "output_config.effort, set so a model change does not change the spend.",
       choices=EFFORTS),
    _f("require_mention", "bool", True,
       "Answer only a message that @mentions the bot (or replies to the assistant, below); the rest of "
       "the chat is people talking to each other, and is neither answered nor kept."),
    _f("reply_to_assistant_is_mention", "bool", True,
       "A reply to one of the assistant's own messages counts as addressed to it - continuing a "
       "conversation needs no @ each time."),
    _f("tools_mode", "enum", "read_only",
       "What the assistant may do with dbabrain's tools: off (answers from what it knows) or read_only.",
       choices=TOOLS_MODES),
    _f("budget_usd_month", "number_or_null", 10.0, "The whole system's money per calendar month (config timezone); null = none.",
       minimum=0),
    _f("budget_usd_day", "number_or_null", None, "The whole system's money per day; null = none.", minimum=0),
    _f("tokens_month", "int_or_null", None, "The whole system's tokens per month; null = none.", minimum=0),
    _f("tokens_day", "int_or_null", None, "The whole system's tokens per day; null = none.", minimum=0),
    _f("requests_day", "int_or_null", None, "The whole system's questions per day; null = none.", minimum=0),
    _f("cooldown_seconds", "int", 5, "One user's wait between two questions.", minimum=0),
    _f("rate_window_seconds", "int", 600, "The window rate_window_max_requests counts one user's questions in.",
       minimum=1),
    _f("rate_window_max_requests", "int", 20, "One user's questions inside rate_window_seconds, at most.", minimum=1),
    _f("duplicate_window_seconds", "int", 300,
       "The same text from the same user inside this many seconds is a duplicate and is not run again.", minimum=0),
    _f("max_tokens_question", "int", 300000, "All turns of one question: input, output and cache tokens.",
       minimum=4000),
    _f("max_cost_usd_question", "number", 0.5, "One question's money, at most; 0 runs nothing.", minimum=0),
    _f("max_tool_calls_question", "int", 20, "Tool calls of one question; then the model answers with what it has.",
       minimum=0),
    _f("max_turns_question", "int", 25, "Calls to the model for one question.", minimum=1),
    _f("max_output_tokens_turn", "int", 16000, "max_tokens of each call.", minimum=1024, maximum=128000),
    _f("min_tokens_turn", "int", 1500, "A turn with fewer tokens left than this is not started.", minimum=256),
    _f("conversation_idle_seconds", "int", 3600, "A message after this long starts a new conversation.", minimum=60),
    _f("max_history_questions", "int", 30, "A conversation with this many questions starts over (the cost of resending it).",
       minimum=1),
    _f("api_timeout_seconds", "int", 600, "The SDK's timeout for one call.", minimum=10),
    _f("api_max_retries", "int", 2, "The SDK's retries of a 429, a 5xx or a connection error.", minimum=0),
    _f("run_budget_seconds", "int", 840, "One run of the job; no new question is started after it. Below the app command's timeout.",
       minimum=30),
    _f("stale_running_seconds", "int", 1800, "A question still running after this long is closed as an error.",
       minimum=60),
    _f("max_message_age_seconds", "int", 900, "An older message is not answered (a job that was off does not answer yesterday).",
       minimum=10),
    _f("questions_per_run", "int", 5, "Questions one run of the job takes, one at a time.", minimum=1),
    _f("ack_message", "bool", True, "Queue a short 'working on it' reply when a question starts."),
    _f("show_cost_footer", "bool", True, "End each answer with the model, the tokens, the cost and the tools used."),
    _f("reply_chunk_chars", "int", 3900, "One Telegram message of an answer, at most (Telegram's limit is 4096).",
       minimum=500, maximum=4000),
    _f("attachments_enabled", "bool", True, "Read a photo (as an image) or a text document sent with a question."),
    _f("attachment_max_bytes", "int", 5000000, "One attachment, at most.", minimum=1),
    _f("http_proxy", "str", "", "An egress proxy for the API, if the node needs one; empty = none."),
    _f("reply_language", "str", "auto",
       "The language of every answer: auto = the language of the question being answered, whatever earlier "
       "turns, the context or a tool result were in; or a language's name (e.g. Vietnamese, English)."),
    _f("system_prompt_extra", "str", "", "The operator's own lines, appended to the assistant's rules."),
    _f("tool_result_max_chars", "int", 30000, "One tool result sent to the model, at most; a longer one is cut and says so.",
       minimum=1000),
    _f("tool_kept_output_chars", "int", 200000, "One tool result kept in the store, at most.", minimum=1000),
    _f("store_query_enabled", "bool", True, "The store_schema and store_query tools."),
    _f("store_query_max_rows", "int", 200, "Rows store_query returns, at most.", minimum=1),
    _f("store_query_timeout_seconds", "int", 30, "store_query's statement timeout.", minimum=1),
    _f("store_query_blocked_tables", "str_list", ["web_users", "web_sessions", "web_login_attempts"],
       "Store tables no tool reads."),
    _f("files_enabled", "bool", True, "The list_files, read_file and grep_files tools."),
    _f("file_roots", "str_list", ["data", "assets", "logs", "runtime/output", "dbabrain", "config.json"],
       "The tool root's folders and files the file tools may read, relative to the tool root."),
    # Every spelling of the secret store: the encrypted one, the plaintext source wherever a node
    # keeps it (data/secret_text.json is still a resolvable path), and an archived copy.
    _f("file_denied", "str_list", ["*secret_text*.json", "**/secrets/**", "**/ssh_keys/**", "*.pem",
                                   "*.key", "*.pfx", "*.p12", ".env", "*.env", "*.sqlite", "*.lock", "*.pyc"],
       "Patterns no file tool reads, whatever the roots say."),
    _f("file_max_chars", "int", 30000, "read_file's text, at most.", minimum=1000),
    _f("grep_max_matches", "int", 200, "grep_files' matches, at most.", minimum=1),
    _f("grep_max_file_bytes", "int", 20000000, "A file larger than this is not searched by grep_files.", minimum=1),
    _f("run_sql_enabled", "bool", True, "The run_sql tool: a read-only SELECT on a monitored server."),
    _f("run_sql_server_ids", "str_list", [], "The server_ids run_sql may reach; empty = every server with a database."),
    _f("run_sql_credential_name", "str", "", "A read-only login for run_sql; empty = the instance's own credential."),
    _f("run_sql_max_rows", "int", 200, "Rows run_sql returns, at most.", minimum=1),
    _f("run_sql_timeout_seconds", "int", 60, "run_sql's connect and statement timeout.", minimum=1),
    _f("bot_commands_enabled", "bool", True,
       "The bot_commands tool: the Telegram commands the asker may run, so an answer can propose one."),
)

PRICE_FIELDS: tuple[FieldSpec, ...] = (
    _f("model", "str", "", "The model this row prices."),
    _f("input", "number", 0.0, "USD per million input tokens.", minimum=0),
    _f("output", "number", 0.0, "USD per million output tokens.", minimum=0),
    _f("cache_write", "number", 0.0, "USD per million tokens written to the prompt cache.", minimum=0),
    _f("cache_read", "number", 0.0, "USD per million tokens read from the prompt cache.", minimum=0),
    _f("long_prompt_from", "int_or_null", None,
       "A prompt longer than this many tokens is priced at long_input / long_output; null = one price.", minimum=1),
    _f("long_input", "number_or_null", None, "USD per million input tokens of a long prompt.", minimum=0),
    _f("long_output", "number_or_null", None, "USD per million output tokens of a long prompt.", minimum=0),
)

USER_FIELDS: tuple[FieldSpec, ...] = (
    _f("telegram_user_id", "str", "", "The Telegram user id (telegram_users.json's user_id)."),
    _f("name", "str", "", "Who this is, for a reader of the file and of the usage report."),
    _f("active", "bool", False,
       "Whether this user may ask. Off unless stated: a user is switched on in this list, by name, never by default."),
    _f("budget_usd_month", "number_or_null", 5.0, "This user's money per calendar month; null = none.", minimum=0),
    _f("budget_usd_day", "number_or_null", None, "This user's money per day; null = none.", minimum=0),
    _f("tokens_month", "int_or_null", None, "This user's tokens per month; null = none.", minimum=0),
    _f("tokens_day", "int_or_null", None, "This user's tokens per day; null = none.", minimum=0),
    _f("requests_day", "int_or_null", None, "This user's questions per day; null = none.", minimum=0),
)

#: The prices shipped (claude-api skill, cached 2026-10-06). Cache write is 1.25 x input (5-minute
#: cache), cache read as each model's table states. Haiku 5.5 charges more above 100k tokens.
SHIPPED_PRICES: tuple[dict[str, Any], ...] = (
    {"model": "claude-sonnet-5-5", "input": 2.0, "output": 10.0, "cache_write": 2.5, "cache_read": 0.2,
     "long_prompt_from": None, "long_input": None, "long_output": None},
    {"model": "claude-opus-5-5", "input": 4.0, "output": 20.0, "cache_write": 5.0, "cache_read": 0.2,
     "long_prompt_from": None, "long_input": None, "long_output": None},
    {"model": "claude-haiku-5-5", "input": 0.1, "output": 0.5, "cache_write": 0.125, "cache_read": 0.01,
     "long_prompt_from": 100000, "long_input": 0.5, "long_output": 2.5},
)


def shipped_document() -> dict[str, Any]:
    """What a node that states nothing gets: every field at its shipped value, no user, off."""
    document: dict[str, Any] = {spec.name: _copy(spec.shipped) for spec in SETTINGS_FIELDS}
    document["prices"] = [dict(row) for row in SHIPPED_PRICES]
    document["users"] = []
    return document


def shipped_user(telegram_user_id: str, name: str, *, active: bool) -> dict[str, Any]:
    """A user entry with every field stated - the operator's default of $5 a month."""
    entry = {spec.name: _copy(spec.shipped) for spec in USER_FIELDS}
    entry.update({"telegram_user_id": str(telegram_user_id), "name": str(name), "active": bool(active)})
    return entry


def _copy(value: Any) -> Any:
    return list(value) if isinstance(value, list) else value


def state_ai_assist(document: Any) -> list[str]:
    """Write every missing field into a node's ``ai_assist.json`` document, in place; return what
    was written (``upgrade-config``). A user or price row gets its missing fields too; nothing a
    node states is changed."""
    if not isinstance(document, dict):
        return []
    written: list[str] = []
    for spec in SETTINGS_FIELDS:
        if spec.name not in document:
            document[spec.name] = _copy(spec.shipped)
            written.append(spec.name)
    if "prices" not in document:
        document["prices"] = [dict(row) for row in SHIPPED_PRICES]
        written.append("prices")
    if "users" not in document:
        document["users"] = []
        written.append("users")
    for list_name, specs in (("prices", PRICE_FIELDS), ("users", USER_FIELDS)):
        for index, row in enumerate(document.get(list_name) or []):
            if not isinstance(row, dict):
                continue
            for spec in specs:
                if spec.name not in row:
                    row[spec.name] = _copy(spec.shipped)
                    written.append(f"{list_name}[{index}].{spec.name}")
    return written


# --------------------------------------------------------------------------- #
# Parsing
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class Price:
    model: str
    input: float
    output: float
    cache_write: float
    cache_read: float
    long_prompt_from: int | None
    long_input: float | None
    long_output: float | None


@dataclass(frozen=True)
class AiUser:
    telegram_user_id: str
    name: str
    active: bool
    budget_usd_month: float | None
    budget_usd_day: float | None
    tokens_month: int | None
    tokens_day: int | None
    requests_day: int | None


@dataclass(frozen=True)
class AiAssistSettings:
    values: dict[str, Any]
    prices: dict[str, Price]
    users: tuple[AiUser, ...]

    def __getattr__(self, name: str) -> Any:
        values = object.__getattribute__(self, "values")
        if name in values:
            return values[name]
        raise AttributeError(name)

    def user(self, telegram_user_id: str) -> AiUser | None:
        for user in self.users:
            if user.telegram_user_id == str(telegram_user_id):
                return user
        return None


def _check(spec: FieldSpec, value: Any, where: str) -> Any:
    kind = spec.kind
    nullable = kind.endswith("_or_null")
    if value is None and nullable:
        return None
    if kind == "bool":
        if not isinstance(value, bool):
            raise AiAssistConfigError(f"{where}{spec.name} must be true or false; got {value!r}.")
        return value
    if kind == "str":
        if not isinstance(value, str):
            raise AiAssistConfigError(f"{where}{spec.name} must be a string; got {value!r}.")
        return value
    if kind == "enum":
        if value not in spec.choices:
            raise AiAssistConfigError(f"{where}{spec.name} must be one of {', '.join(spec.choices)}; got {value!r}.")
        return value
    if kind == "str_list":
        if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
            raise AiAssistConfigError(f"{where}{spec.name} must be a list of strings; got {value!r}.")
        return [item.strip() for item in value if item.strip()]
    integer = kind.startswith("int")
    if isinstance(value, bool) or not isinstance(value, (int, float)) or (integer and not float(value).is_integer()):
        wanted = "a whole number" if integer else "a number"
        raise AiAssistConfigError(f"{where}{spec.name} must be {wanted}{' or null' if nullable else ''}; got {value!r}.")
    number = int(value) if integer else float(value)
    if spec.minimum is not None and number < spec.minimum:
        raise AiAssistConfigError(f"{where}{spec.name} must be at least {spec.minimum:g}; got {value!r}.")
    if spec.maximum is not None and number > spec.maximum:
        raise AiAssistConfigError(f"{where}{spec.name} must be at most {spec.maximum:g}; got {value!r}.")
    return number


def _parse_block(raw: Any, specs: tuple[FieldSpec, ...], where: str) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise AiAssistConfigError(f"{where.rstrip('.') or FILE_NAME} must be an object; got {type(raw).__name__}.")
    missing = [spec.name for spec in specs if spec.name not in raw]
    if missing:
        raise AiAssistConfigError(
            f"{FILE_NAME}: missing field(s) {', '.join(where + name for name in missing)} - every field is stated "
            f"(R50); `upgrade-config` writes the shipped values.")
    return {spec.name: _check(spec, raw[spec.name], where) for spec in specs}


def parse_ai_assist(raw: Any) -> AiAssistSettings:
    """``ai_assist.json`` as the assistant uses it, or :class:`AiAssistConfigError` naming the field."""
    values = _parse_block(raw, SETTINGS_FIELDS, "")
    prices: dict[str, Price] = {}
    for index, row in enumerate(raw.get("prices") if isinstance(raw.get("prices"), list) else []):
        parsed = _parse_block(row, PRICE_FIELDS, f"prices[{index}].")
        if not parsed["model"]:
            raise AiAssistConfigError(f"prices[{index}].model is empty.")
        if parsed["long_prompt_from"] is not None and (parsed["long_input"] is None or parsed["long_output"] is None):
            raise AiAssistConfigError(f"prices[{index}]: long_prompt_from needs long_input and long_output.")
        prices[parsed["model"]] = Price(**parsed)
    if not isinstance(raw.get("prices"), list):
        raise AiAssistConfigError("prices must be a list of price rows.")
    users: list[AiUser] = []
    if not isinstance(raw.get("users"), list):
        raise AiAssistConfigError("users must be a list of user entries.")
    seen: set[str] = set()
    for index, row in enumerate(raw["users"]):
        parsed = _parse_block(row, USER_FIELDS, f"users[{index}].")
        if not parsed["telegram_user_id"]:
            raise AiAssistConfigError(f"users[{index}].telegram_user_id is empty.")
        if parsed["telegram_user_id"] in seen:
            raise AiAssistConfigError(f"users[{index}].telegram_user_id {parsed['telegram_user_id']} is listed twice.")
        seen.add(parsed["telegram_user_id"])
        users.append(AiUser(**parsed))
    if values["model"] not in prices:
        raise AiAssistConfigError(
            f"model {values['model']!r} has no row in prices - its cost could not be counted against any limit.")
    return AiAssistSettings(values=values, prices=prices, users=tuple(users))


# --------------------------------------------------------------------------- #
# Cost
# --------------------------------------------------------------------------- #
@dataclass
class Usage:
    input: int = 0
    output: int = 0
    cache_read: int = 0
    cache_write: int = 0

    @property
    def tokens(self) -> int:
        return self.input + self.output + self.cache_read + self.cache_write

    def add(self, other: "Usage") -> None:
        self.input += other.input
        self.output += other.output
        self.cache_read += other.cache_read
        self.cache_write += other.cache_write


def price_for(model: str, prices: dict[str, Price]) -> tuple[Price, bool]:
    """The row that prices ``model``; a model with no row (a served model the table does not list)
    is priced at the dearest row, and the caller says so."""
    if model in prices:
        return prices[model], True
    # A dated or suffixed id of a listed model (claude-sonnet-5-5-20261001) is that model.
    for name, row in prices.items():
        if model.startswith(name):
            return row, True
    dearest = max(prices.values(), key=lambda row: (row.output, row.input))
    return dearest, False


def cost_of(model: str, usage: Usage, prices: dict[str, Price]) -> tuple[float, bool]:
    """USD for one call, and whether the model had a price of its own."""
    row, known = price_for(model, prices)
    input_price, output_price = row.input, row.output
    write_price, read_price = row.cache_write, row.cache_read
    prompt = usage.input + usage.cache_read + usage.cache_write
    if row.long_prompt_from is not None and prompt > row.long_prompt_from:
        factor = (row.long_input or row.input) / row.input if row.input else 1.0
        input_price, output_price = float(row.long_input), float(row.long_output)
        write_price, read_price = row.cache_write * factor, row.cache_read * factor
    cost = (usage.input * input_price + usage.output * output_price
            + usage.cache_write * write_price + usage.cache_read * read_price) / 1_000_000
    return cost, known


def worst_rate(model: str, prices: dict[str, Price]) -> float:
    """The dearest a token of ``model`` can be (an output token), to hold a question's money."""
    row, _known = price_for(model, prices)
    return max(row.output, row.long_output or 0.0) / 1_000_000


# --------------------------------------------------------------------------- #
# Limits
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class Periods:
    """The day and the month a moment falls in, in the config timezone, as UTC instants."""
    day_start: datetime
    next_day: datetime
    month_start: datetime
    next_month: datetime


def periods(now: datetime, zone: Any) -> Periods:
    local = now.astimezone(zone)
    day = local.replace(hour=0, minute=0, second=0, microsecond=0)
    month = day.replace(day=1)
    next_month = (month + timedelta(days=32)).replace(day=1)
    to_utc = lambda value: value.astimezone(timezone.utc)  # noqa: E731
    return Periods(to_utc(day), to_utc(day + timedelta(days=1)), to_utc(month), to_utc(next_month))


@dataclass
class Used:
    """What a level has used: today and this month (a running question counts what it holds)."""
    day_tokens: int = 0
    month_tokens: int = 0
    day_requests: int = 0
    day_cost: float = 0.0
    month_cost: float = 0.0


@dataclass
class Recent:
    """One user's recent questions, for the cooldown, the rate window and duplicates."""
    last_started_at: datetime | None = None
    window_requests: int = 0
    duplicate_of: int | None = None


@dataclass(frozen=True)
class Decision:
    ok: bool
    key: str = ""
    level: str = ""
    message: str = ""
    until: datetime | None = None
    tokens: int = 0
    cost_cap: float = 0.0
    details: dict[str, Any] = field(default_factory=dict)


def question_fingerprint(user_id: str, text: str) -> str:
    """The same question from the same user, whitespace and case aside - the duplicate key."""
    normalised = re.sub(r"\s+", " ", str(text or "")).strip().lower()
    return hashlib.sha256(f"{user_id}\n{normalised}".encode("utf-8")).hexdigest()


def _refuse(key: str, level: str, message: str, until: datetime | None = None, **details: Any) -> Decision:
    return Decision(ok=False, key=key, level=level, message=message, until=until, details=details)


def decide(settings: AiAssistSettings, user: AiUser, *, system_used: Used, user_used: Used, recent: Recent,
           now: datetime, period: Periods) -> Decision:
    """May the question go, and with how many tokens and how much money at most: the question's own
    limits, cut to what is left of the system's and the user's day and month."""
    if recent.duplicate_of is not None:
        return _refuse("duplicate", "user",
                       f"The same question was asked within {settings.duplicate_window_seconds} s "
                       f"(request {recent.duplicate_of}); it is not run twice.", duplicate_of=recent.duplicate_of)
    if settings.cooldown_seconds and recent.last_started_at is not None:
        wait_until = recent.last_started_at + timedelta(seconds=settings.cooldown_seconds)
        if now < wait_until:
            return _refuse("cooldown", "user",
                           f"One question every {settings.cooldown_seconds} s; wait "
                           f"{int((wait_until - now).total_seconds()) + 1} s.", wait_until)
    if recent.window_requests >= settings.rate_window_max_requests:
        return _refuse("rate", "user",
                       f"At most {settings.rate_window_max_requests} questions in {settings.rate_window_seconds} s.",
                       now + timedelta(seconds=settings.rate_window_seconds))
    model = settings.model
    rate = worst_rate(model, settings.prices)
    tokens = int(settings.max_tokens_question)
    cost_cap = float(settings.max_cost_usd_question)
    levels = (
        ("system", system_used, settings.budget_usd_month, settings.budget_usd_day, settings.tokens_month,
         settings.tokens_day, settings.requests_day),
        ("user", user_used, user.budget_usd_month, user.budget_usd_day, user.tokens_month, user.tokens_day,
         user.requests_day),
    )
    floor_tokens = int(settings.min_tokens_turn)
    for level, used, money_month, money_day, tokens_month, tokens_day, requests_day in levels:
        if requests_day is not None and used.day_requests >= requests_day:
            return _refuse("requests_day", level, f"The {level}'s {requests_day} questions a day are used.",
                           period.next_day)
        for limit, spent, name, until in ((tokens_day, used.day_tokens, "day", period.next_day),
                                          (tokens_month, used.month_tokens, "month", period.next_month)):
            if limit is None:
                continue
            left = int(limit) - int(spent)
            if left < floor_tokens:
                return _refuse(f"tokens_{name}", level,
                               f"The {level}'s {limit:,} tokens a {name} are used ({spent:,}).", until)
            tokens = min(tokens, left)
        for limit, spent, name, until in ((money_day, used.day_cost, "day", period.next_day),
                                          (money_month, used.month_cost, "month", period.next_month)):
            if limit is None:
                continue
            left = float(limit) - float(spent)
            if left < floor_tokens * rate:
                return _refuse(f"budget_{name}", level,
                               f"The {level}'s ${float(limit):.2f} a {name} is used (${float(spent):.4f}).", until)
            cost_cap = min(cost_cap, left)
    if tokens < floor_tokens:
        return _refuse("tokens_question", "question",
                       f"Less than {floor_tokens} tokens are left for a question.")
    return Decision(ok=True, tokens=tokens, cost_cap=cost_cap)
