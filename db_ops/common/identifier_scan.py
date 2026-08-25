"""Which of this estate's real identifiers appear in files that ship.

`step01-mvp03` recorded the source scrub as done on 2026-08-21. On 2026-08-22 a measurement found
**~177 identifiers still in the shipping surface**, and the reason is worth stating because it
decides how this module works: the earlier sweep was a **pattern** sweep over `.py` files. It could
not see `.sql`, `.ps1`, `.sh` or `.yml` inside the package, and it could not see the hyphenated
form `ACME-192-0-2-250`, where the organisation half had been replaced and the address half had
not — so a grep for the organisation code came back clean while the address sat there in the
same token.

**So this does not guess.** The identifiers come from the operator's own configuration:
`db_instances.json` already names every address, `server_id`, service and credential in the estate,
and `telegram_groups.json` / `telegram_users.json` name the people. Searching for *those* strings
cannot produce a false positive — every hit is, by construction, a real value this operator uses.
A pattern sweep is the backstop for what configuration does not name, not the method.

Three things this has to get right, each one a mistake already made in this project:

- **A term has spellings.** `192.0.2.248` is also written `192-0-2-248` (inside a `server_id`)
  and `192_0_2_248` (inside a secret ref, where a `\\b` in a grep does not match because `_` is a
  word character). All three are the same machine.
- **Siblings must be found together.** One four-letter database code was replaced and its
  warehouse sibling was not, which silently reordered an alphabetically sorted assertion — the test still passed and meant something else.
  Deriving from configuration finds both, because both are in it.
- **Not every configured value identifies anything.** `prod`, `sqlserver`, `master` and `1433` are
  in the inventory and are in every file in the tree. :data:`GENERIC_TERMS` and a minimum length
  keep the report readable, because a checker that cries wolf teaches people to switch it off.

What is deliberately *not* reported is recorded in :data:`ALWAYS_ALLOWED`, and each entry cost
something to learn.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Iterable

from db_ops.common import data_sources
from db_ops.lib.paths import TOOL_ROOT

#: What ships: the package, plus the repository-root files a clone receives. `data/`, `audits/`,
#: `scripts/archive/`, `tools/` and `config.json` are excluded — not because they are clean, but
#: because they never leave this repository, and scrubbing a file that does not ship is work with
#: no purpose. An audit that names no host stops being evidence.
DEFAULT_PATHS: tuple[str, ...] = (
    "db_ops",
    "docs",
    "examples",
    "docker-compose.yml",
    "docker-compose.runtime.yml",
    "Dockerfile",
    "pyproject.toml",
    "README.md",
    "CONTRIBUTING.md",
    "SECURITY.md",
)

#: Text file types worth reading. A binary is not scannable and is refused by rule instead
#: (`03-16`): the repository holds two `.xlsx` exports of real Oracle data that any text scanner
#: would pass and that would still leak.
#: `.html` and `.j2` are here because leaving them out meant a whole class of shipped files was
#: never certified: the report templates carry long prose comments explaining *why* each section
#: exists, written from the incident that motivated it — and an incident is described in terms of
#: the environment it happened on. The scan reported those trees clean because it never opened the
#: file. A type that ships and holds prose has to be read; term coverage is not file coverage.
DEFAULT_EXTENSIONS: tuple[str, ...] = (
    ".py", ".sql", ".sh", ".ps1", ".bat", ".cmd",
    ".json", ".yml", ".yaml", ".toml", ".cfg", ".ini",
    ".md", ".txt", ".env", ".example",
    ".html", ".htm", ".css", ".js", ".j2", ".jinja", ".jinja2", ".xml",
)

#: Directories never walked, whatever the requested path.
SKIP_DIRS: frozenset[str] = frozenset({
    "__pycache__", ".git", ".venv", ".pytest_cache", ".pytest_tmp",
    "node_modules", "build", "dist", "deploy", "logs", "runtime",
})

#: Inventory fields that identify a machine, a service or an account. Everything else in a record
#: is a setting, and a setting is not a secret.
IDENTIFYING_FIELDS: tuple[str, ...] = (
    "ip", "server_id", "db_instance_name", "instance_name", "server_name",
    "site", "service_name", "owner", "default_credential_name",
)

#: Configured values that identify nothing, each with the reason it is excluded. A dict rather
#: than a set because an unexplained exclusion is how a scan quietly stops covering something:
#: the next person cannot tell a considered decision from a term somebody found annoying.
GENERIC_TERMS: dict[str, str] = {
    # Environments and engines. Every record carries one and every file mentions them.
    "prod": "an environment label", "uat": "an environment label",
    "dev": "an environment label", "test": "an environment label",
    "lab": "an environment label", "staging": "an environment label",
    "production": "an environment label",
    "sqlserver": "an engine name", "oracle": "an engine name", "mysql": "an engine name",
    "postgresql": "an engine name", "postgres": "an engine name", "mssql": "an engine name",
    "db2": "an engine name",
    # System databases: shipped by the engine, present in every install on earth.
    "master": "a SQL Server system database", "msdb": "a SQL Server system database",
    "tempdb": "a SQL Server system database", "model": "a SQL Server system database",
    "information_schema": "a standard SQL schema",
    # Product defaults. These read as estate names and are not: they are what the vendor or a
    # shipped template creates, so they are identical in every install and scrubbing them would
    # break the code that depends on the vendor's spelling.
    "mssqlserver": "how Windows registers a *default* SQL Server instance - a vendor constant",
    "sqlexpress": "the default instance name of SQL Server Express - a vendor constant",
    "freepdb1": "Oracle Free's default PDB name - a vendor constant",
    "free_sb": "the standby db_unique_name a shipped Data Guard template creates, not a host",
    # Lab container names. These *are* in the inventory, and they are still not estate data: the
    # shipped templates create them, so every operator who runs a lab gets the same names. The
    # inventory records them because the operator ran the template, which is the opposite direction
    # from a name that identifies this estate.
    "ora_dg_lab-primary": "a container name the shipped Data Guard template creates",
    "ora_dg_lab-standby-1": "a container name the shipped Data Guard template creates",
    "pg_ha_01_primary": "a container name the shipped PostgreSQL HA template creates",
    "pg_ha_01_standby_1": "a container name the shipped PostgreSQL HA template creates",
    "pg_ha_01_standby_2": "a container name the shipped PostgreSQL HA template creates",
    "pg_ha-primary": "a container name the shipped PostgreSQL HA template creates",
    "pg_ha-standby-1": "a container name the shipped PostgreSQL HA template creates",
    "pg_ha-standby-2": "a container name the shipped PostgreSQL HA template creates",
    "mssql_ha-primary": "a container name the shipped SQL Server AG template creates",
    "mssql_ha-secondary": "a container name the shipped SQL Server AG template creates",
    "sql-server": "a service *label* an operator typed, and it names the engine, not a machine",
    # A metric's output label that happens to spell a database name. `201_oracle_backup_health.sh`
    # emits `DBBK|<age>` meaning "database backup"; it named no estate before this estate existed.
    "dbbk": "an output label in a shipped Oracle metric, not the database of the same name",
    # Platform and placeholder vocabulary.
    "windows": "a platform", "linux": "a platform", "docker": "a runtime",
    "local": "an access method", "localhost": "not a machine anyone else has",
    "default": "a placeholder", "none": "a placeholder", "null": "a placeholder",
    "server": "a common noun", "database": "a common noun", "instance": "a common noun",
    "primary": "a role", "secondary": "a role", "standby": "a role",
    "0.0.0.0": "bind-all, not an address", "127.0.0.1": "loopback", "::1": "loopback",
}

#: The shortest term worth searching for. Below this a "match" is a coincidence: a two-letter site
#: code appears inside a hundred ordinary words.
MIN_TERM_LENGTH = 4

#: Substrings that make a hit deliberate rather than a leak, with the reason each is allowed.
#: Every one of these was a false positive found the hard way, and `CONTRIBUTING.md` records the
#: first two as rules rather than as exceptions.
ALWAYS_ALLOWED: dict[str, str] = {
    "192.0.2.": "RFC 5737 documentation range - reserved so an example can never be a real machine",
    "198.51.100.": "RFC 5737 documentation range",
    "203.0.113.": "RFC 5737 documentation range",
    "192-0-2-": "RFC 5737, hyphenated inside a server_id",
    "198-51-100-": "RFC 5737, hyphenated",
    "203-0-113-": "RFC 5737, hyphenated",
    "192_0_2_": "RFC 5737, underscored inside a secret ref",
    "198_51_100_": "RFC 5737, underscored",
    "203_0_113_": "RFC 5737, underscored",
    "172.17.0.0": "Docker's default address pool - a fact about Docker, not about this estate",
    "172.18.0.0": "Docker's default address pool",
    "172.30.240.0": "Docker's default address pool",
    "172.30.42.0": "Docker's default address pool",
    "11.2.0.2": "an Oracle version number, not an address - a naive IPv4 regex corrupts it",
    "8.1.7.0": "an Oracle version number",
    "example.com": "RFC 2606 reserved domain",
}


class IdentifierScanError(RuntimeError):
    """The scan could not run. A *finding* is not an error; it is the answer."""


def _spellings(term: str) -> set[str]:
    """Every way this project writes one identifier.

    An address appears dotted in configuration, hyphenated inside a ``server_id``
    (``ACME-192-0-2-248``) and underscored inside a secret ref
    (``ORACLE_203_0_113_121_1522_SYS``). They are one machine, and a scan that knows only the
    dotted form reports a tree as clean while two of the three are still in it.
    """
    forms = {term}
    if "." in term:
        forms.add(term.replace(".", "-"))
        forms.add(term.replace(".", "_"))
    if "-" in term:
        forms.add(term.replace("-", "_"))
    if "_" in term:
        forms.add(term.replace("_", "-"))
    return {form for form in forms if form}


def _harvest(value: Any, into: dict[str, str], kind: str) -> None:
    """Add a configured value, and anything nested under it, as identifiers of *kind*."""
    if isinstance(value, str):
        text = value.strip()
        if len(text) >= MIN_TERM_LENGTH and text.lower() not in GENERIC_TERMS:
            into.setdefault(text, kind)
    elif isinstance(value, (list, tuple)):
        for item in value:
            _harvest(item, into, kind)
    elif isinstance(value, dict):
        for item in value.values():
            _harvest(item, into, kind)


def collect_identifiers(data_dir: str | Path | None = None) -> dict[str, str]:
    """Every real identifier this estate's configuration names, mapped to what kind it is.

    Reading the inventory rather than matching a pattern is the whole design. It also means the
    answer improves on its own: a machine added to `db_instances.json` is searched for from that
    moment, with no map to maintain and no chance of the map and the estate disagreeing.
    """
    terms: dict[str, str] = {}

    try:
        instances = data_sources.load_db_instances(data_dir)
    except Exception as exc:  # noqa: BLE001 - a missing inventory is a usage error, not a crash.
        raise IdentifierScanError(
            f"cannot read the inventory, so there is nothing to search for: {exc}"
        ) from exc

    # `load_db_instances` is best-effort: a missing file yields `[]` rather than raising, which is
    # right for a collector that must not stop the estate's monitoring over one bad path and
    # exactly wrong here. Zero terms means every tree scans clean, so the one failure this checker
    # must never have is the one it would report as success. Say which file was expected.
    if not instances:
        raise IdentifierScanError(
            f"the inventory at {data_sources.db_instances_path(data_dir)} named no instances, so "
            "there is nothing to search for. A scan with no terms reports every tree as clean, "
            "which is why this refuses instead. Pass extra_terms to search without an inventory."
        )

    for record in instances:
        if not isinstance(record, dict):
            continue
        for field in IDENTIFYING_FIELDS:
            _harvest(record.get(field), terms, "inventory")
        _harvest(record.get("database_names"), terms, "database")
        access = record.get("cmd_access")
        if isinstance(access, dict):
            _harvest(access.get("host"), terms, "inventory")
            _harvest(access.get("credential_name"), terms, "credential")

    # Telegram ids and usernames are people, and no address or hostname pattern matches a person.
    # Two of them were hardcoded in 22 places and were found by accident rather than by the scrub.
    for loader, kind in ((data_sources.load_telegram_groups, "chat"),
                         (data_sources.load_telegram_users, "person")):
        try:
            records = loader(data_dir)
        except Exception:  # noqa: BLE001 - these files are optional; the inventory is not.
            continue
        for record in records or []:
            if not isinstance(record, dict):
                continue
            for field in ("group_id", "chat_id", "user_id", "username", "user_name", "full_name"):
                value = record.get(field)
                if value is None:
                    continue
                _harvest(str(value), terms, kind)

    return terms


#: How much a hit is worth acting on. The tiers exist because the inventory legitimately contains
#: ordinary English words - this estate really has databases called `Export`, `Inventory`,
#: `Damage` and `Maintenance` - and a scan that reports every occurrence of "inventory" in
#: `inventory_report.py` buries the four real addresses under fifteen hundred false ones. That is
#: not a hypothetical: the first run of this module did exactly that.
CERTAIN, LIKELY, REVIEW = "certain", "likely", "review"

#: An address, in any of the three spellings this project writes.
ADDRESS_SHAPED = re.compile(r"^\d{1,3}[._-]\d{1,3}[._-]\d{1,3}[._-]\d{1,3}$")


#: Any IPv4-shaped literal. The **backstop** the module docstring promised and did not have until
#: 2026-08-22, when an independent audit found a third party's address in a metric comment and this
#: scanner reported the tree clean. (The address is not repeated here, for the same reason it was
#: removed there.)
#:
#: The reason it was missed is structural, not a bug: terms are derived from `db_instances.json`,
#: which cannot contain a **third party's** address. That one was a linked-server target — a
#: machine this estate talks to and does not own — so no amount of reading the inventory would ever
#: have produced it. Deriving from configuration removes false positives and takes this blind spot
#: in exchange, and the exchange is only safe with a pattern sweep beside it.
#:
#: Reported as `review`: an address that is not in the inventory cannot be classified automatically.
#: It might be a third party's, an example somebody invented, or a version number. A person decides.
#: The trailing guard is `(?!\d)(?!\.\d)`, not `(?![\d.])`. An address at the end of a sentence
#: is followed by a full stop, and the simpler form rejected exactly that — which is how the
#: first version of this backstop found nothing at all, including the leak it was written for.
ADDRESS_LITERAL = re.compile(r"(?<![\d.])((?:\d{1,3}\.){3}\d{1,3})(?!\d)(?!\.\d)")


def unrecognised_addresses(text: str) -> list[str]:
    """IPv4 literals in *text* that are neither documentation ranges nor known-benign.

    Deliberately does **not** try to decide what they are. `11.2.0.2` is an Oracle version,
    `172.17.0.0` is Docker's pool, `0.0.0.0` is bind-all — the first is excluded here and the other
    two by :data:`ALWAYS_ALLOWED`, and everything left is a question for a person.
    """
    found: list[str] = []
    for match in ADDRESS_LITERAL.finditer(text):
        literal = match.group(1)
        octets = literal.split(".")
        if any(int(part) > 255 for part in octets):
            continue  # a version number, or a decimal that happens to have three dots
        window = text[max(0, match.start() - 8): match.end() + 8]
        if any(fragment in window for fragment in ALWAYS_ALLOWED):
            continue
        if literal.startswith(("0.", "127.", "255.")) or literal.endswith(".0.0.0"):
            continue
        found.append(literal)
    return found


def confidence(term: str) -> str:
    """How certain a hit on *term* is to be a leak rather than a coincidence.

    ``certain`` — an address, or a token carrying both a digit and a separator (`server_id`,
    a credential ref). Nothing in ordinary prose looks like that, so a match is the estate.

    ``likely`` — a distinctive token: all-caps, or carrying an underscore. A four-letter database
    code is here. Matched on a word boundary and case-sensitively, so a short code does not match
    a longer name that starts with it, and an all-caps name does not match a lowercase variable
    spelled the same.

    ``review`` — an ordinary word that happens to be a database name. Reported, counted
    separately, and **never** in the headline number: acting on it mechanically is how a scrub
    renames a Python function called `export`.
    """
    if ADDRESS_SHAPED.match(term):
        return CERTAIN
    if any(char.isdigit() for char in term) and any(sep in term for sep in "._-"):
        return CERTAIN
    if "_" in term or (term.isupper() and len(term) >= MIN_TERM_LENGTH):
        return LIKELY
    return REVIEW


def _search_terms(terms: dict[str, str]) -> dict[str, tuple[str, str, str]]:
    """Expand each identifier into its spellings, keyed by the form actually searched.

    An address is keyed lowercase and matched case-insensitively, because it has no case. Every
    other term keeps its case, because case is most of what distinguishes the database `Export`
    from the verb.
    """
    expanded: dict[str, tuple[str, str, str]] = {}
    for term, kind in terms.items():
        level = confidence(term)
        for spelling in _spellings(term):
            key = spelling.lower() if level == CERTAIN else spelling
            expanded.setdefault(key, (term, kind, level))
        for short in _address_shorthands(term):
            expanded.setdefault(short, (term, kind, LIKELY))
    return expanded


#: A two-octet shorthand key, in any of the three spellings prose uses.
_SHORTHAND_KEY = re.compile(r"\d{1,3}[._-]\d{1,3}")


def _address_shorthands(term: str) -> set[str]:
    """How prose actually refers to a machine: by the part of its address that differs.

    Nobody writing a sentence repeats the whole address — a document says "measured on 2.248", and
    everyone on that estate knows which machine that is. The full-address spellings never match it,
    so the scan certified documents that name the estate on every page.

    `LIKELY` rather than `CERTAIN` and deliberately so: two octets are short enough to collide with
    an ordinary number — a percentage, a duration, a version — and a tier that cries wolf is one
    people learn to switch off. Reported, not asserted.
    """
    parts = term.split(".")
    if len(parts) != 4 or not all(p.isdigit() for p in parts):
        return set()
    tail = f"{parts[2]}.{parts[3]}"
    return {tail, tail.replace(".", "-"), tail.replace(".", "_")}


def _patterns(searchable: dict[str, tuple[str, str, str]]) -> list[tuple[re.Pattern[str], bool]]:
    """Two patterns, because the two halves are matched by different rules.

    Addresses are substrings and case-insensitive — `192.0.2.248` is inside
    `ACME-192-0-2-248-MSSQL` and must be found there. Everything else is a whole word and
    case-sensitive, which is the single change that took this module from 1,550 hits to a number
    a person can read.
    """
    loose = sorted((key for key, value in searchable.items() if value[2] == CERTAIN),
                   key=len, reverse=True)
    rest = [key for key, value in searchable.items() if value[2] != CERTAIN]
    # Two-octet shorthand needs a boundary the others do not: `2.3` sits inside `10.1.2.3`, so the
    # ordinary `(?<![\w-])` lets the shorthand match the very address it was derived from and every
    # full-address hit is counted twice. Excluding a neighbouring `.` keeps it to the prose form.
    shorthand = sorted((k for k in rest if _SHORTHAND_KEY.fullmatch(k)), key=len, reverse=True)
    strict = sorted((k for k in rest if not _SHORTHAND_KEY.fullmatch(k)), key=len, reverse=True)
    built: list[tuple[re.Pattern[str], bool]] = []
    if loose:
        built.append((re.compile("|".join(re.escape(k) for k in loose), re.IGNORECASE), True))
    if strict:
        built.append((re.compile(r"(?<![\w-])(?:" + "|".join(re.escape(k) for k in strict)
                                 + r")(?![\w-])"), False))
    if shorthand:
        # The boundary has to reject two things and accept a third, and the obvious `(?<![\w.-])`
        # gets the third wrong. Reject a neighbouring **digit or dot**, which is what keeps the
        # shorthand from matching inside the address it came from, or inside a version number.
        # Accept a neighbouring letter, underscore or hyphen, because that is how prose, filenames
        # and credential refs actually write it — treating `_` as a word boundary meant every
        # `<something>_<octet>_<octet>` name read as clean, and 30 of them had.
        built.append((re.compile(r"(?<![\d.])(?<!\d-)(?<!\d_)(?:"
                                 + "|".join(re.escape(k) for k in shorthand)
                                 + r")(?!\d)(?![.\-_]\d)"), False))
    return built


def _files(paths: Iterable[str], extensions: Iterable[str], root: Path) -> list[Path]:
    wanted = {ext.lower() for ext in extensions}
    found: list[Path] = []
    for name in paths:
        target = root / name
        if target.is_file():
            found.append(target)
            continue
        if not target.is_dir():
            continue
        for child in sorted(target.rglob("*")):
            if not child.is_file():
                continue
            if any(part in SKIP_DIRS for part in child.parts):
                continue
            if child.suffix.lower() in wanted:
                found.append(child)
    return found


def _allowed(line: str, column: int, extra: Iterable[str]) -> str:
    """The reason this hit is deliberate, or an empty string.

    Checked against the surrounding text rather than the matched token, because the thing that
    makes `172.17.0.0` allowed is the `/16` after it — the token alone cannot say.
    """
    window = line[max(0, column - 8): column + 40]
    for fragment, reason in ALWAYS_ALLOWED.items():
        if fragment in window:
            return reason
    for fragment in extra:
        if fragment and fragment in line:
            return "allowed by the request"
    return ""


def scan(request: dict[str, Any] | None = None, *, data_dir: str | Path | None = None) -> dict[str, Any]:
    """Find this estate's identifiers in the files that ship. A hit is the answer, never an error."""
    request = dict(request or {})
    root = Path(request.get("root") or TOOL_ROOT)
    paths = tuple(request.get("paths") or DEFAULT_PATHS)
    extensions = tuple(request.get("extensions") or DEFAULT_EXTENSIONS)
    extra_allow = tuple(request.get("allow") or ())
    max_examples = int(request.get("max_examples") or 3)

    terms = collect_identifiers(data_dir) if request.get("from_inventory", True) else {}
    for term in request.get("extra_terms") or ():
        terms.setdefault(str(term), "requested")
    if not terms:
        raise IdentifierScanError("no identifiers to search for; pass extra_terms or an inventory")

    searchable = _search_terms(terms)
    patterns = _patterns(searchable)

    findings: dict[str, dict[str, Any]] = {}
    unrecognised: dict[str, set[str]] = {}
    allowed_count = 0
    scanned = 0
    for path in _files(paths, extensions, root):
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        scanned += 1
        relative = path.relative_to(root).as_posix()

        # The backstop, per file: any IPv4 literal the inventory does not name. Reported as
        # `review` because it cannot be classified automatically — a third party's address, an
        # invented example and a version number all look the same from here.
        for literal in unrecognised_addresses(text):
            bucket = findings.setdefault(relative, {
                "file": relative, "certain": 0, "likely": 0, "review": 0,
                "terms": {}, "examples": [],
            })
            bucket["review"] += 1
            bucket["terms"].setdefault(literal, REVIEW)
            unrecognised.setdefault(literal, set()).add(relative)
        for number, line in enumerate(text.splitlines(), start=1):
            for pattern, fold_case in patterns:
                for match in pattern.finditer(line):
                    if _allowed(line, match.start(), extra_allow):
                        allowed_count += 1
                        continue
                    key = match.group(0).lower() if fold_case else match.group(0)
                    found = searchable.get(key)
                    if found is None:
                        continue
                    term, kind, level = found
                    bucket = findings.setdefault(relative, {
                        "file": relative, "certain": 0, "likely": 0, "review": 0,
                        "terms": {}, "examples": [],
                    })
                    bucket[level] += 1
                    bucket["terms"][term] = level
                    if level != REVIEW and len(bucket["examples"]) < max_examples:
                        bucket["examples"].append({
                            "line": number,
                            "matched": match.group(0),
                            "confidence": level,
                            "text": line.strip()[:160],
                        })

    files = [item for item in findings.values() if item["certain"] or item["likely"]]
    files.sort(key=lambda item: (-(item["certain"] + item["likely"]), item["file"]))
    for item in files:
        item["terms"] = sorted(term for term, level in item["terms"].items() if level != REVIEW)

    review_only = sorted(
        item["file"] for item in findings.values() if not (item["certain"] or item["likely"])
    )
    certain = sum(item["certain"] for item in findings.values())
    likely = sum(item["likely"] for item in findings.values())
    review = sum(item["review"] for item in findings.values())

    return {
        "root": str(root),
        "paths": list(paths),
        "identifiers_searched": len(terms),
        "spellings_searched": len(searchable),
        "files_scanned": scanned,
        "files_with_findings": len(files),
        # `hits` is what a gate acts on, and it deliberately excludes `review`: those are ordinary
        # words that happen to be database names, and mechanically rewriting them is how a scrub
        # renames a Python function called `export`.
        "hits": certain + likely,
        "certain": certain,
        "likely": likely,
        "review": review,
        "review_only_files": review_only,
        # Addresses no configuration names. Not counted in `hits` — they cannot be classified
        # without a person — but listed on their own, because this is the one category the
        # inventory-derived half is structurally unable to find.
        "unrecognised_addresses": {
            literal: sorted(files) for literal, files in sorted(unrecognised.items())
        },
        "allowed": allowed_count,
        "files": files,
    }


def format_report(outcome: dict[str, Any], *, limit: int = 40) -> str:
    """A human-readable summary. The JSON is the contract; this is for reading in a terminal."""
    lines = [
        f"searched {outcome['identifiers_searched']} identifier(s) "
        f"({outcome['spellings_searched']} spellings) across {outcome['files_scanned']} file(s)",
        f"{outcome['hits']} hit(s) to act on in {outcome['files_with_findings']} file(s): "
        f"{outcome['certain']} certain, {outcome['likely']} likely",
        f"{outcome['review']} more are ordinary words that are also database names - "
        f"reported, not counted; {outcome['allowed']} deliberate",
    ]
    unrecognised = outcome.get("unrecognised_addresses") or {}
    if unrecognised:
        lines.append(
            f"{len(unrecognised)} address(es) no configuration names - decide each by hand:")
        for literal, files in list(unrecognised.items())[:10]:
            lines.append(f"     {literal:18} {', '.join(files[:3])}")
    for item in outcome["files"][:limit]:
        total = item["certain"] + item["likely"]
        lines.append(f"  {total:4}  {item['file']}   {', '.join(item['terms'][:4])}")
    if len(outcome["files"]) > limit:
        lines.append(f"  ... and {len(outcome['files']) - limit} more file(s)")
    return "\n".join(lines)
