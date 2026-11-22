"""A dbabrain key, read offline - and the one answer to "may this feature run" (1.0.0, plan K2).

The operator, 2026-11-21 (translated): *dbabrain needs the email verified through dbabrain.dev and a
key issued for a term; when it ends a new key is fetched* - and *without a key dbabrain still does
the metrics and the instant alerts, but no reports, no backup / restore*. The plan is
``audits/20261121_plan_dbabrain_key_and_ai_relay.md``.

A key is a token dbabrain.dev signs (``connector/worker/src/key-token.ts`` - the same format; both
sides are tested against ``tests/fixtures/dbabrain_key_vector.json``)::

    dbbk1.<payload, base64url JSON>.<Ed25519 signature of "dbbk1.<payload>", base64url>

dbabrain ships the public half of the signing key (:data:`SIGNING_PUBLIC_KEYS`), so the signature
and the term are checked here with no network: a node off the internet still knows its key.

The rules, pure:

* **no key, or not one dbabrain.dev signed** - a gated feature (:data:`FEATURES`) does not run; it
  says once what is missing and how to get it. Everything else (metrics, instant alerts, SLA, SQL
  tasks, the scheduler) runs with no key at all;
* **the term** (14 days; the operator's own account 365): warnings 14, 7, 3 and 1 day(s) before its
  end (:data:`WARN_DAYS`, the last day CRITICAL) - only the steps shorter than the key's own term, so a
  14-day key is not warned on the day it is issued (the operator, 2026-11-21, after the worker's first
  hour on 1.0.0: its new key warned at once);
* **a grace of 3 days after it** (:data:`GRACE_DAYS`): gated features still run, CRITICAL every day;
* **after the grace: new work stops, the existing never breaks** - a restore that was asked for still
  runs (``new_work=False``), nothing already backed up becomes unreadable;
* **revoked**, learnt at the daily online check: as after the grace. A check that failed (no network,
  dbabrain.dev down) is not a revocation - the offline answer stands.

dbabrain is open source: a check here can be removed by whoever runs a modified copy. The signature
stops forged keys, not patched code; what cannot be patched away is dbabrain.dev's own check of the
key (the AI relay, the public connector).

``cryptography`` is imported where the signature is checked, as ``secret_text`` does.
"""

from __future__ import annotations

import base64
import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping

from dbabrain.lib import errors

PREFIX = "dbbk1"
#: dbabrain.dev's service: where a key is asked for and checked, and the MCP connector's node link.
DEFAULT_SERVER = "https://mcp.dbabrain.dev"
#: The secret-store ref the node keeps its key under - a bearer credential for the AI relay, so
#: never a file in clear, never in an image, never on an argv.
KEY_REF = "DBABRAIN_KEY"
GRACE_DAYS = 3
#: Days before the end a warning is said - once each; the last one CRITICAL (D1).
WARN_DAYS = (14, 7, 3, 1)

#: What a key unlocks (plan §2, D2/D3) - the names are the Worker's (``FREE_FEATURES``).
FEATURES: dict[str, str] = {
    "reports": "the reports and the console's report pages",
    "backup_restore": "backups and restores",
    "sre": "the SRE commands",
    "mcp": "the MCP server (local and the dbabrain.dev connector)",
    "assistant": "the assistant (with your own Anthropic key)",
    "ai_relay": "the AI through dbabrain.dev",
}

#: The signing keys whose keys this release accepts: skid -> the raw Ed25519 public key (base64url,
#: a JWK's ``x``). The skid is the first 16 hex of the SHA-256 of the raw key, so it cannot name
#: another key. A new signing key is added here - an old one is kept while its keys can be live.
SIGNING_PUBLIC_KEYS: dict[str, str] = {
    # The first, made 2026-11-21 for the Worker at mcp.dbabrain.dev. Its private half is the Worker's
    # secret KEY_SIGNING_JWK and an offline copy in the master's gitignored .secret/ - never a node's
    # secret store, which a deploy copies to the worker (the operator: dbabrain.dev's keys stay there).
    "787d82afaa9608f3": "FNLvesDgRbOtCxSG57RiNuylcRsYxawsW41DKRCOTCY",
}

#: How to get one, said wherever a gated feature is refused.
HOW_TO_GET = "run `dbabrain key-request` (an email sign-in on dbabrain.dev, free)"


class KeyInvalid(errors.RequestError):
    """The text is not a dbabrain key dbabrain.dev signed."""


@dataclass(frozen=True)
class DbabrainKey:
    kid: str
    email: str
    plan: str
    features: tuple[str, ...]
    issued_at: datetime
    expires_at: datetime
    node: str
    skid: str

    @property
    def grace_ends_at(self) -> datetime:
        return self.expires_at + timedelta(days=GRACE_DAYS)

    def describe(self) -> dict[str, Any]:
        return {
            "kid": self.kid, "email": self.email, "plan": self.plan, "features": list(self.features),
            "issued_at": iso_utc(self.issued_at), "expires_at": iso_utc(self.expires_at),
            "grace_ends_at": iso_utc(self.grace_ends_at), "node": self.node,
        }


def iso_utc(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def skid_of(public_x: str) -> str:
    return hashlib.sha256(_unb64(public_x)).hexdigest()[:16]


def _verify(public_x: str, signature: bytes, message: bytes) -> bool:
    try:
        from cryptography.exceptions import InvalidSignature
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
    except ImportError as exc:  # pragma: no cover - cryptography is a core dependency
        raise errors.NotConfigured("The 'cryptography' package is required to read a dbabrain key.") from exc
    try:
        Ed25519PublicKey.from_public_bytes(_unb64(public_x)).verify(signature, message)
    except (InvalidSignature, ValueError):
        return False
    return True


def parse_key(token: str, *, public_keys: Mapping[str, str] | None = None) -> DbabrainKey:
    """The key in ``token`` when dbabrain.dev signed it; else :class:`KeyInvalid` saying why.

    The term is not judged here - :func:`standing` does that, with a clock.
    """
    keys = SIGNING_PUBLIC_KEYS if public_keys is None else public_keys
    parts = str(token or "").strip().split(".")
    if len(parts) != 3 or parts[0] != PREFIX:
        raise KeyInvalid("not a dbabrain key")
    try:
        payload = json.loads(_unb64(parts[1]))
        signature = _unb64(parts[2])
    except (ValueError, TypeError) as exc:
        raise KeyInvalid("not a dbabrain key") from exc
    if not isinstance(payload, dict) or payload.get("v") != 1:
        raise KeyInvalid("not a dbabrain key this release reads")
    skid = str(payload.get("skid") or "")
    public_x = keys.get(skid)
    if not public_x or skid_of(public_x) != skid:
        raise KeyInvalid("signed by a key this release does not know - a newer dbabrain may read it")
    if not _verify(public_x, signature, f"{parts[0]}.{parts[1]}".encode("ascii")):
        raise KeyInvalid("the signature does not match - the key was changed")
    try:
        return DbabrainKey(
            kid=str(payload["kid"]), email=str(payload["sub"]), plan=str(payload["plan"]),
            features=tuple(str(f) for f in payload.get("features") or ()),
            issued_at=datetime.fromtimestamp(int(payload["iat"]), timezone.utc),
            expires_at=datetime.fromtimestamp(int(payload["exp"]), timezone.utc),
            node=str(payload.get("node") or ""), skid=skid,
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise KeyInvalid("a signed key without the fields of one") from exc


#: An online answer that ends a key now, whatever its term says (``GET /key/status``).
REVOKING_STATES = frozenset({"revoked", "replaced"})


@dataclass(frozen=True)
class KeyStanding:
    """Where a node's key stands at one moment."""

    #: none, invalid, active, grace, ended, revoked
    state: str
    key: DbabrainKey | None
    reason: str
    #: Days to the end of the term (negative in the grace) - None without a key.
    days_left: float | None = None

    @property
    def warning_due(self) -> int | None:
        """The warning step reached (14, 7, 3, 1) while active - each is said once; None before the first.

        A step as long as the key's whole term or longer is not one: it would be reached the moment the
        key is issued (a 14-day key's first warning is at 7).
        """
        if self.state != "active" or self.days_left is None or self.key is None:
            return None
        term = (self.key.expires_at - self.key.issued_at).total_seconds() / 86400
        reached = [days for days in WARN_DAYS if self.days_left <= days < term]
        return min(reached) if reached else None

    def describe(self) -> dict[str, Any]:
        out: dict[str, Any] = {"state": self.state, "reason": self.reason}
        if self.days_left is not None:
            out["days_left"] = round(self.days_left, 2)
        if self.key is not None:
            out["key"] = self.key.describe()
        return out


def standing(
    token: str | None,
    *,
    now: datetime,
    online: Mapping[str, Any] | None = None,
    public_keys: Mapping[str, str] | None = None,
) -> KeyStanding:
    """The key's standing: its signature and term offline, then the last online answer if one came.

    ``online`` is the last ``GET /key/status`` answer *for this key* (its ``kid``); a failed check is
    passed as None and changes nothing.
    """
    if not str(token or "").strip():
        return KeyStanding("none", None, "no dbabrain key on this node")
    try:
        key = parse_key(str(token), public_keys=public_keys)
    except KeyInvalid as exc:
        return KeyStanding("invalid", None, str(exc))
    days_left = (key.expires_at - now).total_seconds() / 86400
    if online and str(online.get("kid") or "") == key.kid and str(online.get("state") or "") in REVOKING_STATES:
        said = str(online.get("state"))
        reason = "dbabrain.dev revoked this key" if said == "revoked" else "a newer key of this account replaced it"
        return KeyStanding("revoked", key, reason, days_left)
    if now < key.expires_at:
        return KeyStanding("active", key, f"valid until {iso_utc(key.expires_at)}", days_left)
    if now < key.grace_ends_at:
        return KeyStanding("grace", key, f"ended {iso_utc(key.expires_at)}; grace until {iso_utc(key.grace_ends_at)}", days_left)
    return KeyStanding("ended", key, f"ended {iso_utc(key.expires_at)}; the grace ended {iso_utc(key.grace_ends_at)}", days_left)


@dataclass(frozen=True)
class Decision:
    allowed: bool
    #: ok, warning, critical - how loudly :attr:`sentence` should be said
    level: str
    sentence: str
    #: The standing it was decided from (``KeyStanding.state``) - empty for a feature not gated.
    state: str = ""


def may_run(standing_: KeyStanding, feature: str, *, new_work: bool = True) -> Decision:
    """May ``feature`` run now - :func:`_may_run`, with the standing it was decided from."""
    from dataclasses import replace

    decision = _may_run(standing_, feature, new_work=new_work)
    return replace(decision, state=standing_.state) if feature in FEATURES else decision


def _may_run(standing_: KeyStanding, feature: str, *, new_work: bool = True) -> Decision:
    """May ``feature`` run now - the one place that answers it (K4's gate asks only this).

    ``new_work=False`` is work already asked for that must not be stranded by a key's end - a restore
    the operator requested; it runs after the grace and after a revocation too, said CRITICAL.
    A feature not in :data:`FEATURES` is not gated and always runs.
    """
    if feature not in FEATURES:
        return Decision(True, "ok", "")
    what = FEATURES[feature]
    state = standing_.state
    if state in ("none", "invalid"):
        why = standing_.reason if state == "invalid" else "no dbabrain key on this node"
        if not new_work:
            return Decision(True, "critical", f"{what} ran without a valid dbabrain key ({why}) - {HOW_TO_GET}")
        return Decision(False, "warning", f"{what} need a dbabrain key ({why}) - {HOW_TO_GET}")
    key = standing_.key
    assert key is not None
    if feature not in key.features:
        return Decision(False, "warning", f"{what} are not in this key's plan ({key.plan})")
    renew = "run `dbabrain key-renew`"
    if state in ("ended", "revoked"):
        if not new_work:
            return Decision(True, "critical", f"the dbabrain key {standing_.reason} - this asked-for run goes on; {renew}")
        return Decision(False, "critical", f"{what} stopped: the dbabrain key {standing_.reason} - {renew}")
    if state == "grace":
        return Decision(True, "critical", f"the dbabrain key {standing_.reason} - {what} stop after it; {renew}")
    due = standing_.warning_due
    if due is not None:
        level = "critical" if due == 1 else "warning"
        days = max(0, int(standing_.days_left or 0))
        return Decision(True, level, f"the dbabrain key ends in {days} day(s) ({iso_utc(key.expires_at)}) - {renew}")
    return Decision(True, "ok", "")


# --------------------------------------------------------------------------- #
# A node's key: where its pieces are kept, and which scheduled command needs it (K4)
# --------------------------------------------------------------------------- #
#: The last online answer about the node's key, beside data/ (data/ is what a deploy ships).
STATUS_FILENAME = "dbabrain_key_status.json"

#: A scheduled command's module -> the feature it needs. The assistant's queue is not here: it
#: answers a question without a key by saying so, one question at a time (plan A2); the console
#: (webhost) is free (D2) - the report pages it serves are built by `reports`, which is gated.
COMMAND_FEATURES: dict[str, str] = {
    "dbabrain.reports.cli": "reports",
    "dbabrain.backup_restore.cli": "backup_restore",
    "dbabrain.sre.cli": "sre",
    "dbabrain.mcp.cli": "mcp",
}


def status_file(data_dir: str | Path) -> Path:
    """``<tool root>/runtime/dbabrain_key_status.json`` - the tool root being data/'s parent."""
    return Path(data_dir).resolve().parent / "runtime" / STATUS_FILENAME


def read_status(data_dir: str | Path) -> dict[str, Any] | None:
    """The last online answer kept, or None - a missing or unreadable file is no answer, not a fault."""
    try:
        raw = json.loads(status_file(data_dir).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return raw if isinstance(raw, dict) else None


def feature_of_command(command_text: str) -> str | None:
    """The gated feature a scheduled command needs, read from the module it runs; None when free."""
    words = str(command_text or "").split()
    for index, word in enumerate(words[:-1]):
        if word == "-m" and words[index + 1] in COMMAND_FEATURES:
            return COMMAND_FEATURES[words[index + 1]]
    return None


def node_standing(data_dir: str | Path, *, key: str | None, now: datetime,
                  public_keys: Mapping[str, str] | None = None) -> KeyStanding:
    """This node's standing: the key from its encrypted store, the last online answer beside it.

    A store that does not open is said as the reason (``invalid``) - never raised: the gate's
    caller is a scheduler, and one unreadable store must not stop the metrics that need no key.
    """
    from dbabrain.lib import secret_text

    try:
        token = secret_text.load_secret_text(data_dir, key=key).get(KEY_REF)
    except Exception as exc:  # noqa: BLE001 - the reason is the answer
        return KeyStanding("invalid", None, f"the secret store did not open ({type(exc).__name__})")
    return standing(token, now=now, online=read_status(data_dir), public_keys=public_keys)


#: The exit code of a hand run refused because its key ENDED (or was revoked, or is not one) - not
#: 1, which is a failure of the work. A node that never had a key exits 0: not configured is a
#: state, not a failure (the house rule), and a metrics-only install is a correct one.
EXIT_NEEDS_KEY = 3


def gate_here(feature: str, *, new_work: bool = True, data_dir: str | Path | None = None,
              now: datetime | None = None) -> Decision:
    """:func:`may_run` for this process's node - the data folder it resolves, the passphrase it holds.

    What a gated app's CLI asks first, so a run by hand (or by the bot) obeys the key the way the
    scheduler does; the app prints the sentence, this decides.
    """
    import os

    from dbabrain.lib.paths import resolve_data_dir
    from dbabrain.lib.secret_text import SECRET_KEY_ENV_VAR

    here = node_standing(data_dir if data_dir is not None else resolve_data_dir(),
                         key=os.environ.get(SECRET_KEY_ENV_VAR) or None,
                         now=now or datetime.now(timezone.utc))
    return may_run(here, feature, new_work=new_work)


def said_at_the_door(decision: Decision, *, app: str, stream: Any = None) -> int | None:
    """What a gated CLI does with :func:`gate_here`'s answer: refused -> the sentence and
    :data:`EXIT_NEEDS_KEY`; allowed with a warning -> the sentence, and None (go on)."""
    import sys

    out = stream if stream is not None else sys.stderr
    if not decision.allowed:
        print(f"{app}: {decision.sentence}", file=out)
        return 0 if decision.state == "none" else EXIT_NEEDS_KEY
    if decision.level != "ok" and decision.sentence:
        print(f"{app}: {decision.level.upper()}: {decision.sentence}", file=out)
    return None
