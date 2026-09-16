r"""Turn this estate's published pages into a showcase nobody can trace back to it.

The reports db_ops produces are the best documentation it has: real fleets, real fragmentation,
real SLA verdicts, over real days. Screenshots go stale and hand-written samples always look like
samples. What is wanted is the actual HTML, for many servers over at least a fortnight, with every
name that belongs to the operator replaced by a stable fake one.

**The safety argument is the whole design, so it is stated first.** Three properties, in order:

1. **The terms come from configuration and from db_ops's own output shapes, never from a guess
   at what looks sensitive.** :func:`db_ops.common.identifier_scan.collect_identifiers` already
   knows every address, `server_id`, credential and person the estate names, in all three
   spellings. Database, schema, table and index names are parsed out of the metric item
   (``db\schema.table.index``) and the ``k=v`` message fields - shapes this tool wrote, so
   reading them back is parsing. Guessing what looks sensitive is how a scrub misses the one
   thing that mattered.
2. **The replacement is stable and shaped** (:mod:`db_ops.lib.pseudonym`) so the output still reads
   as one estate followed across days and pages, which is the only reason to publish real pages
   rather than invent samples.
3. **The output is verified by the checker that already exists, and a failure deletes it.**
   `identifier_scan.scan` is run over the written folder; if it reports one hit the folder is
   removed and the command fails. Publishing is not attempted on a maybe.

Nothing here is reversible. No mapping file is written: a table that turns the showcase back into
the estate would *be* the estate, and it would live beside the thing it compromises.
"""

from __future__ import annotations

import re
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from db_ops.common import identifier_scan
from db_ops.lib import interval_rates, page_banner, pseudonym, timezone as display_timezone

#: Where a showcase is written unless the caller says otherwise. **Under `runtime/`, which never
#: ships and is never scanned**, and deliberately not under `examples/`.
#:
#: The first default was `examples/showcase`, and on 2026-09-10 the export gate refused the whole
#: tree over it: 819 identifier hits across nine pages, real addresses in a directory that is part
#: of the published surface. The pages had been written by a smoke test whose output was deleted -
#: and re-created seconds later by the same command still running in the background. Two lessons,
#: and the second is the one that matters: a scrubber that fails leaves its failure *somewhere*, so
#: the somewhere must be a path that cannot be published even by accident.
DEFAULT_OUTPUT = "runtime/showcase"

#: What a showcase is made of. Anything else in the reports folder is a working file.
PAGE_EXTENSIONS: tuple[str, ...] = (".html", ".htm")

#: The data a page **fetches at run time**, which is part of the page in every sense that matters.
#: `server-metrics.html` is one page listing 40 servers and one `fetch` per server against
#: `server-metrics_<slug>.json`; copy only the HTML and every chart on it is empty. Found on
#: 2026-09-12 by opening a shipped showcase, which is the only way it could have been found — the
#: scrub had certified a folder whose pages could not render.
DATA_EXTENSIONS: tuple[str, ...] = (".json",)

#: Everything a showcase copies. A data file keeps the name its page asks for (put through the
#: mapping, because the slug in it is a `server_id`) and is **not** stamped: the page fetches it by
#: an exact name, so a stamp there is a 404.
SHOWCASE_EXTENSIONS: tuple[str, ...] = PAGE_EXTENSIONS + DATA_EXTENSIONS

#: Engine-owned names that must survive the rewrite. Renaming `dbo` or `master` would make the page
#: read as a different product rather than a different estate, and they identify nobody.
ENGINE_NAMES: frozenset[str] = frozenset({
    "dbo", "public", "sys", "guest", "information_schema",
    "master", "msdb", "model", "tempdb", "postgres", "template0", "template1",
})

#: **The words db_ops itself prints into a page.** Same rule as the engine names, one level up: a
#: report whose own chrome has been renamed reads as a different product rather than a different
#: estate, and none of these words identifies anybody.
#:
#: This list is not a precaution, it is a repair. A customer schema really called `reports` and a
#: table really called `Inventory` are ordinary English words that the object harvest reads off the
#: page like any other, and on 2026-09-12 the shipped showcase came out titled
#: `DB Ops · QuotaStaging`, offering `Fleet BillingResult` and `Index Usage SettlementHistory`. It
#: is the same failure `build_mapping` already records from 2026-09-10, where `inventory` was a
#: configured term and `database-inventory-report.html` became `database-FORECAST-report.html`; the
#: filter that fixed it covers configured values only, and these arrive from the pages.
#:
#: Every entry has to be a word db_ops actually prints - checked by diffing a scrubbed page's
#: chrome against the original, not by imagination. `schedule` is the awkward one and is kept: it
#: is also a real schema on this estate, so keeping it ships that name. A schema called `schedule`
#: identifies nobody; a page whose prose has been rewritten is unreadable.
#:
#: The cost is stated plainly: a customer object with one of these names ships unscrubbed. That is
#: the line `identifier_scan` already draws at `review` confidence — "acting on it mechanically is
#: how a scrub renames the word export" — and a generic English word is not an identifier.
PRODUCT_WORDS: frozenset[str] = frozenset({
    # the page families and their furniture
    "report", "reports", "inventory", "usage", "metric", "metrics", "fleet", "summary",
    "overview", "detail", "details", "index", "indexes", "page", "pages", "chart", "series",
    # what a report is about
    "server", "servers", "host", "hosts", "instance", "instances", "database", "databases",
    "storage", "backup", "backups", "restore", "restores", "log", "logs", "archive", "history",
    "snapshot", "session", "sessions", "job", "jobs", "task", "tasks", "command", "commands",
    "execution", "executions", "queue", "node", "nodes", "engine", "collector", "collectors",
    # verdicts and measurements
    "status", "health", "score", "total", "count", "result", "results", "value", "values",
    "alert", "alerts", "severity", "warning", "critical", "passed", "failed", "threshold",
    "compliance", "policy", "audit", "monitor", "security", "event", "events",
    # time and shape
    "daily", "monthly", "weekly", "day", "days", "hour", "hours", "minute", "minutes",
    "date", "time", "batch", "group", "groups",
    "name", "names", "size", "file", "files", "type", "kind", "level", "state", "label",
    "title", "data", "configuration", "settings", "deployment", "db_ops",
    # Found the way the rest were: by diffing a scrubbed page's chrome against the original and
    # reading what had changed. `metric_results` is db_ops's own table; the others are words it
    # prints in a label, a heading or a sentence.
    "dashboard", "policies", "source", "incident", "measure", "metric_results", "mode",
    "partitions", "record", "schedule", "step", "test",
    # CSS, because a page builds inline style in its own template literals - which is text, not
    # code, so the `<style>` exemption does not reach it. `style="color:${…}"` came out as
    # `style="OrdersSnapshot:${…}"` on an estate with a table called `Color`.
    "color", "background", "border", "margin", "padding", "font", "align", "width", "height",
    "display", "position", "opacity", "transform", "flex", "grid", "left", "right", "top",
    "bottom", "style", "class", "span", "div", "row", "cell", "text", "line", "block",
})

#: Everything the rewrite must leave exactly as it found it.
KEEP: frozenset[str] = ENGINE_NAMES | PRODUCT_WORDS


#: The command's own help. It lives here rather than in the CLI so the text and the behaviour it
#: describes are edited in one file — a usage string that drifts from its command is a lie with a
#: `--help` in front of it.
USAGE = r"""usage: python -m db_ops.common.cli build-showcase <json>|@<file>|-

Copies the published HTML reports into a folder with every identifier this estate owns replaced by
a stable fake one, so real pages - many servers, many days - can be shown to people who must not
learn anything about the estate that produced them.

The terms are NOT guessed. Addresses, server_ids, credentials and people come from
check-identifiers' own inventory-derived list; database, schema, table and index names are parsed
out of db_ops's own shapes on the page - the db\schema.table.index item and the k=v message fields
a collector writes. A configured value that is only an ordinary word is left alone, exactly where
check-identifiers draws that line, and named in the result as left_as_ordinary_words.

The vocabulary is learnt from every page first and then applied to all of them, so a table that
first appears on day three is mapped on day one's page too.

The replacement is stable and keeps the shape: one machine is the same fake machine on every page
and on every day, an index keeps its PK_/IX_ prefix, a server_id keeps its construction. A showcase
where a name changes between pages documents nothing.

It certifies itself. check-identifiers is run over the result, and unrecognised addresses count as
failures too - a report page carries addresses the collectors found on the wire that no
configuration names. ANY finding deletes the output and fails: a partially scrubbed page is the one
that gets published.

  source       the reports directory to read (default: runtime/reports)
  output       where to write the showcase (default: runtime/showcase - NOT a path that ships)
  pattern      optional glob to narrow which pages are taken
  extra_terms  {name: kind} for anything the page shapes do not state outright
  force        overwrite a non-empty output folder
  verify       false only to debug a mapping - never to publish
  allow        [fragment, ...] lines carrying one of these are not counted as findings. For the
               one hit the scrub CANNOT fix: the two-octet shorthand tier reads `storage_used_pct=
               100.108` as a reference to a machine, and rewriting a percentage would corrupt the
               page it is trying to publish. Each entry is a judgement, so make it narrow and read
               `allowed` in the result.
  stamp        name each page after the moment it states, in UTC (default true):
               20260912T0130Z_sla.html. The node names its archive in its own display zone, so
               the same page copied by two operators is filed under two different days; the page
               carries the offset, so the name can be read the same way everywhere. Sibling links
               are repointed at the renamed files, so the folder still browses.

Run it where the pages actually accumulate - the node that serves them - after the daily archive
has built up the window you want. A showcase of one day is a screenshot.
"""


class ShowcaseError(RuntimeError):
    """The showcase cannot be built, or cannot be certified once built."""


#: A metric item names an object as ``db\schema.table.index`` (with ``#p<n>`` on a partitioned
#: index), and a collector message carries the same parts as ``db=``/``schema=``/``table=``/
#: ``index=``. Both shapes are db_ops's own, written by db_ops, so reading them back out of a page
#: is parsing rather than guessing.
_PART = r"[A-Za-z0-9_$\-]{2,}"
_ITEM_RE = re.compile(
    rf"({_PART})\\({_PART})\.({_PART})(?:\.({_PART}))?")

#: The same four parts, **all dotted and filling one HTML element by themselves** — which is how a
#: report page renders an object (`<td>METER_Prod.flx.FLXSupplier.IX_FLXSupplier_Type</td>`) rather
#: than how the store writes it. Missed until 2026-09-12, and it was the larger half: the index
#: pages are almost entirely this shape, so a scrubbed page kept every schema, table and index name
#: the customer had invented while the database name — the one part configuration knows — was
#: correctly replaced.
#:
#: Two guards, and the **second one is the whole reason this is anchored to an element** rather
#: than matched anywhere in the text:
#:
#: * each part must contain a letter, which keeps a version (`16.0.1000.6`), a date and a decimal
#:   out — without it the pattern reads four numbers as four object names and renames them;
#: * the match must *be* the element's entire content. Loose, it reads a **JavaScript property
#:   chain** as an object: measured the same day, `chart.data.datasets.length` inside the page's
#:   own script taught the scrub that `data` and `inventory` were customer tables, and the output
#:   was written to `database-inventory.html` renamed to `BillingAuditbase-PricingQueueingResult`.
#:   That is the 2026-09-10 failure — the scrub rewriting the product's own vocabulary — arriving
#:   by a second route, and it also multiplied the term count until a 15 MB showcase took an hour.
_NAMED_PART = r"(?=[A-Za-z0-9_$\-]*[A-Za-z])[A-Za-z0-9_$\-]{2,}"
_DOTTED_ITEM_RE = re.compile(
    rf">\s*({_NAMED_PART})\.({_NAMED_PART})\.({_NAMED_PART})\.({_NAMED_PART})\s*<")

#: A page's own script and style, which are the product's code and not the operator's data. Taken
#: out before the dotted shape is read, because a one-expression script body *is* an element's
#: whole content and `chart.data.datasets.length` is therefore indistinguishable from an item.
#: Only that pass skips them: the `db\schema.table.index` item and the `k=v` message fields are
#: shapes db_ops wrote, and they appear legitimately inside the series JSON a page embeds.
_CODE_BLOCK_RE = re.compile(r"<(script|style)\b[^>]*>.*?</\1\s*>", re.IGNORECASE | re.DOTALL)

#: The same two blocks, kept whole so a rewrite can treat each one differently.
_STYLE_BLOCK_RE = re.compile(r"(<style\b[^>]*>)(.*?)(</style\s*>)", re.IGNORECASE | re.DOTALL)
_SCRIPT_BLOCK_RE = re.compile(r"(<script\b[^>]*>)(.*?)(</script\s*>)", re.IGNORECASE | re.DOTALL)

#: Where a JavaScript comment starts and ends. Read by :func:`_js_string_spans`, which has to know
#: about them: an apostrophe inside `/* don't */` is not a quote, and a scanner that thinks it is
#: gets every string boundary after it wrong. Measured 2026-09-12 - a regex alternation over the
#: two quote characters desynchronised on exactly that and left a whole page unscrubbed while
#: reporting success.
_QUOTES = "\"'`"


#: After one of these, a `/` starts a regular expression rather than a division. The classic
#: JavaScript ambiguity, resolved the classic way: a regex may only appear where an expression
#: may.
_BEFORE_REGEX = set("(,=:[!&|?{};+-*%~^<>") | set(" \t\r\n")


def _expects_expression(body: str, index: int) -> bool:
    """Whether the ``/`` at ``index`` opens a regular-expression literal."""
    cursor = index - 1
    while cursor >= 0 and body[cursor] in " \t\r\n":
        cursor -= 1
    if cursor < 0:
        return True
    if body[cursor] in _BEFORE_REGEX:
        return True
    # `return /x/`, `typeof /x/` - a keyword is an operator position too.
    word = ""
    while cursor >= 0 and (body[cursor].isalpha() or body[cursor] == "_"):
        word = body[cursor] + word
        cursor -= 1
    return word in {"return", "typeof", "case", "in", "of", "new", "delete", "void", "throw"}


def _js_string_spans(body: str, start: int = 0, end: int | None = None) -> list[tuple[int, int]]:
    """The `(start, end)` of every string literal's **contents** in a script body.

    A script is the one place a page mixes the product and the estate: the program is the
    product's, and the data it renders - the fleet array, the series file names, the triage text -
    is the operator's, and all of it is inside quotes. So the quotes are where the rewrite goes,
    and the code between them is left exactly as written.

    Four things break a naive scan, and every one of them was met on a real page:

    * **escapes**, and both quote characters;
    * **comments** - an apostrophe in `/* don't */` is not a quote, and a scanner that thinks it is
      gets every boundary after it wrong;
    * **regular-expression literals** - these pages carry `.replace(/[&<>"']/g, …)`, which holds
      one of each quote. That one left a whole page unscrubbed while the command reported success;
    * **template literals**, whose `${…}` is not text at all but an expression. Missed until
      2026-09-12, and it is the one that broke the *published* showcase rather than the scrub:
      ``Could not load ${esc(err.message)}`` became ``${esc(err.AssetStatus)}``, 21 expressions
      were rewritten across the fleet page, and the first one to run threw - so the reader saw
      `Loading…` for ever and the inventory's detail panel never opened.
    """
    size = len(body) if end is None else end
    spans: list[tuple[int, int]] = []
    index = start
    while index < size:
        char = body[index]
        if char == "/" and index + 1 < size and body[index + 1] == "/":
            end_of_line = body.find("\n", index)
            index = size if end_of_line < 0 or end_of_line > size else end_of_line
        elif char == "/" and index + 1 < size and body[index + 1] == "*":
            close = body.find("*/", index + 2)
            index = size if close < 0 or close > size else close + 2
        elif char == "/" and _expects_expression(body, index):
            index = _skip_regex(body, index, size)
        elif char in "\"'":
            cursor = _skip_quoted(body, index, size)
            spans.append((index + 1, min(cursor - 1, size)))
            index = cursor
        elif char == "`":
            index, inner = _scan_template(body, index, size)
            spans.extend(inner)
        else:
            index += 1
    return spans


def _skip_quoted(body: str, index: int, size: int) -> int:
    """Index just past the closing quote of the ordinary string literal opening at ``index``."""
    quote = body[index]
    cursor = index + 1
    while cursor < size:
        if body[cursor] == "\\":
            cursor += 2
            continue
        if body[cursor] == quote:
            return cursor + 1
        cursor += 1
    return size + 1


def _skip_regex(body: str, index: int, size: int) -> int:
    """Index just past the regular-expression literal opening at ``index``."""
    cursor, in_class = index + 1, False
    while cursor < size:
        if body[cursor] == "\\":
            cursor += 2
            continue
        if body[cursor] == "[":
            in_class = True
        elif body[cursor] == "]":
            in_class = False
        elif body[cursor] == "/" and not in_class:
            break
        elif body[cursor] == "\n":
            break
        cursor += 1
    return cursor + 1


def _scan_template(body: str, index: int, size: int) -> tuple[int, list[tuple[int, int]]]:
    """A template literal, as (index past its closing backtick, spans of its **text**).

    The text between `${…}` holes is data; each hole is an expression and is scanned as code, so a
    string written inside one is still found and everything else in it is left alone.
    """
    spans: list[tuple[int, int]] = []
    cursor = index + 1
    chunk = cursor
    while cursor < size:
        char = body[cursor]
        if char == "\\":
            cursor += 2
            continue
        if char == "`":
            spans.append((chunk, cursor))
            return cursor + 1, spans
        if char == "$" and cursor + 1 < size and body[cursor + 1] == "{":
            spans.append((chunk, cursor))
            closes = _skip_hole(body, cursor + 2, size)
            spans.extend(_js_string_spans(body, cursor + 2, closes))
            cursor = closes + 1
            chunk = cursor
            continue
        cursor += 1
    spans.append((chunk, size))
    return size, spans


def _skip_hole(body: str, index: int, size: int) -> int:
    """Index of the `}` closing a `${` substitution, counting nested braces and strings."""
    depth = 1
    cursor = index
    while cursor < size:
        char = body[cursor]
        if char == "\\":
            cursor += 2
            continue
        if char in "\"'":
            cursor = _skip_quoted(body, cursor, size)
            continue
        if char == "`":
            cursor, _ = _scan_template(body, cursor, size)
            continue
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return cursor
        cursor += 1
    return size


def apply_to_script(mapping: "pseudonym.Mapping", body: str) -> str:
    """Rewrite the **data** in a script or a JSON document, and nothing else.

    Two rules, both learned from output that had already certified clean:

    * only inside a string literal. Everything else is the program, and a term is a word: a
      customer table called `Color` turned every `color:` in the page into a fake name.
    * **not** a literal that is a property *name* — one followed by `:`. `{"file": "…"}` is read by
      the page as `server.file`, so renaming the key leaves a fleet picker that cannot find its own
      data. The output parsed and would not render.

    A JSON file is the same shape with no code around it, which is why this is shared: the pages
    fetch their series as JSON, and its keys are as much a part of the program as the page's.
    """
    rebuilt: list[str] = []
    cursor = 0
    for begins, ends in _js_string_spans(body):
        rebuilt.append(body[cursor:begins])
        after = ends + 1
        while after < len(body) and body[after] in " \t\r\n":
            after += 1
        is_key = after < len(body) and body[after] == ":"
        rebuilt.append(body[begins:ends] if is_key else mapping.apply(body[begins:ends]))
        cursor = ends
    rebuilt.append(body[cursor:])
    return "".join(rebuilt)


def apply_to_document(mapping: "pseudonym.Mapping", text: str) -> str:
    """Rewrite a page, leaving the code that renders it alone.

    A term is a word, and some of the operator's words are also the page's own. Measured on
    2026-09-12, on a shipped showcase that had certified clean: a customer table really called
    `Color` turned every `color:` in the stylesheet into `OrdersSnapshot:`, and the pages arrived
    unstyled and unreadable. The scrub had rewritten the product, not the estate.

    So the document is rewritten in three kinds of region:

    * **`<style>` — never.** A stylesheet is generated by this tool and holds nothing of anyone's.
    * **`<script>` — its data only**, through :func:`apply_to_script`.
    * **everything else — fully**, as before.

    This is the same rule as `object_terms` reading only element content, arriving from the other
    side: what the page says is the estate's, what runs the page is the product's.
    """
    def in_script(match: "re.Match[str]") -> str:
        return match.group(1) + apply_to_script(mapping, match.group(2)) + match.group(3)

    pieces: list[str] = []
    index = 0
    for block in _CODE_BLOCK_RE.finditer(text):
        pieces.append(mapping.apply(text[index:block.start()]))
        chunk = block.group(0)
        if block.group(1).lower() == "style":
            pieces.append(chunk)
        else:
            pieces.append(_SCRIPT_BLOCK_RE.sub(in_script, chunk))
        index = block.end()
    pieces.append(mapping.apply(text[index:]))
    # Last, and after the mapping: the login half is already a pseudonym by now, so what is left to
    # drop is only the domain that qualified it. Doing it first would have removed the prefix the
    # mapping needs to recognise `DOMAIN\principal` as one value.
    return drop_domain_prefixes("".join(pieces))


def object_terms(text: str) -> dict[str, str]:
    """Database, schema, table and index names that a page states in db_ops's own shapes.

    These are the operator's *business* vocabulary — a table called `EmployeeTimeKeepingResult`
    says more about a customer than an IP address does — and no configuration file names them, so
    :func:`identifier_scan.collect_identifiers` structurally cannot see them.

    Read from the page rather than from the store, because ``common`` may not import ``db``: the
    shared tier is a stack and the store belongs above it. That is not a workaround with a cost
    hidden in it, but it does have a **limit worth stating**: a name that reaches a page in some
    other shape — inside a JSON blob a template renders client-side, say — is not found here.
    ``extra_terms`` exists for exactly that, and the certifier still refuses anything the inventory
    knows about.
    """
    terms: dict[str, str] = {}

    def remember(value: Any, kind: str) -> None:
        text_value = str(value or "").strip()
        if (len(text_value) >= identifier_scan.MIN_TERM_LENGTH
                and text_value.casefold() not in KEEP):
            terms.setdefault(text_value, kind)

    fields = interval_rates.message_fields(text)
    for key, kind in (("db", "database"), ("schema", "schema"),
                      ("table", "table"), ("index", "index"),
                      # `login=tanthanh_dba, host=DB-THANH` — written into a collector message and
                      # into the error text a failed connection hands back, and named by no
                      # configuration file when the login is one the estate does not monitor with.
                      # Added 2026-09-12, after a scrubbed page shipped a DBA's own account name.
                      ("login", "credential"), ("user", "credential"), ("host", "host")):
        remember(fields.get(key), kind)

    for match in _ITEM_RE.finditer(str(text or "")):
        database, first, second, third = match.groups()
        remember(database, "database")
        if third:
            remember(first, "schema")
            remember(second, "table")
            remember(third, "index")
        else:
            remember(first, "table")
            remember(second, "index")

    for match in _DOTTED_ITEM_RE.finditer(_CODE_BLOCK_RE.sub(" ", str(text or ""))):
        database, schema, table, index = match.groups()
        remember(database, "database")
        remember(schema, "schema")
        remember(table, "table")
        remember(index, "index")
    return terms


#: JSON fields on a published page whose value is an IDENTITY - a person, an account, a database.
#: Every value of one of these is a name, which is what makes harvesting them wholesale safe;
#: `name`, `item` and `message` are deliberately absent, because they carry metric names and column
#: names too and mapping those would rewrite the page's vocabulary along with its identities.
_JSON_IDENTITY_FIELDS: dict[str, str] = {
    "login": "credential",
    "owner": "credential",
    "owners": "credential",      # a list
    "username": "credential",
    "account": "credential",
    "principal": "credential",
    "database": "database",
    "schema": "schema",
}

#: `"login": "user_prod_dw_staging"` and `"owners": ["CRED_1184", "user_payroll_prod"]`.
_JSON_FIELD_RE = re.compile(
    r'"(' + "|".join(_JSON_IDENTITY_FIELDS) + r')"\s*:\s*(?:"([^"]*)"|\[([^\]]*)\])')


#: Metric codes whose ``item`` names a PRINCIPAL or a JOB rather than a measurement. `item` is
#: always "the thing this metric is about", so for most codes it is a database, an index or a
#: volume - already reached by `object_terms`, and for the OS ones a word like `disk_iops` that must
#: NOT be renamed or the page stops meaning anything. These are the codes where it is a name that
#: belongs to somebody: a login, a server principal, an Agent job.
_IDENTITY_ITEM_CODES = frozenset({
    "SECURITY_SERVER_PRINCIPALS",
    "SECURITY_LOGIN_HEALTH",
    "SECURITY_FAILED_LOGINS",
    "DATABASE_USER_PERMISSIONS",
    "SQL_AGENT_JOB_INVENTORY",
    "SQL_AGENT_JOB_RUNTIME",
})

_ITEM_BY_CODE_RE = re.compile(
    r'"code"\s*:\s*"([A-Z_]+)"[^{}]*?"item"\s*:\s*"([^"]*)"')


def _terms_from_identity_items(text: str) -> dict[str, str]:
    """Logins and job names, which a page states as a metric's ``item`` and nothing declares.

    `SECURITY_SERVER_PRINCIPALS` answers with `host-669\\payroll_dbadmin`; `SQL_AGENT_JOB_INVENTORY`
    with `Job_PAYROLL_DeleteCancelled`. Both are the estate's own names, produced by the servers
    at collection time, and no configuration file has ever heard of either - so neither the mapping
    nor `check-identifiers` could see them. Found 2026-09-16 in the published showcase.

    Keyed on the metric **code**, not on the field: `item` is whatever the metric is about, and for
    `OS_DISK_USAGE` that is `disk_iops`. Renaming a measurement's name would leave a page that is
    scrubbed and meaningless.
    """
    found: dict[str, str] = {}
    for match in _ITEM_BY_CODE_RE.finditer(str(text or "")):
        code, item = match.group(1), str(match.group(2) or "").strip()
        if code not in _IDENTITY_ITEM_CODES:
            continue
        kind = "job" if code.startswith("SQL_AGENT_JOB") else "credential"
        if len(item) >= identifier_scan.MIN_TERM_LENGTH and item.casefold() not in KEEP:
            found.setdefault(item, kind)
            # `DOMAIN\login` is two names joined, and BOTH are the customer's. The login half is
            # the one that also appears alone; the domain half turns up in Group Policy error text
            # and in domain-controller hostnames, where nothing else would have reached it.
            if "\\" in item:
                found.update(_names_in_a_qualified_login(item, kind))
    return found


def _names_in_a_qualified_login(value: str, kind: str) -> dict[str, str]:
    """Both halves of ``DOMAIN\\principal``, each as the kind of thing it actually is.

    Found 2026-09-16: the login half was being renamed and the domain was not, so a published page
    read ``EXAMPLECORP\\CRED_8087`` - half scrubbed, and the half that survived is the customer's
    Active Directory name. It appears in three shapes on one page: that prefix, a UNC path in Group
    Policy error text, and the domain controllers' own hostnames. Learning it here as a **host**
    reaches all three, because host terms are matched inside longer names.
    """
    found: dict[str, str] = {}
    domain, _, principal = str(value or "").rpartition("\\")
    for text_value, value_kind in ((principal, kind), (domain.strip("\\"), "host")):
        text_value = text_value.strip()
        if (len(text_value) >= identifier_scan.MIN_TERM_LENGTH
                and text_value.casefold() not in KEEP):
            found.setdefault(text_value, value_kind)
    return found


#: ``DOMAIN\principal`` where a page states an identity. The domain is dropped rather than renamed:
#: a fake Active Directory name tells a reader nothing the login did not already, and every one left
#: on a page is a thing that has to be got right. Scoped to the identity fields on purpose - a
#: Windows path is full of backslashes and `D:\MSSQL\Log\x.ldf` must survive intact.
_QUALIFIED_LOGIN_RE = re.compile(
    r'("(?:login|owner|owners|username|account|principal)"\s*:\s*\[?\s*"|login=)'
    r'[A-Za-z0-9._\-]+\\\\?(?=[A-Za-z0-9._\-])')


def drop_domain_prefixes(text: str) -> str:
    """Leave only the principal where a page writes ``DOMAIN\\principal``."""
    return _QUALIFIED_LOGIN_RE.sub(lambda match: match.group(1), str(text or ""))


def _terms_from_json_fields(text: str) -> dict[str, str]:
    """Names the PAGE states, as opposed to names the inventory states.

    `_terms_from_text` already harvests `login=` and `host=` - but from the `key=value` shape a
    collector writes into a *message*. A report page is JSON, and `"login": "a.person"` went
    through that harvester as nothing at all. Measured 2026-09-16 on the published showcase: **910
    `"login"` values, 348 `"owners"` lists and 110 `"owner"` values**, carrying a person's account
    name, database logins and credential labels - none of which any configuration file names,
    because they are what the *databases* answered when the collectors asked.

    That is the whole class this misses by construction: `build_mapping` learns from
    `db_instances.json` and `users.json`, so it can only rename what the estate declared. A page is
    made of what the estate's servers reported. Learning the page's own identity fields is how the
    two are closed against each other.
    """
    found: dict[str, str] = {}
    for match in _JSON_FIELD_RE.finditer(str(text or "")):
        field, single, listed = match.group(1), match.group(2), match.group(3)
        kind = _JSON_IDENTITY_FIELDS[field]
        values = [single] if single is not None else re.findall(r'"([^"]*)"', listed or "")
        for value in values:
            text_value = str(value or "").strip()
            if "\\" in text_value:
                found.update(_names_in_a_qualified_login(text_value, kind))
                continue
            if (len(text_value) >= identifier_scan.MIN_TERM_LENGTH
                    and text_value.casefold() not in KEEP):
                found.setdefault(text_value, kind)
    return found


def build_mapping(*, data_dir: str | Path | None = None, pages_text: str = "",
                  extra_terms: dict[str, str] | None = None) -> pseudonym.Mapping:
    """Every real term this estate would put on a page, mapped to its stable fake.

    Refuses an empty inventory for the reason `identifier_scan` does: a mapping with no terms
    rewrites nothing and certifies clean, so the one failure this must never have is the one that
    would look like success.
    """
    configured = identifier_scan.collect_identifiers(data_dir)
    if not configured:
        raise ShowcaseError(
            "the inventory named no identifiers, so a rewrite would change nothing and still "
            "report success. Refusing rather than publishing the estate unchanged.")

    # Only what the scanner would act on. `collect_identifiers` returns every configured value,
    # including ordinary words that happen to be a database name - `identifier_scan` puts those at
    # `review` confidence and deliberately keeps them out of its headline number, "because acting
    # on it mechanically is how a scrub renames a Python function called export". Measured here on
    # 2026-09-10: without this filter the word **inventory** was a term, and the first run renamed
    # `database-inventory-report.html` to `database-FORECAST-report.html`. The scrub had started
    # rewriting the product's own vocabulary.
    terms = {name: kind for name, kind in configured.items()
             if identifier_scan.confidence(name) != identifier_scan.REVIEW}
    skipped_as_ordinary = sorted(set(configured) - set(terms))
    #: What the inventory named, kept apart from what the pages name - the two need different
    #: matching rules, and only this half is small enough for the substring pass.
    configured_terms = dict(terms)

    # The store-derived names are not filtered, and that asymmetry is deliberate: a table called
    # `Shift` is `review` confidence as a word and is still this customer's schema. They arrive
    # with an explicit kind because a collector wrote them as `table=`, not because they matched.
    if pages_text:
        terms.update(object_terms(pages_text))
        # The page's own identity fields, which no configuration names: `"login"`, `"owner"`,
        # `"owners"`. Applied after `object_terms` so a name the page states as an object keeps
        # that kind, and before `extra_terms` so a hand-written entry still wins.
        terms.update(_terms_from_json_fields(pages_text))
        terms.update(_terms_from_identity_items(pages_text))
    named_by_hand = {str(name): str(kind) for name, kind in (extra_terms or {}).items()
                     if str(name).casefold() not in KEEP}
    terms.update(named_by_hand)
    for name in list(terms):
        if name.casefold() in KEEP:
            terms.pop(name)
    mapping = pseudonym.Mapping(terms)

    # What the operator wrote into `extra_terms` is rewritten **wherever it appears**, not only as
    # a whole name. They are naming it by hand precisely because the page shapes did not reach it:
    # `tanthanh_dba` sits inside a credential name and `ORG1` inside a server_id the page builds for
    # itself, and `_` and `-` are word characters, so both survived a clean certification.
    for name, kind in named_by_hand.items():
        mapping.add(name, kind, loose=True)

    # **A database name is written inside longer names, and that is the normal case, not the odd
    # one.** `pseudonym._passes` reasoned that "nothing writes `PAYROLL_Prod` inside a longer word",
    # and the estate writes it into every file it keeps for that database: `PAYROLL_Prod_log.ldf`,
    # `host-669$PAYROLL_APP_Prod_FULL_20260916_010000.bak`, an SSIS project named after it. The
    # whole-name pass could not see any of them, so 37 filenames carrying a real database name
    # survived a clean certification - found 2026-09-16 by an operator reading the published pages.
    #
    # Matching is already case-insensitive (`_passes` keys on `casefold`), so `user_payroll_prod`
    # matches `PAYROLL_Prod` too. Only the *database* kind is loosened: a schema or a column called
    # `Shift` is a word that appears inside ordinary prose, and rewriting those would scrub the
    # page's vocabulary along with its identities.
    #
    # `host` is loosened with it, for the same evidence: a SQL Server instance is written
    # `SERVER$INSTANCE`, so the instance name sits inside `host-669$PAYROLL_APP_Prod_...trn` and
    # inside an SSIS project named after it. The instance name is the estate's system
    # name, which is the thing being protected.
    # Over **every** term, not just the configured ones: an instance name is something the pages
    # state as `host=`, and no inventory field carries it. Reading only the configured half is what
    # left the first attempt at this fix with the same 19 filenames it started with.
    for name, kind in list(terms.items()):
        if kind in {"database", "host"}:
            mapping.add(name, kind, loose=True)

    # And so is a **configured** value the checker matches as a substring. `identifier_scan` puts
    # an address, a `server_id` and any token carrying both a digit and a separator at `certain`
    # confidence and searches for them *inside* other words - which is how it finds `SQL-NODE2`
    # in `SQL-NODE2_SALES_LOG_20260910.trn`, a backup path these pages print. The rewrite has
    # to use the same rule, or the two disagree and the one that disagrees quietly is the scrub.
    #
    # Configured values only - a few hundred of them. The names read off the *pages* are not
    # included even though many are `certain` by that definition (`IX_9366CHANNELTRANS` carries a
    # digit and a separator): there are 52,000 of those, they appear as a whole cell and never
    # inside another word, and matching them as substrings puts every one of them into the
    # alternation - which is the shape that does not finish.
    for name, kind in configured_terms.items():
        if identifier_scan.confidence(name) == identifier_scan.CERTAIN:
            mapping.add(name, kind, loose=True)

    # The certifier searches for the **two-octet shorthand** as well, because that is how prose
    # names a machine: "measured on 2.248", or a credential called `sqlserver_100.86_MSSQLSERVER`.
    # The rewrite has to know the same spellings, or the two can never agree - measured on
    # 2026-09-12, when a scrub that had replaced every address still could not certify its own
    # output, on shorthands sitting inside operator notes. Each one lands on the *fake* address's
    # own shorthand, so a note and the inventory row still name one machine.
    for term, fake in list(mapping.as_dict().items()):
        for short in identifier_scan.address_shorthands(term):
            mapping.add_pair(short, _shorthand_of(short, fake), bounded=True)

    mapping.skipped_as_ordinary = skipped_as_ordinary
    return mapping


def _shorthand_of(short: str, fake: str) -> str:
    """The two trailing parts of ``fake``, written with the separator ``short`` uses."""
    separator = next((mark for mark in "._-" if mark in short), ".")
    parts = re.split(r"[._-]", fake)
    return separator.join(parts[-2:]) if len(parts) >= 2 else fake


#: The stamps a published page can already carry in front of its name: the daily archive's
#: ``20260825_sla.html`` and the fleet inventory's finer
#: ``20260908_205847_database-inventory-report.html``, both written by
#: :mod:`db_ops.lib.report_archive` in the node's display zone — and the UTC one this module
#: writes. All three are matched by the one pattern so that re-running a showcase over its own
#: output does not stack a second stamp on every file.
_ARCHIVE_STAMP_PATTERN = re.compile(r"^(?:\d{8}T\d{4}Z|\d{8}(?:_\d{6})?)_")

#: ``href="database-inventory.html"`` - a sibling page in the same folder, with an optional query.
#: Deliberately narrow: no slash, so a link off the mount is left alone. These pages link to each
#: other by bare file name (:data:`db_ops.lib.page_banner.SIBLING_PAGES`), which is what a rename
#: has to repair.
_HREF_PATTERN = re.compile(r'href="(?!https?:|//|/|\.\.)([^"/?#]+\.html?)(\?[^"#]*)?(?:#[^"]*)?"',
                           re.IGNORECASE)


#: A page named as a **JSON string value** rather than an ``href``. The fleet page carries its
#: server list as data — ``"index_usage_file":"index-usage_<server_id>.html"`` — and the browser
#: follows that name, so it is a link in every sense except the one :data:`_HREF_PATTERN` matches.
#:
#: Found on 2026-09-14 by opening the published showcase on GitHub Pages: every per-server index
#: usage link answered **404**. The pages had been renamed to their stamped form
#: (``20260911T1801Z_index-usage_…``) and the repair below rewrote only ``href="…"``, so twelve of
#: fourteen links pointed at files that no longer existed under that name. The two that worked did
#: so by accident: those pages carried no banner, kept the node's own name, and that name happened
#: to be the stable one.
#:
#: Matched as a quoted whole value, never as a substring: a name is repointed because it *is* the
#: file, not because it appears inside a longer string.
_JSON_PAGE_PATTERN = re.compile(r'"([^"/?#\\]+\.html?)"', re.IGNORECASE)


def stable_name(name: str) -> str:
    """A published file name with the node's own archive stamp taken off.

    ``20260825_sla.html`` and ``sla.html`` are the same page on two days, and every link between
    pages is written against the second spelling. Stripping the stamp is what lets a captured
    window be re-linked to itself after the files are renamed.
    """
    return _ARCHIVE_STAMP_PATTERN.sub("", str(name or ""))


def _pages(source: Path) -> list[Path]:
    return sorted(path for path in source.rglob("*")
                  if path.is_file() and path.suffix.lower() in SHOWCASE_EXTENSIONS)


def is_page(path: Path) -> bool:
    """Whether this file is a page a reader opens, rather than data a page fetches."""
    return path.suffix.lower() in PAGE_EXTENSIONS


def _output_name(path: Path, markup: str, mapping: "pseudonym.Mapping", *,
                 stamped: bool) -> str:
    """What one page is called in the showcase: its scrubbed name, under the moment it was built.

    Two rewrites, and both are about a name being evidence:

    * **The mapping is applied to the file name**, because ``index-usage_<server_id>.html`` is a
      whole server_id in a path — the page's own contents are scrubbed and the folder listing
      would still name the estate.
    * **The stamp is replaced, not kept.** The node writes ``20260825_sla.html`` in its own display
      zone, so the same page copied by two operators is filed under two different days and neither
      name says which clock it is on. The page states its moment with an offset
      (:func:`db_ops.lib.page_banner.snapshot_stamp`), so the name can state it in UTC and be read
      the same way everywhere: ``20260825T1632Z_sla.html``.

    A page that carries **no** banner keeps the name the node gave it, archive prefix and all, and
    :func:`build` reports it separately. Two reasons, and the second is the one that decides it:
    stamping such a page with the moment it was *copied* states something the page never said, and
    it is not even distinguishing — a window of days with no banners would collapse onto one name
    and overwrite itself down to a single file.
    """
    if not stamped or not is_page(path):
        return mapping.apply(stable_name(path.name))
    moment = display_timezone.parse_display(page_banner.snapshot_stamp(markup))
    if moment is None:
        return mapping.apply(path.name)
    return f"{display_timezone.utc_file_stamp(moment)}_{mapping.apply(stable_name(path.name))}"


def _link_targets(from_name: str, names: dict[Path, str]) -> dict[str, str]:
    """For one page, which captured file each ``href="sibling.html"`` should now point at.

    A snapshot that cannot be browsed is a folder of orphans, and the stamped names break every
    link the pages ship with. Within one day the answer is obvious; across a captured window it is
    not, so the rule is stated: **link to the sibling from the same day, and otherwise to the
    newest copy of it at or before this page's day.** A page from the 25th that linked forward to
    the 12th would read as one estate on two dates.
    """
    grouped: dict[str, list[str]] = {}
    for name in names.values():
        grouped.setdefault(stable_name(name), []).append(name)

    day = from_name[:8] if from_name[:8].isdigit() else ""
    targets: dict[str, str] = {}
    for stable, candidates in grouped.items():
        ordered = sorted(candidates)
        earlier = [name for name in ordered if not day or name[:8] <= day]
        targets[stable] = (earlier[-1] if earlier else ordered[0])
    return targets


def _relink(text: str, targets: dict[str, str]) -> str:
    """Repoint every sibling link in a scrubbed page at the file that is actually in the folder.

    **Both spellings of a link**: ``href="sibling.html"``, and a page named as a JSON string value
    in the page's own data (``"index_usage_file":"index-usage_….html"``). The second is followed by
    the browser exactly as the first is, and repairing only the first published a showcase whose
    per-server links all returned 404 — see :data:`_JSON_PAGE_PATTERN`.

    The query string is dropped on purpose. ``?date=`` is answered by the web host reading its
    archive; a folder of files has no host, and a link that keeps the query would ask a static
    copy a question only a running node can answer.
    """
    def swap_href(match: "re.Match[str]") -> str:
        target = targets.get(match.group(1))
        return f'href="{target}"' if target else match.group(0)

    def swap_json(match: "re.Match[str]") -> str:
        target = targets.get(match.group(1))
        return f'"{target}"' if target else match.group(0)

    return _JSON_PAGE_PATTERN.sub(swap_json, _HREF_PATTERN.sub(swap_href, text))


def build(request: dict[str, Any] | None = None, *,
          data_dir: str | Path | None = None) -> dict[str, Any]:
    """Copy the published pages into a showcase folder with every real name replaced.

    Request fields:

    ``source``      the reports directory to read (default ``runtime/reports``).
    ``output``      where the showcase is written (default :data:`DEFAULT_OUTPUT`).
    ``extra_terms`` ``{name: kind}`` for anything the page shapes do not state outright.
    ``pattern``     optional glob to narrow which pages are taken.
    ``force``       overwrite a non-empty output folder.
    ``verify``      run the identifier scan over the result (default true, and *keep it true*).
    ``stamp``       name each page after its own moment, in UTC (default true) — see below.
    ``allow``       fragments whose line is not counted as a finding — see :func:`certify`.
    """
    payload = dict(request or {})
    source = Path(payload.get("source") or "runtime/reports")
    output = Path(payload.get("output") or DEFAULT_OUTPUT)
    verify = bool(payload.get("verify", True))
    stamped = bool(payload.get("stamp", True))

    if not source.is_dir():
        raise ShowcaseError(f"no reports directory at {source} - nothing to snapshot.")
    pages = _pages(source)
    if payload.get("pattern"):
        pages = [path for path in pages if path.match(str(payload["pattern"]))]
    if not pages:
        raise ShowcaseError(
            f"{source} holds no .html pages. Run the reports and let the daily archive build up "
            "first: a showcase of one day is a screenshot.")
    if not any(is_page(path) for path in pages):
        raise ShowcaseError(
            f"{source} holds data files but no page to read them. Copy the .html as well.")

    if output.exists() and any(output.iterdir()):
        if not payload.get("force"):
            raise ShowcaseError(f"{output} is not empty. Pass force to overwrite it.")
        # Cleared, not written over. `force` used to overwrite file by file, so a rebuild of a
        # window that no longer holds Tuesday's page left Tuesday's page in the folder - and the
        # scan then certified a folder that was partly the previous run's. Measured 2026-09-14:
        # a rebuild produced 60 pages into a folder that ended up with 77 files, 17 of them
        # orphans from three days earlier, including a fleet page scrubbed by the older code.
        for item in output.iterdir():
            shutil.rmtree(item) if item.is_dir() else item.unlink()

    # One pass to learn the vocabulary, a second to rewrite: an object name that appears only on
    # day three must be mapped on day one's page too, or the same table is two things across the
    # window and the showcase stops being one estate.
    sources = {path: path.read_text(encoding="utf-8", errors="replace") for path in pages}
    mapping = build_mapping(data_dir=data_dir,
                            pages_text="\n".join(sources.values()),
                            extra_terms=payload.get("extra_terms"))

    # Every output name is decided before anything is written, because the pages link to each
    # other: a rename that is applied file by file leaves the first page pointing at a name the
    # last page no longer has.
    names = {path: _output_name(path, sources[path], mapping, stamped=stamped) for path in pages}
    kept_node_naming = sorted(names[path] for path in pages
                              if stamped and not page_banner.snapshot_stamp(sources[path]))

    output.mkdir(parents=True, exist_ok=True)
    written: list[str] = []
    for path in pages:
        target = output / path.relative_to(source)
        target.parent.mkdir(parents=True, exist_ok=True)
        target = target.with_name(names[path])
        if is_page(path):
            text = _relink(apply_to_document(mapping, sources[path]),
                           _link_targets(names[path], names))
        else:
            # A data file is all data - and all keys. Same rule as a script body.
            text = apply_to_script(mapping, sources[path])
        target.write_text(text, encoding="utf-8")
        written.append(str(target.relative_to(output)))

    outcome: dict[str, Any] = {
        "source": str(source), "output": str(output),
        "pages": len(written), "terms": len(mapping),
        "days_covered": sorted({stem[:8] for stem in (Path(n).name for n in written)
                                if stem[:8].isdigit()}),
        "files": written[:50],
        "unmapped_kinds": sorted(mapping.unmapped_kinds),
        # Named rather than silent: a page with no banner cannot say when it was built, so it was
        # left under the name the node gave it rather than restamped with a moment it never stated.
        "kept_node_naming": kept_node_naming,
        # Stated back, because it is the one place a human judgement overrides the checker.
        "allowed": sorted(str(fragment) for fragment in (payload.get("allow") or ())),
        # Named in the result rather than hidden: these are configured values left
        # in the pages on purpose, and a reader of the showcase should be able to
        # check that judgement rather than take it.
        "left_as_ordinary_words": list(getattr(mapping, "skipped_as_ordinary", [])),
    }

    if verify:
        found = certify(output, data_dir=data_dir, allow=payload.get("allow"))
        outcome["verified"] = True
        outcome["findings"] = found
        if found:
            # Deleted, not left for someone to inspect and forget: a folder of pages that failed
            # the check is a folder somebody eventually publishes.
            shutil.rmtree(output, ignore_errors=True)
            raise ShowcaseError(
                f"the rewritten pages still name {len(found)} real identifier(s) "
                f"({', '.join(found[:5])}). The output was deleted. Add the missing terms and "
                "run again - never publish a partially scrubbed page.")
    return outcome


def certify(output: str | Path, *, data_dir: str | Path | None = None,
            allow: "list[str] | tuple[str, ...] | None" = None) -> list[str]:
    """Which real identifiers survive in the written showcase. Empty means it may be published.

    Deliberately the **same** scanner the export gate uses, not a second implementation: a showcase
    certified by its own rules would be certified by the rules that already let something through.

    Two categories are treated as failures, and the second is the one a naive check would miss:

    * ``hits`` — terms the inventory names, at ``certain`` or ``likely`` confidence. ``review`` is
      excluded for the scanner's own reason: those are ordinary words that happen to be database
      names, and rewriting them mechanically is how a scrub renames the word "export".
    ``allow`` is the narrow escape hatch, and it exists for hits the scrub is structurally unable to
    fix rather than for ones nobody wants to look at. The shorthand tier reads the last two octets
    of an address wherever they stand alone, because that is how prose names a machine — and
    `storage_used_pct=100.108` is a percentage that happens to be spelled the same way. Rewriting
    it would corrupt the page; leaving it refuses the release for ever. So it is stated, in the
    request, narrowly, and echoed in the result as `allowed`.

    * ``unrecognised_addresses`` — anything address-shaped that **no configuration names**. On a
      report page that is the dangerous category rather than a curiosity: a page carries addresses
      the collectors found on the wire — a listener, a replica, a link target — which were never in
      `db_instances.json` and which the mapping therefore never learned.
    """
    # Rooted at the parent with the folder as the path, never an absolute path against an
    # unrelated root: the scanner evaluates its skip list with `relative_to(root)`, which
    # raises outright when the tree is not under it.
    folder = Path(output).resolve()
    result = identifier_scan.scan(
        {"root": str(folder.parent), "paths": [folder.name],
         "extensions": list(SHOWCASE_EXTENSIONS), "allow": list(allow or ())},
        data_dir=data_dir)

    names: set[str] = set()
    for item in result.get("files") or []:
        names.update(str(term) for term in item.get("terms") or ())
    names.update(str(literal) for literal in (result.get("unrecognised_addresses") or {}))
    return sorted(names)
