"""Who can take a database's files - what the two detection metrics read, as one block (0.38.0).

The operator, 2026-11-11: *the backups are protected now, but anyone who gets onto the VM can copy
the database's own data files and open them on their machine.* Encrypting at rest protects copied
files and a stolen disk; it never protects against an administrator on the running machine. So the
question has two halves, and dbabrain's part is to **see and report** both, read-only:

* **Are the data files encrypted?** - ``SECURITY_DATA_ENCRYPTION``, read where each engine says it.
* **Who can be an administrator on the host?** - ``SECURITY_HOST_ADMINS``, read with the login
  dbabrain already has.

and one comparison nothing else on the pages made: the **edition against the environment** (1.325)
- a production instance on Developer, Express or Evaluation edition.

One builder for both pages: the inventory's overlay calls it per server, the fleet page and the
server page render the block it returns, so they cannot describe one server two ways. It shows what
was read and says what was not: a section the collector could not read is listed with its reason,
a metric that failed says so, and a certificate is called *kept in the vault* only when the vault
was read and holds that thumbprint.

Nothing here raises an alert. The rows are OK by design; what alerts is a **change** between two
reads, written by the collector as its own rows (``lib.change_alert``) - they are listed here as
``changes`` while they are the newest read.
"""

from __future__ import annotations

import re
from typing import Any

from dbabrain.lib import change_alert

ENCRYPTION_CODE = "SECURITY_DATA_ENCRYPTION"
HOST_ADMINS_CODE = "SECURITY_HOST_ADMINS"
CODES = (ENCRYPTION_CODE, HOST_ADMINS_CODE)

#: How each engine encrypts its data files - what a finding points to. Guidance, not a reading:
#: 0.38.0 detects and reports, and turning it on is a decision per server (D-38.4).
HOW_TO_ENCRYPT = {
    "sqlserver": ("SQL Server TDE: a certificate in master, CREATE DATABASE ENCRYPTION KEY in the "
                  "database, ALTER DATABASE ... SET ENCRYPTION ON - with the certificate and its "
                  "private key kept away from the host (dbabrain's certificate vault)"),
    "oracle": ("Oracle TDE: a keystore (ADMINISTER KEY MANAGEMENT), then ALTER TABLESPACE ... "
               "ENCRYPTION ONLINE ENCRYPT - with a password keystore, or the auto-login wallet "
               "kept off the database host"),
    "postgresql": ("community PostgreSQL has no data-file encryption: encrypt the volume under "
                   "the data directory, or run a distribution that provides it (pg_tde)"),
}

_KV = re.compile(r"([A-Za-z_][A-Za-z0-9_]*)=([^,|]*)")
_ENCRYPTED_STATES = {"encrypted"}
_NOT_ENCRYPTED_STATES = {"not encrypted"}


def _fields(message: Any) -> dict[str, str]:
    return {key: value.strip() for key, value in _KV.findall(str(message or ""))}


def _rows(code_map: dict, code: str) -> list[tuple[Any, dict]]:
    return [(item, row) for (metric_code, item), row in code_map.items() if metric_code == code]


def _as_of(rows: list[tuple[Any, dict]]) -> str:
    return max((str(row.get("collected_at") or "") for _item, row in rows), default="")


def _failure(rows: list[tuple[Any, dict]]) -> str:
    """The collector's own words when the read failed: its one row has no item and is not OK."""
    for item, row in rows:
        if item is None and str(row.get("status") or "").upper() not in ("OK", "LOGGING"):
            return " ".join(str(row.get("message") or "the collector returned no result").split())
    return ""


def _changes(rows: list[tuple[Any, dict]], code: str) -> list[dict]:
    return [{"metric": code,
             "item": str(item or "")[len(change_alert.CHANGE_ITEM_PREFIX):],
             "status": str(row.get("status") or "").upper(),
             "message": str(row.get("message") or ""),
             "at": str(row.get("collected_at") or "")}
            for item, row in rows if change_alert.is_change_row(row)]


def build_encryption(code_map: dict, *, vault: set[str] | None = None) -> dict:
    """Each database's (Oracle: each tablespace's) data-file encryption, and Oracle's keystores.

    ``vault`` is the set of certificate thumbprints dbabrain's vault holds for this server (lower
    case), or ``None`` when the vault was not read - then no row claims anything about it.
    """
    rows = _rows(code_map, ENCRYPTION_CODE)
    if not rows:
        return {"collected": False}
    items: list[dict] = []
    keystores: list[dict] = []
    engine = ""
    for item, row in rows:
        unit = str(row.get("metric_unit") or "").lower()
        if item is None or unit in change_alert.NOT_COMPARED_UNITS:
            continue
        fields = _fields(row.get("message"))
        engine = engine or fields.get("engine", "")
        value = str(row.get("metric_value") or "").strip()
        if unit == "keystore":
            keystores.append({
                "name": str(item),
                "status": value,
                "walletType": fields.get("wallet_type", ""),
                "location": fields.get("location", "").replace(";", ","),
                "opensWithoutPassword": fields.get("opens_without_password", ""),
            })
            continue
        name = str(item)
        scope = "tablespace" if name.startswith("tablespace:") else "database"
        thumbprint = fields.get("thumbprint", "")
        thumbprint = "" if thumbprint in ("", "-") else thumbprint
        entry = {
            "name": name.partition(":")[2] if scope == "tablespace" else name,
            "scope": scope,
            "state": value,
            "encrypted": value.lower() in _ENCRYPTED_STATES,
            "notEncrypted": value.lower() in _NOT_ENCRYPTED_STATES,
            "algorithm": "" if fields.get("algorithm", "-") == "-" else fields.get("algorithm", ""),
            "protector": "" if fields.get("encryptor", "-") == "-" else fields.get("encryptor", ""),
            "protectorType": fields.get("encryptor_type", ""),
            "thumbprint": thumbprint,
            "privateKeyBackedUp": fields.get("private_key_backed_up", ""),
            "provider": fields.get("provider", ""),
            "tablesEncrypted": fields.get("tables_encrypted", ""),
            "tablesTotal": fields.get("tables_total", ""),
            # Only a thumbprint the engine reported can be held to the vault, and only a vault
            # that was read can answer: "" is "not checked", never "not kept".
            "vault": ("" if not thumbprint or vault is None
                      else ("kept" if thumbprint.lower() in vault else "not kept")),
        }
        items.append(entry)
    items.sort(key=lambda entry: (entry["encrypted"], entry["name"].casefold()))
    return {
        "collected": True,
        "asOf": _as_of(rows),
        "error": _failure(rows),
        "engine": engine,
        "items": items,
        "keystores": keystores,
        "encrypted": sum(1 for entry in items if entry["encrypted"]),
        "total": len(items),
        "vaultRead": vault is not None,
        "howToEncrypt": HOW_TO_ENCRYPT.get(engine, ""),
    }


#: sshd settings whose ``yes`` the page marks: root reachable over SSH, and logins by password.
#: Remote Desktop being on is what a Windows server is - shown, not marked.
_MARKED_SETTINGS = {"sshd:permitrootlogin": {"yes"}, "sshd:passwordauthentication": {"yes"}}


def build_admins(code_map: dict) -> dict:
    """Who can be an administrator on the host: accounts by group, settings, root's keys."""
    rows = _rows(code_map, HOST_ADMINS_CODE)
    if not rows:
        return {"collected": False}
    accounts: list[dict] = []
    settings: list[dict] = []
    keys: list[dict] = []
    unreadable: list[dict] = []
    read_as, root_access = "", ""
    for item, row in rows:
        unit = str(row.get("metric_unit") or "").lower()
        if item is None or unit == change_alert.CHANGE_UNIT:
            continue
        name = str(item)
        value = str(row.get("metric_value") or "").strip()
        message = str(row.get("message") or "")
        fields = _fields(message)
        if unit == change_alert.UNREADABLE_UNIT:
            unreadable.append({"section": name, "why": message})
        elif unit == change_alert.SUMMARY_UNIT:
            read_as, root_access = value, fields.get("root_access", "")
        elif name.startswith("group:"):
            group, _, account = name[len("group:"):].rpartition(":")
            accounts.append({"group": group, "account": account, "kind": value,
                             "source": fields.get("source", ""), "sid": fields.get("sid", "")})
        elif name.startswith("uid0:"):
            accounts.append({"group": "uid 0", "account": name[len("uid0:"):], "kind": value,
                             "source": "", "sid": ""})
        elif name.startswith("root_key:"):
            keys.append({"fingerprint": name[len("root_key:"):], "type": value,
                         "comment": "" if fields.get("comment", "-") == "-" else fields.get("comment", ""),
                         "file": fields.get("file", "")})
        else:
            settings.append({"name": name, "value": value, "detail": message,
                             "marked": value.lower() in _MARKED_SETTINGS.get(name.lower(), set())})
    accounts.sort(key=lambda entry: (entry["group"].casefold(), entry["account"].casefold()))
    settings.sort(key=lambda entry: entry["name"].casefold())
    keys.sort(key=lambda entry: entry["fingerprint"])
    return {
        "collected": True,
        "asOf": _as_of(rows),
        "error": _failure(rows),
        "readAs": read_as,
        "rootAccess": root_access,
        "accounts": accounts,
        "settings": settings,
        "keys": keys,
        "unreadable": unreadable,
        "distinctAccounts": len({entry["account"].casefold() for entry in accounts}),
    }


def edition_finding(*, edition: str, environment: str, rule: dict | None) -> str:
    """The sentence for an instance whose edition does not belong in its environment, else ``""``.

    ``rule`` is ``SECURITY_DATA_ENCRYPTION``'s ``report_policy.edition_rule`` - which environments
    are production and which editions are not meant for it are the estate's to state, not this
    module's. An edition is matched as a whole word of what the engine reports.
    """
    rule = rule if isinstance(rule, dict) else {}
    environments = {str(name).strip().lower() for name in rule.get("production_environments") or []}
    if not edition or str(environment or "").strip().lower() not in environments:
        return ""
    for word in rule.get("non_production_editions") or []:
        word = str(word).strip()
        if word and re.search(rf"\b{re.escape(word)}\b", edition, re.IGNORECASE):
            return (f"{edition} on a {environment} instance: {word} is an edition not meant for "
                    "production")
    return ""


def build_protection(code_map: dict, *, environment: str = "", edition: str = "",
                     edition_rule: dict | None = None, vault: set[str] | None = None) -> dict:
    """The block both pages render for one server; ``{}`` when there is nothing to say."""
    encryption = build_encryption(code_map, vault=vault)
    admins = build_admins(code_map)
    findings: list[str] = []
    wrong_edition = edition_finding(edition=edition, environment=environment, rule=edition_rule)
    if wrong_edition:
        findings.append(wrong_edition)
    if encryption.get("collected") and not encryption.get("error"):
        plain = [entry["name"] for entry in encryption["items"] if entry["notEncrypted"]]
        if plain:
            scope = encryption["items"][0]["scope"]
            listed = ", ".join(plain[:6]) + (", ..." if len(plain) > 6 else "")
            findings.append(
                f"data files not encrypted at rest: {len(plain)} of {encryption['total']} "
                f"{scope}{'s' if encryption['total'] != 1 else ''} ({listed}) - a copy of the files "
                "opens on another install"
                + (f". {encryption['howToEncrypt']}" if encryption.get("howToEncrypt") else ""))
        for entry in encryption["items"]:
            if entry["vault"] == "not kept":
                findings.append(
                    f"{entry['name']} is encrypted by {entry['protector'] or 'a certificate'} "
                    f"(thumbprint {entry['thumbprint']}), which dbabrain's certificate vault does not "
                    "hold: without that certificate and its private key the database restores nowhere")
        for store in encryption["keystores"]:
            if store["opensWithoutPassword"] == "yes":
                findings.append(
                    f"Oracle {store['name']} is an auto-login keystore ({store['walletType']}) at "
                    f"{store['location'] or 'its default location'}: it opens with no password on the "
                    "database host, so the files and what opens them can be copied together")
    changes = (_changes(_rows(code_map, ENCRYPTION_CODE), ENCRYPTION_CODE)
               + _changes(_rows(code_map, HOST_ADMINS_CODE), HOST_ADMINS_CODE))
    if not (encryption.get("collected") or admins.get("collected") or findings):
        return {}
    return {
        "environment": str(environment or ""),
        "edition": str(edition or ""),
        "editionFinding": wrong_edition,
        "encryption": encryption,
        "admins": admins,
        "changes": changes,
        "findings": findings,
    }
