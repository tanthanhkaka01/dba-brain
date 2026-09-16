"""Stable fake names for a real estate's identifiers, so a real page can be shown to strangers.

Pure. The caller decides *which* strings are identifiers — from the operator's own configuration and
store, never from a pattern — and this module decides *what each one becomes*.

Three properties, and every one of them is what makes the output usable rather than merely scrubbed:

* **Stable.** The same real name yields the same fake name on every page and on every day of the
  window. A showcase where `SALESDB` is one thing on Monday's inventory page and another on
  Tuesday's index page is not a record of an estate, it is noise.
* **Shaped like the original.** `PAYROLL_Prod` becomes another `<Word>_Prod`, a `server_id` keeps its
  `ORG-ADDRESS-ENGINE-PORT` construction, an index keeps its `IX_` prefix. The point of publishing
  real pages is that they read like real pages; a column of `db_1`, `db_2` teaches nothing about
  what the tool shows.
* **Safe by construction, not by hope.** Every fake address comes from RFC 5737 documentation
  ranges, which can never be a real machine, and every fake host name is under RFC 2606
  `example.com`. Those are exactly the strings `identifier_scan.ALWAYS_ALLOWED` permits, so the
  output of this module is verifiable by the checker that already exists - and the caller is
  expected to run it and refuse to publish anything it flags.

The mapping is derived by hashing, not by a counter, so it does not depend on the order terms were
discovered in: two runs a week apart over a changed estate still agree about the machines they share.
It is deliberately **not** reversible - there is no key and no table written out. A showcase that
could be turned back into the estate would be the estate.
"""

from __future__ import annotations

import hashlib
import re

#: RFC 5737. Reserved for documentation, so a fake address can never route to a real machine, and
#: `identifier_scan` already allows all three in every spelling.
DOC_NETWORKS: tuple[str, ...] = ("192.0.2", "198.51.100", "203.0.113")

#: RFC 2606. Same guarantee for anything that looks like a host or a domain.
DOC_DOMAIN = "example.com"

#: The organisation prefix every fake `server_id` carries. Short, obviously not a company.
ORG = "ACME"

#: Word pools. Deliberately dull and business-shaped: a showcase page has to look like somebody's
#: estate, and whimsical names read as a toy. Sized past the largest estate measured (22 databases,
#: ~90 tables on one page) so collisions are rare rather than merely unlikely.
_SUBJECTS: tuple[str, ...] = (
    "Sales", "Orders", "Billing", "Ledger", "Payroll", "Staffing", "Roster", "Shift",
    "Invoice", "Customer", "Supplier", "Catalog", "Pricing", "Shipment", "Warehouse",
    "Booking", "Contract", "Claim", "Policy", "Ticket", "Asset", "Meter", "Route",
    "Batch", "Reconcile", "Settlement", "Forecast", "Quota", "Territory", "Campaign",
)
_QUALIFIERS: tuple[str, ...] = (
    "Header", "Detail", "Line", "Entry", "History", "Summary", "Daily", "Monthly",
    "Audit", "Archive", "Staging", "Queue", "Log", "Snapshot", "Result", "Status",
)
_SCHEMAS: tuple[str, ...] = (
    "dbo", "sales", "billing", "ops", "hr", "core", "staging", "report", "audit", "ref",
)
_ENV_SUFFIXES: tuple[str, ...] = ("Prod", "Test", "Stg", "Dev", "UAT", "BK")


def _digest(term: str, salt: str = "") -> int:
    """A stable integer for one term. Order-independent, so two runs agree."""
    return int.from_bytes(
        hashlib.sha256(f"{salt}:{term.casefold()}".encode()).digest()[:8], "big")


def _pick(pool: tuple[str, ...], term: str, salt: str = "") -> str:
    return pool[_digest(term, salt) % len(pool)]


def address(term: str) -> str:
    """A documentation-range address, stable per real address.

    The last octet avoids 0 and 255 so the result is a plausible host rather than a network or a
    broadcast address - a reader who knows subnets should not be distracted by an impossible one.
    """
    value = _digest(term, "addr")
    network = DOC_NETWORKS[value % len(DOC_NETWORKS)]
    return f"{network}.{(value >> 8) % 254 + 1}"


def hostname(term: str) -> str:
    """A host name under the reserved documentation domain, or a bare label if the original was."""
    label = f"host-{_digest(term, 'host') % 900 + 100}"
    return label if "." not in term else f"{label}.{DOC_DOMAIN}"


def server_id(term: str, *, address_of=None) -> str:
    """A `server_id`, keeping the shape `ORG-ADDRESS-ENGINE-PORT` where the original had it.

    The address inside is mapped through the **same** function the dotted form uses, so
    `192.0.2.10` and `ACME-192-0-2-10-MSSQL-1433` stay recognisably one machine on the page. That
    is not cosmetic: the whole value of a real page is that a reader can follow one server across
    the inventory, the metrics page and the index report.
    """
    parts = term.split("-")
    rebuilt: list[str] = [ORG]
    index = 0
    while index < len(parts):
        # An address inside a server_id is four numeric parts joined by the same separator.
        window = parts[index:index + 4]
        if len(window) == 4 and all(part.isdigit() for part in window):
            dotted = ".".join(window)
            mapped = (address_of or address)(dotted)
            rebuilt.append(mapped.replace(".", "-"))
            index += 4
            continue
        # The leading part is the organisation label, and :data:`ORG` has already replaced it.
        # This used to keep it whenever it was not purely alphabetic, which let a real prefix
        # through the moment it carried a digit - `ORG1-192-0-2-15-MSSQL25-1433` came back as
        # `ACME-ORG1-192-0-2-15-...`, the estate's own label in front of the fake one.
        if index > 0:
            rebuilt.append(parts[index])
        index += 1
    # A server_id that carried no address still has to change, or the organisation half leaks.
    if len(rebuilt) == 1:
        rebuilt.append(f"NODE-{_digest(term, 'node') % 900 + 100}")
    return "-".join(rebuilt)


def database(term: str) -> str:
    """A database name, keeping an environment suffix when the original carried one."""
    subject = _pick(_SUBJECTS, term, "db")
    for suffix in _ENV_SUFFIXES:
        if term.casefold().endswith(suffix.casefold()):
            separator = "_" if "_" in term else ""
            return f"{subject.upper()}{separator}{suffix}"
    return subject.upper()


def schema(term: str) -> str:
    """A schema name. `dbo` and `public` are the engine's own and are left alone by the caller."""
    return _pick(_SCHEMAS, term, "schema")


def table(term: str) -> str:
    """A table name, in the two-or-three word shape real application tables have."""
    subject = _pick(_SUBJECTS, term, "table")
    qualifier = _pick(_QUALIFIERS, term, "tableq")
    return f"{subject}{qualifier}"


def index(term: str, *, table_of=None) -> str:
    """An index name, keeping the prefix that says what kind of index it is.

    `PK_`, `UQ_`, `IX_`, `FK_` are a convention every SQL Server estate uses and a reader of the
    page relies on: an index report where nothing says which row is a primary key has lost the one
    distinction that decides whether it may be dropped.
    """
    text = str(term or "")
    for prefix in ("PK_", "UQ_", "UX_", "IX_", "NCI_", "CI_", "FK_", "AK_"):
        if text.upper().startswith(prefix):
            body = f"{_pick(_SUBJECTS, text, 'ix')}{_pick(_QUALIFIERS, text, 'ixq')}"
            return f"{prefix}{body}"
    return f"IX_{_pick(_SUBJECTS, text, 'ix')}{_pick(_QUALIFIERS, text, 'ixq')}"


def procedure(term: str) -> str:
    """A routine name, keeping the prefix that says it is one.

    Same reasoning as :func:`index`: `usp_` and `sp_` are a convention a reader of the page uses to
    tell a procedure from a table at a glance, and a showcase that loses it reads as a different
    product. The rest is replaced - a routine called `usp_QuanLyKhachHang_ViewBookingVisitor` says
    what somebody's business does, in their own language.
    """
    text = str(term or "")
    for prefix in ("usp_", "sp_", "fn_", "udf_", "proc_"):
        if text.lower().startswith(prefix):
            return (f"{text[:len(prefix)]}{_pick(_SUBJECTS, text, 'proc')}"
                    f"_{_pick(_QUALIFIERS, text, 'procq')}")
    return f"usp_{_pick(_SUBJECTS, text, 'proc')}_{_pick(_QUALIFIERS, text, 'procq')}"


def person(term: str) -> str:
    """A Telegram display name or username. People are the one term kind with no shape to keep."""
    return f"operator{_digest(term, 'person') % 90 + 10}"


def credential(term: str) -> str:
    """A credential or secret ref. Kept SHOUTY, because that is how every ref in the estate reads."""
    return f"CRED_{_digest(term, 'cred') % 9000 + 1000}"


#: An address, dotted or written into a `server_id`/secret ref with the separator that file uses.
_ADDRESS_SHAPE = re.compile(r"^(?:\d{1,3}[._-]){3}\d{1,3}$")


def inventory(term: str) -> str:
    """Anything `db_instances.json` names, rendered by **shape** rather than by field.

    `identifier_scan.collect_identifiers` reports the whole inventory under one kind, because it
    harvests values and does not model what each field means. Left to fall through to
    :func:`generic`, that made every address, host and `server_id` on a showcase page read
    `redacted4187` — and a showcase whose addresses are not addresses documents nothing, which is
    the one failure this module's docstring promises it does not have.

    The shapes are unambiguous and this project wrote all three: four numeric parts joined by one
    separator is an address; a hyphenated value carrying one is a `server_id`; anything else is a
    host name.
    """
    text = str(term or "").strip()
    if _ADDRESS_SHAPE.match(text):
        return address(text.replace("-", ".").replace("_", "."))
    if "-" in text and any(part.isdigit() for part in text.split("-")):
        return server_id(text)
    return hostname(text)


#: Which function renders each kind `identifier_scan.collect_identifiers` reports. A kind with no
#: entry falls back to `generic`, which is safe but shapeless — see `Mapping.unmapped_kinds`.
BY_KIND = {
    "address": address,
    # Everything `collect_identifiers` harvests out of the inventory arrives under this one kind.
    "inventory": inventory,
    "ip": address,
    "host": hostname,
    "hostname": hostname,
    "server_id": server_id,
    "database": database,
    "db_name": database,
    "service": database,
    "schema": schema,
    "table": table,
    "index": index,
    "procedure": procedure,
    "routine": procedure,
    "credential": credential,
    "credential_name": credential,
    "secret_ref": credential,
    "person": person,
    "telegram_user": person,
    "telegram_group": person,
}


def generic(term: str) -> str:
    """The fallback: recognisably fake, and it does not pretend to keep a shape it never knew."""
    return f"redacted{_digest(term, 'generic') % 9000 + 1000}"


def replacement(term: str, kind: str) -> str:
    """One fake name for one real term of one kind."""
    render = BY_KIND.get(str(kind or "").strip().lower(), generic)
    if render is server_id:
        return server_id(term)
    if render is index:
        return index(term)
    return render(term)


class Mapping:
    """Real term -> fake term, with the replacement order that makes a rewrite correct.

    **Longest first, always.** `PAYROLL` is a substring of `PAYROLL_Prod`; replacing the short one first
    leaves `<fake>_Prod` behind, which is both wrong and a partial leak of the original. The same
    trap caught this project once already, in the opposite direction: a four-letter database code
    was replaced and its sibling was not.
    """

    def __init__(self, terms: dict[str, str] | None = None) -> None:
        self._map: dict[str, str] = {}
        #: Terms that must not match inside a longer number - see :meth:`add_pair`.
        self._bounded: set[str] = set()
        #: Terms to rewrite **wherever they appear**, including inside a longer name - see
        #: :meth:`add`. What the operator names by hand is not a guess that needs a word boundary.
        self._loose: set[str] = set()
        #: The compiled rewrite, thrown away whenever a term is added. See :meth:`_passes`.
        self._compiled: list[tuple[re.Pattern[str], dict[str, str]]] | None = None
        self.unmapped_kinds: set[str] = set()
        #: Configured values the caller judged ordinary words rather than
        #: identifiers, recorded so the decision is visible in the result.
        self.skipped_as_ordinary: list[str] = []
        for term, kind in (terms or {}).items():
            self.add(term, kind)

    def add(self, term: str, kind: str, *, loose: bool = False) -> str:
        """Record one term. ``loose`` rewrites it inside a longer name as well as on its own.

        The default is a whole name, matching how `identifier_scan` searches for one: nothing
        writes `PAYROLL_Prod` inside a longer word, and a rewrite that fired there would be
        scrubbing something the checker would not flag.

        ``loose`` is for a term the **operator named by hand**, where that reasoning does not
        apply - they are naming it precisely because it is buried somewhere the shapes do not
        reach. Measured 2026-09-12: `tanthanh_dba` inside `sqlserver_113.155_MSSQLSERVER_tanthanh_
        dba`, and the organisation label `ORG1` inside every `ORG1-…-MSSQLAG-1533` the page built
        for itself. Both survived a clean certification, because `_` is a word character.
        """
        text = str(term or "").strip()
        if not text:
            return ""
        if loose:
            self._loose.add(text)
            self._compiled = None
        if text not in self._map:
            if str(kind or "").strip().lower() not in BY_KIND:
                self.unmapped_kinds.add(str(kind or "?"))
            self._map[text] = replacement(text, kind)
            self._compiled = None
        return self._map[text]

    def add_pair(self, term: str, fake: str, *, bounded: bool = False) -> str:
        """Record a replacement the caller has already worked out.

        For a term whose fake is **not** independent of another term's: the two-octet shorthand a
        note writes a machine by (`sqlserver_100.86_...`) has to land on the *same* fake machine the
        full address did, or the showcase says two things about one server. An independently
        generated fake would be safe and meaningless.

        ``bounded`` refuses a match that sits **inside a longer number**, and a shorthand must
        always ask for it. Measured on 2026-09-12: `168.1` rewrote the middle of `192.168.1.120`,
        an address no term named, and the page came out carrying `192.100.108.120` - a string that
        is not the estate's and not a documentation address either, invented by the scrub and
        flagged by the certifier as a leak. A fragment is only a machine when it stands alone.
        """
        text = str(term or "").strip()
        if text and text not in self._map:
            self._map[text] = str(fake)
            self._compiled = None
            if bounded:
                self._bounded.add(text)
        return self._map.get(text, "")

    def __len__(self) -> int:
        return len(self._map)

    def as_dict(self) -> dict[str, str]:
        return dict(self._map)

    def ordered(self) -> list[tuple[str, str]]:
        """Every pair, longest real term first, so no replacement can eat another's prefix."""
        return sorted(self._map.items(), key=lambda pair: (-len(pair[0]), pair[0]))

    def apply(self, text: str) -> str:
        """Rewrite every occurrence, in every spelling the estate writes it in.

        Case-insensitive: SQL Server compares object names case-insensitively by default, so the
        same table is `EmployeeShift` in one page and `EMPLOYEESHIFT` in a message built by an
        uppercasing query. A rewrite that only knew one of them would publish the other.

        **One pass over the text, not one per term.** This used to run a `re.sub` for every term
        and every spelling of it, which is fine for the couple of hundred names a configuration
        file holds and impossible for the vocabulary a real page carries: an estate's index report
        names 52,000 objects, and 52,000 scans of 15 MB does not finish. The alternation is ordered
        longest-first, which is what preserves the property the loop had — `PAYROLL` must not be
        replaced inside `PAYROLL_Prod` — and a single pass buys one more: a fake can no longer be
        rewritten by a later term, which the sequential version could do.
        """
        result = str(text or "")
        tokens, loose, bounded = self._passes()
        if tokens:
            result = _TOKEN_RE.sub(
                lambda match: tokens.get(match.group(0).casefold()) or match.group(0), result)
        for entry in (loose, bounded):
            if entry is not None:
                pattern, table = entry
                result = pattern.sub(lambda match: table[match.group(0).casefold()], result)
        return result

    def _passes(self) -> tuple:
        r"""The compiled rewrite, built once per mapping and kept until a term is added.

        Three passes, because three kinds of term are matched by three different rules — and the
        split is what makes a real page's vocabulary affordable at all:

        * **a name** (`IX_FLXSupplier_Type`, `PAYROLL_Prod`) is matched as a whole **token** and
          looked up in a dict. This is where the volume is: an estate's index report names 52,000
          objects, and neither a `re.sub` per term nor one 200,000-branch alternation finishes —
          `re` walks alternatives linearly, so the second is as quadratic as the first. Reading
          each token once and asking a dict is linear in the page. It also matches the checker:
          `identifier_scan` searches a name with `(?<![\w-])…(?![\w-])`, a whole word, so a
          rewrite that fires inside a longer token would be scrubbing what nothing flags.
        * **anything carrying an address** must match *inside* a token, exactly as the checker's
          `certain` tier does: `10.1.2.3` sits inside `ACME-10-1-2-3-MSSQL`, and that `server_id`
          in turn sits inside `index-usage_acme-10-1-2-3-mssql-1433.html`, which is a file name a
          folder listing shows. One alternation covers them, longest first so no replacement eats
          another's prefix — there are a hundred of these on a real estate, not fifty thousand.
          (A term with no address in it does not need this: nothing writes `PAYROLL_Prod` inside a
          longer word, and the checker would not flag it there either.)
        * **a bounded term** — an address shorthand — needs its own guard, and there are fewer
          still.
        """
        if self._compiled is None:
            tokens: dict[str, str] = {}
            loose: dict[str, str] = {}
            bounded: dict[str, str] = {}
            for term, fake in self.ordered():
                if term in self._bounded:
                    into = bounded
                elif (term in self._loose or _CARRIES_ADDRESS.search(term)
                      or not _TOKEN_RE.fullmatch(term)):
                    into = loose
                else:
                    into = tokens
                for spelling, fake_spelling in _spelling_pairs(term, fake):
                    into.setdefault(spelling.casefold(), fake_spelling)
            self._compiled = (tokens,
                              _compile_pass(loose, bounded=False),
                              _compile_pass(bounded, bounded=True))
        return self._compiled

#: Where a bounded term - a two-octet address shorthand - may match: **touching a letter or an
#: underscore on one side, and not sitting inside a longer number on the other**. Deliberately the
#: same rule `identifier_scan._patterns` applies to its shorthand tier, because a scrub that
#: rewrites more than the checker looks for corrupts pages, and one that rewrites less can never
#: certify them.
#:
#: Both halves were measured on 2026-09-12, on one published inventory page. Without the
#: digit guard, `168.1` rewrote the middle of `192.168.1.120`, inventing an address that belongs to
#: nobody. Without the letter requirement, `0.2` - the shorthand of a real `172.19.0.2` - rewrote
#: every `"sharePct": 0.2` in the file, and 53 measurements came out as `100.108`.
_BOUNDED = (r"(?:(?<=[A-Za-z_])(?:{core})(?!\d)(?![.\-_]\d)"
            r"|(?<![\d.])(?<!\d[-_])(?:{core})(?=[A-Za-z_]))")


#: An address in any of the three spellings this project writes - dotted in configuration,
#: hyphenated inside a `server_id`, underscored inside a secret ref. A term carrying one has to be
#: rewritten **inside** whatever token it sits in, because that is where it appears: the whole
#: `server_id` lives inside `index-usage_<server_id>.html`, and a folder listing shows that name.
_CARRIES_ADDRESS = re.compile(r"(?:\d{1,3}[.\-_]){3}\d{1,3}")


#: What counts as one name in a page: letters, digits, `_` and `-`. Deliberately the **same
#: characters `identifier_scan` treats as a word** (`(?<![\w-])…(?![\w-])`), because a scrub that
#: draws the boundary anywhere else disagrees with the checker about what a name is. `$` was in
#: this class for one afternoon and `HOST-NAME$INSTANCE` came out unscrubbed: the checker read a host
#: name ending at the `$`, the rewrite read one long token and found nothing.
#:
#: Also excludes the dot, so `METER_Prod.flx.FLXSupplier.IX_x` is four names rather than one -
#: which is the point, since each part is mapped separately and the item keeps its shape.
_TOKEN_RE = re.compile(r"[A-Za-z0-9_\-]+")


def _compile_pass(table: dict[str, str], *, bounded: bool):
    """One alternation over every spelling in ``table``, longest first, or ``None`` if it is empty.

    Longest first is not a nicety: Python's alternation takes the **first** branch that matches at
    a position, so `PAYROLL` listed before `PAYROLL_Prod` would leave `<fake>_Prod` on the page -
    both wrong and a partial leak of the original.
    """
    if not table:
        return None
    core = "|".join(re.escape(spelling) for spelling in
                    sorted(table, key=lambda spelling: (-len(spelling), spelling)))
    pattern = _BOUNDED.format(core=core) if bounded else core
    return re.compile(pattern, re.IGNORECASE), table


def _spelling_pairs(term: str, fake: str) -> list[tuple[str, str]]:
    """Each way the estate writes ``term``, paired with the fake written the same way.

    Mirrors `identifier_scan._spellings`: an address is dotted in configuration, hyphenated inside
    a `server_id` and underscored inside a secret ref, and a rewrite that knows one spelling leaves
    the other two on the page. The fake is re-spelled to match, so a hyphenated original does not
    become a dotted replacement in the middle of a `server_id`.
    """
    pairs = [(term, fake)]
    if "." in term:
        pairs.append((term.replace(".", "-"), fake.replace(".", "-")))
        pairs.append((term.replace(".", "_"), fake.replace(".", "_")))
    if "-" in term:
        pairs.append((term.replace("-", "_"), fake.replace("-", "_")))
    if "_" in term:
        pairs.append((term.replace("_", "-"), fake.replace("_", "-")))
    # Longest first here too: `192.0.2.10` and `192-0-2-10` are the same length, but a term whose
    # spellings differ in length must not have the short one applied first.
    return sorted(set(pairs), key=lambda pair: (-len(pair[0]), pair[0]))
