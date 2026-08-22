"""What a target *is*, and which tool that implies — as values, decided from the request.

The third member of the family that starts with :mod:`db_ops.lib.sql_access` (how to reach a
database) and :mod:`db_ops.lib.cmd_access` (how to reach a host). Those two answer *by which
transport*; this one answers *with which tool*, which is a different question and was the one
nobody was asking.

**Why it exists.** The estate spans Oracle 8.1.7 to 23, SQL Server 2008 R2 to 2022 and Windows
Server 2003 to 2025, and until 2026-08-19 every entry point in ``common`` knew the engine and the
OS *family* and not one of them knew a *version*. ``run-sql`` therefore could not tell an 8i
instance from a 23c one: it handed both to python-oracledb, which speaks only 12.1 and newer in
thin mode, and the 8i targets survived only because somebody had written
``sql_access.method: "api"`` on each of them by hand. ``ACME-192-0-2-136`` is the one where that
hand-editing was missed, and it fails with ``DPY-3010`` — a driver code that names neither the
cause nor the fix. Meanwhile the rule that *would* have answered it already existed, tested and in
production, inside ``metrics/collector.py``: pick the variant whose ``min_major_version`` /
``max_major_version`` window contains this target. One app could select by version and nothing
else could.

So the rule moved down here, where every caller can reach it, and it is written as **pure
functions of their arguments**: nothing in this module opens a file, reads ``data/``, or imports
an app. A profile is built from whatever the caller already has — a request that states it, an
inventory record it happened to read, or both — and the answer says **which of those sources
decided**, because a selection you cannot attribute is a selection you cannot debug.

The three vocabularies stay separate on purpose:

* ``platform`` — ``windows`` / ``linux``: *which OS family*, and therefore which shell.
* ``os_major``/``os_minor`` — the NT or kernel version: *how old*, and therefore which cmdlets
  exist. ``hostcmd`` conflates this with the next one; see :func:`hostcmd_runtime`.
* ``runtime`` — ``host`` / ``docker`` / ``k8s``: *what the command runs inside*.

An unknown field is ``None`` or ``""`` and never a guess. Every rule below is written to fall back
to today's behaviour when a fact is missing, so adding this module to a call path changes nothing
until the caller starts supplying facts — which is the only way a change like this can be shipped
against a live estate.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, replace
from typing import Any, Iterable, Sequence

from db_ops.lib.cmd_access import PLATFORM_LINUX, PLATFORM_WINDOWS, infer_platform_from_os
from db_ops.lib.coerce import as_optional_int as _optional_int
from db_ops.lib.sql_access import normalize_db_type

__all__ = [
    "ORACLE_THIN_MIN_MAJOR",
    "POWERSHELL_CIM_MIN_NT",
    "RUNTIME_DOCKER",
    "RUNTIME_HOST",
    "RUNTIME_K8S",
    "SOURCE_CONFIG",
    "SOURCE_DEFAULT",
    "SOURCE_REQUEST",
    "SOURCE_RULE",
    "SUPPORTED_RUNTIMES",
    "TargetProfile",
    "ToolChoice",
    "ToolSelectionError",
    "candidate_variants",
    "hostcmd_runtime",
    "parse_os_version",
    "select_oracle_client_mode",
    "select_powershell_dialect",
    "select_sqlserver_driver",
    "select_variant",
    "version_matches",
    "windows_management_transport_available",
]


# --------------------------------------------------------------------------- #
# Vocabulary
# --------------------------------------------------------------------------- #
RUNTIME_HOST = "host"
RUNTIME_DOCKER = "docker"
RUNTIME_K8S = "k8s"
SUPPORTED_RUNTIMES = frozenset({RUNTIME_HOST, RUNTIME_DOCKER, RUNTIME_K8S})

#: Where a decision came from. Carried on every :class:`ToolChoice` because "why did it use
#: pymssql?" has four possible answers and they lead to four different places to go and edit.
SOURCE_REQUEST = "request"
SOURCE_CONFIG = "config"
SOURCE_RULE = "rule"
SOURCE_DEFAULT = "default"

#: python-oracledb's thin mode implements the 12.1 protocol and refuses anything older with
#: ``DPY-3010``. This is the floor that makes an 8i/9i/10g/11g target a *configuration* question
#: (bridge or thick client) rather than a driver error.
ORACLE_THIN_MIN_MAJOR = 12

#: ``Get-CimInstance`` and ``ConvertTo-Json`` arrived with PowerShell 3.0, which ships with
#: Windows 8 / Server 2012 — NT 6.2. Below it the same facts need ``Get-WmiObject``, and a script
#: that assumes otherwise fails as an unknown cmdlet rather than as "your PowerShell is too old".
POWERSHELL_CIM_MIN_NT = (6, 2)

#: Windows Server 2003 is NT 5.2. It ships no WinRM at all (WS-Management 1.1 is an add-on that
#: still cannot host PowerShell remoting) and cannot run the OpenSSH server, so *neither* of the
#: two transports ``lib.cmd_access`` supports can exist there. Knowing this from the profile is
#: what turns a connect timeout into a sentence.
WINDOWS_NO_MANAGEMENT_TRANSPORT_MAX_NT = (5, 99)

#: Product name -> NT version, for the captions that carry no dotted number ("Windows Server
#: 2003"). Only the families this estate actually holds; an unknown name yields ``None`` rather
#: than a guess, because a wrong version silently selects a wrong tool.
_WINDOWS_PRODUCT_NT = (
    ("2025", (10, 0)),
    ("2022", (10, 0)),
    ("2019", (10, 0)),
    ("2016", (10, 0)),
    ("2012 r2", (6, 3)),
    ("2012", (6, 2)),
    ("2008 r2", (6, 1)),
    ("2008", (6, 0)),
    ("2003 r2", (5, 2)),
    ("2003", (5, 2)),
)

#: A dotted version inside an OS caption: "Windows Server 2019 Datacenter **10.0** (Build 17763)".
#: Anchored on a word boundary so the four-digit product year cannot be read as one.
_NT_VERSION_RE = re.compile(r"\b(\d{1,2})\.(\d{1,2})\b")


class ToolSelectionError(RuntimeError):
    """The stated facts rule out every tool — an operator message, with the fix in it.

    Distinct from a driver's own exception for the reason the whole module exists: ``DPY-3010``
    tells you a protocol was refused, this tells you which of ``sql_access.method`` /
    ``oracle_client_mode`` to set and why.
    """


@dataclass(frozen=True)
class ToolChoice:
    """Which tool, and *who decided* — request, config, rule, or the engine default."""

    tool: str
    chosen_by: str = SOURCE_DEFAULT
    reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {"tool": self.tool, "chosen_by": self.chosen_by, "reason": self.reason}


# --------------------------------------------------------------------------- #
# The profile
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class TargetProfile:
    """Everything a tool choice is made from. Unknown stays unknown — never guessed."""

    db_type: str = ""
    major_version: int | None = None
    platform: str = ""
    os_text: str = ""
    os_major: int | None = None
    os_minor: int | None = None
    runtime: str = ""
    #: Which source supplied each field, keyed by field name. Empty for a profile nobody
    #: attributed; :meth:`merge` fills it as facts arrive, which is what lets an answer say
    #: "major_version came from the request, platform from config".
    sources: dict[str, str] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        # A frozen dataclass with a mutable default cannot use `field(default_factory=...)` and
        # stay hashable-looking to callers that copy it around, so normalize here instead.
        object.__setattr__(self, "sources", dict(self.sources or {}))

    # -- construction ------------------------------------------------------ #
    @classmethod
    def from_json(cls, payload: Any, *, source: str = SOURCE_REQUEST) -> "TargetProfile":
        """Build a profile from a request object (or an inventory record — same field names).

        Accepts the spellings both shapes already use: ``db_type``, ``major_version`` (or
        ``<engine>_major_version``, which is what ``db_instances.json`` writes for SQL Server),
        ``platform``, ``os``/``os_text``, ``os_major``/``os_minor``, ``runtime``, and
        ``container_name`` — a target with a container is running in one, so the runtime follows
        from it rather than being stated twice.

        Unknown keys are ignored, so a caller may hand the whole instance record over.
        """
        if isinstance(payload, TargetProfile):
            return payload
        if not isinstance(payload, dict):
            return cls()

        db_type = normalize_db_type(payload.get("db_type") or "")
        major = _optional_int(
            payload.get("major_version")
            if payload.get("major_version") not in (None, "")
            else payload.get(f"{db_type}_major_version") if db_type else None
        )
        os_text = str(payload.get("os_text") or payload.get("os") or "").strip()
        platform = str(payload.get("platform") or "").strip().lower() or infer_platform_from_os(os_text)

        os_major = _optional_int(payload.get("os_major"))
        os_minor = _optional_int(payload.get("os_minor"))
        if os_major is None and os_text:
            os_major, os_minor = parse_os_version(os_text, platform=platform)

        runtime = str(payload.get("runtime") or "").strip().lower()
        if not runtime and str(payload.get("container_name") or "").strip():
            runtime = RUNTIME_DOCKER
        if runtime and runtime not in SUPPORTED_RUNTIMES:
            raise ToolSelectionError(
                f"runtime must be one of {sorted(SUPPORTED_RUNTIMES)}, got {runtime!r}."
            )

        stated = {
            "db_type": db_type,
            "major_version": major,
            "platform": platform,
            "os_text": os_text,
            "os_major": os_major,
            "os_minor": os_minor,
            "runtime": runtime,
        }
        sources = {name: source for name, value in stated.items() if value not in (None, "")}
        return cls(**stated, sources=sources)

    def merge(self, other: "TargetProfile | None") -> "TargetProfile":
        """Fill this profile's blanks from ``other``; **anything already known here wins**.

        The precedence the whole module rests on, applied in one place: a caller builds the
        request's profile and merges the inventory's underneath it, so an explicit
        ``major_version`` in the request is never quietly replaced by a stale config field —
        and a request that states nothing behaves exactly as it did before this module existed.
        """
        if other is None:
            return self
        merged: dict[str, Any] = {}
        sources = dict(other.sources)
        sources.update(self.sources)
        for name in ("db_type", "major_version", "platform", "os_text", "os_major", "os_minor", "runtime"):
            mine = getattr(self, name)
            merged[name] = mine if mine not in (None, "") else getattr(other, name)
        return TargetProfile(**merged, sources=sources)

    # -- reading ----------------------------------------------------------- #
    @property
    def os_version(self) -> tuple[int, int] | None:
        """``(major, minor)`` when known — the form every comparison below wants."""
        if self.os_major is None:
            return None
        return (self.os_major, self.os_minor or 0)

    @property
    def is_windows(self) -> bool:
        return self.platform == PLATFORM_WINDOWS

    @property
    def is_linux(self) -> bool:
        return self.platform == PLATFORM_LINUX

    def with_(self, **changes: Any) -> "TargetProfile":
        """A copy with fields replaced — for a caller narrowing a profile it was handed."""
        return replace(self, **changes)

    def describe(self) -> str:
        """One line for a log or an error message: ``oracle 8 on windows 5.2``."""
        engine = f"{self.db_type or 'unknown engine'}"
        if self.major_version is not None:
            engine += f" {self.major_version}"
        if not self.platform:
            return engine
        host = self.platform
        if self.os_version:
            host += f" {self.os_major}.{self.os_minor or 0}"
        if self.runtime and self.runtime != RUNTIME_HOST:
            host += f" ({self.runtime})"
        return f"{engine} on {host}"

    def to_dict(self) -> dict[str, Any]:
        """The JSON shape callers echo back in their answer."""
        return {
            "db_type": self.db_type,
            "major_version": self.major_version,
            "platform": self.platform,
            "os": self.os_text,
            "os_major": self.os_major,
            "os_minor": self.os_minor,
            "runtime": self.runtime or RUNTIME_HOST,
            "sources": dict(self.sources),
        }


def parse_os_version(os_text: str, *, platform: str = "") -> tuple[int | None, int | None]:
    """Read an NT version out of a free-text OS caption.

    The captions in ``db_instances.json`` are written by ``host-facts`` and are regular enough to
    read: ``Windows Server 2019 Datacenter 10.0 (Build 17763, Hypervisor)``,
    ``Windows NT 6.2 (Build 9200)``, ``Windows Server 2003``. A dotted number wins when present
    because it is the version itself; the product year is the fallback, and only for the families
    this estate holds.

    **Linux gets no version on purpose.** ``Linux (Ubuntu 22.04.5 LTS)`` would parse to ``22.4``,
    which is a distribution release and means nothing to the rules below — they are all about
    which PowerShell exists. A number that looks like an answer and is not one is worse than a
    blank, so Linux returns ``(None, None)``.
    """
    text = str(os_text or "").strip()
    if not text:
        return (None, None)
    resolved_platform = str(platform or "").strip().lower() or infer_platform_from_os(text)
    if resolved_platform != PLATFORM_WINDOWS:
        return (None, None)

    match = _NT_VERSION_RE.search(text)
    if match:
        return (int(match.group(1)), int(match.group(2)))

    lowered = text.lower()
    for token, version in _WINDOWS_PRODUCT_NT:
        if token in lowered:
            return version
    return (None, None)


# --------------------------------------------------------------------------- #
# Variant selection — the rule lifted out of metrics/collector.py
# --------------------------------------------------------------------------- #
#: The db_type spellings a variant may use to mean "any engine".
_ANY_DB_TYPE = frozenset({"", "all", "multi", "*"})


def candidate_variants(
    variants: Iterable[Any],
    profile: TargetProfile,
    *,
    match_platform: bool = False,
) -> list[Any]:
    """The variants that could apply to this target, before the version window is considered.

    A variant is any object carrying ``db_type`` and (optionally) ``platform`` — duck-typed so
    this module stays free of the metric model it was extracted from.

    ``match_platform`` is the OS-side collector's rule: an OS metric has a Windows file and a
    Linux file and the engine is irrelevant, so its variants are filtered by platform and accept
    a wildcard db_type. A SQL-side variant is the mirror: engine decides, platform is not asked.
    """
    result = []
    for variant in variants:
        variant_db_type = str(getattr(variant, "db_type", "") or "")
        if match_platform:
            variant_platform = str(getattr(variant, "platform", "") or "")
            if variant_platform and variant_platform != profile.platform:
                continue
            if variant_db_type not in _ANY_DB_TYPE and variant_db_type != profile.db_type:
                continue
        elif variant_db_type != profile.db_type:
            continue
        result.append(variant)
    return result


def version_matches(variant: Any, major_version: int | None) -> bool:
    """Is this variant's ``min``/``max`` major-version window open for that version?

    An unknown version matches everything: the caller decides what to do with "no version was
    stated", and silently excluding every gated variant would be the worst of the options.
    """
    if major_version is None:
        return True
    minimum = getattr(variant, "min_major_version", None)
    maximum = getattr(variant, "max_major_version", None)
    if minimum is not None and major_version < int(minimum):
        return False
    if maximum is not None and major_version > int(maximum):
        return False
    return True


def select_variant(candidates: Sequence[Any], profile: TargetProfile) -> Any | None:
    """The variant to run on this target, or ``None`` when none fits.

    Lifted verbatim from ``metrics/collector.py::_resolve_metric_file_path`` (2026-08-19) so that
    ``run-sql`` can make the same choice the collector just made — reproducing a metric by hand on
    an 8i or 2008 R2 instance was impossible while this rule lived in one app.

    Two behaviours worth keeping in sight, both preserved exactly:

    * **Unsupported variants are skipped, not selected.** A variant marked ``supported: false``
      carries a reason and exists to explain a gap, not to be run.
    * **With no version stated, the LAST supported candidate wins** — the catalog is written
      oldest-first, so the last one is the modern variant. Right for the 32 instances that carry
      no ``major_version`` today and wrong the moment one of them is old, which is why
      ``docs/04_metrics_engine.md`` asks for the field rather than leaning on this.
    """
    supported = [
        variant for variant in candidates
        if bool(getattr(variant, "supported", True)) and getattr(variant, "path", None) is not None
    ]
    if profile.major_version is None:
        return supported[-1] if supported else None
    for variant in candidates:
        if not bool(getattr(variant, "supported", True)):
            continue
        if version_matches(variant, profile.major_version):
            return variant
    return None


# --------------------------------------------------------------------------- #
# Tool selection
# --------------------------------------------------------------------------- #
def select_oracle_client_mode(profile: TargetProfile, requested: str = "") -> ToolChoice:
    """``thin`` or ``thick`` for python-oracledb — or a refusal naming the two ways out.

    The one rule in this module that changes what happens rather than only describing it, and it
    earns that by replacing a driver code with an instruction. Thin mode speaks the 12.1 protocol
    and nothing older; on ``ACME-192-0-2-136`` (Oracle 8.1.7, ``major_version`` unset,
    ``sql_access`` unset) the connect fails with ``DPY-3010`` and the operator is left to work out
    that the fix is a bridge that already exists and serves two sibling hosts.

    An unstated version still connects thin, exactly as before — this refuses what is *known* to
    be impossible, never what is merely unknown.
    """
    wanted = str(requested or "").strip().lower()
    if wanted in {"thick", "thin"}:
        return ToolChoice(wanted, SOURCE_REQUEST, "explicitly requested")
    if wanted:
        raise ToolSelectionError(
            f"oracle_client_mode must be 'thin' or 'thick', got {wanted!r}."
        )

    major = profile.major_version
    if major is not None and major < ORACLE_THIN_MIN_MAJOR:
        raise ToolSelectionError(
            f"Oracle {major} cannot be reached by python-oracledb in thin mode "
            f"(it speaks {ORACLE_THIN_MIN_MAJOR}.1 and newer). Either route this target through "
            'the legacy bridge with sql_access {"method": "api", "bridge_url": "..."}, or '
            'install an Oracle client and set "oracle_client_mode": "thick".'
        )
    return ToolChoice("thin", SOURCE_DEFAULT, "python-oracledb default")


def select_sqlserver_driver(profile: TargetProfile, requested: str = "", configured: str = "") -> ToolChoice:
    """Which SQL Server driver to open with, and who decided.

    **There is no version rule here, and that is a finding rather than an omission.** The obvious
    one — put Driver 17 / ``Encrypt=no`` first for 2008 R2 and older, because Driver 18 refuses
    TLS 1.0 — was written on 2026-08-19 and then measured against all four 10.50 instances in this
    estate. Every one of them completes on **Driver 18 with ``Encrypt=optional``, first attempt,
    no fallback**. The rule would have bought no round trip and cost real encryption
    (``optional`` encrypts when the server offers a certificate; ``no`` is plaintext), so it was
    removed. See ``sqlserver_driver_candidates`` for the same note next to the order itself.

    What is left is attribution: request beats config beats the engine default, and the answer
    says which. That was the actual gap — pyodbc and pymssql were indistinguishable from the
    outside despite binding parameters differently.
    """
    wanted = str(requested or "").strip()
    if wanted:
        return ToolChoice(wanted, SOURCE_REQUEST, "explicitly requested")
    if str(configured or "").strip():
        return ToolChoice(str(configured).strip(), SOURCE_CONFIG, "sqlserver_driver on the instance")
    return ToolChoice("auto", SOURCE_DEFAULT, "ODBC with pymssql fallback")


def select_powershell_dialect(profile: TargetProfile) -> ToolChoice:
    """``cim`` (PowerShell 3.0+) or ``wmi`` (PowerShell 2.0) for a Windows fact script.

    ``host_ops``' fact script is written in ``Get-CimInstance`` + ``ConvertTo-Json``, both of which
    arrived in PowerShell 3.0 / NT 6.2. On an older Windows it fails as an unknown cmdlet — a
    message that sends the reader looking for a permissions problem. Nothing in the estate is that
    old *and* reachable today, so this returns the answer rather than changing the script: the
    dialect belongs in the profile now, and the ``wmi`` script can be written when a host needs it.
    """
    version = profile.os_version
    if version is None:
        return ToolChoice("cim", SOURCE_DEFAULT, "OS version unknown; assuming PowerShell 3.0+")
    if version >= POWERSHELL_CIM_MIN_NT:
        return ToolChoice("cim", SOURCE_RULE, f"NT {version[0]}.{version[1]} has PowerShell 3.0+")
    return ToolChoice(
        "wmi", SOURCE_RULE,
        f"NT {version[0]}.{version[1]} ships PowerShell 2.0: Get-CimInstance/ConvertTo-Json do not exist",
    )


def windows_management_transport_available(profile: TargetProfile) -> bool:
    """Can *any* transport ``lib.cmd_access`` supports exist on this Windows?

    False only for NT 5.x — Windows Server 2003 and older, where WinRM is not in the base install
    and the OpenSSH server cannot run at all. ``192.0.2.235`` and ``192.0.2.236`` are the two
    here: both answer on RDP 3389 and refuse 5985/5986, and both carry ``cmd_access: null`` plus
    nine hand-listed ``OS_*`` codes in ``report_policy.disabled_metric_codes``. That deny-list is
    what this predicate exists to replace.
    """
    if not profile.is_windows:
        return True
    version = profile.os_version
    if version is None:
        return True
    return version > WINDOWS_NO_MANAGEMENT_TRANSPORT_MAX_NT


def hostcmd_runtime(profile: TargetProfile) -> str:
    """This profile in ``hostcmd``'s vocabulary — ``windows``/``linux``/``docker``/``k8s``.

    ``hostcmd.Host.runtime`` answers "what does the command run inside" and "which OS" with one
    field, so a container on Linux is ``docker`` and a Windows VM is ``windows``. That collapse is
    why ``run-cmd`` (which uses ``host_ops``' separate ``method`` + ``platform``) cannot target a
    container while ``backup-database`` can. Keeping the two vocabularies apart *here* and
    translating at the boundary is the first half of closing that: the profile stays precise, and
    the existing callers keep the string they already parse.
    """
    if profile.runtime in {RUNTIME_DOCKER, RUNTIME_K8S}:
        return profile.runtime
    return profile.platform or PLATFORM_LINUX
