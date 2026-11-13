"""Each proven vault row also as a sealed file on this node's disk (0.37.0).

The operator, 2026-11-10: *keep the certificate as a physical file as well as in the vault* - on the
worker's disk and a copy on the master; and *if the secret store's passphrase is lost?* The format
and its rules are :mod:`dbabrain.lib.vault_file`; this module writes and proves the files.

* ``backup_restore certificate-files [--apply]`` - every verified vault row: a file already there is
  opened and proven (never rewritten); a missing one is written with ``--apply``, read back and
  proven. The store is shared, so the same command on the worker and on the master leaves a copy on
  each. The folder is ``store_config.json`` ``certificate_vault.file_folder`` - never ``data/``,
  which a deploy overwrites on the worker.
* ``take-certificate --apply`` writes the file of the row it took; ``--delete-at-source`` refuses
  until that file is written and proven on this node - the server's copy is deleted only when two
  independent copies stand.
* ``backup_restore vault-key --recovery --out FILE`` makes a recovery key: the private half goes to
  FILE for the operator to keep OFF every machine dbabrain runs on, the public half is printed for
  ``recovery_recipients``. ``vault-key --dbabrain`` makes dbabrain's own identity and stores it in
  the secret store under ``identity_ref`` - once; an identity already there is never replaced, the
  files sealed for it need it.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from dbabrain.db.certificate_vault import CertificateVault
from dbabrain.lib import errors
from dbabrain.lib import vault_file as V


class VaultFileRefused(errors.Refused):
    """Not configured, no identity, or a file that does not prove."""


def settings(app_config: Any) -> V.VaultFileSettings | None:
    """The ``certificate_vault`` block of the store's own config file, or ``None`` - not configured."""
    path = getattr(getattr(app_config, "store", None), "config_file", None)
    if not path or not Path(path).is_file():
        return None
    raw = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    return V.parse_settings(raw.get(V.BLOCK))


def _required(app_config: Any) -> V.VaultFileSettings:
    found = settings(app_config)
    if found is None:
        raise VaultFileRefused(
            f"no {V.BLOCK} block in store_config.json - the vault's files are not configured "
            "(file_folder, identity_ref, recovery_recipients; see `backup_restore vault-key`).")
    return found


def folder(app_config: Any, found: V.VaultFileSettings) -> Path:
    from dbabrain.lib.config import TOOL_ROOT

    path = Path(found.file_folder)
    return path if path.is_absolute() else (TOOL_ROOT / path)


def _identity(found: V.VaultFileSettings, secrets: dict[str, str]) -> str:
    value = secrets.get(found.identity_ref) or ""
    if not value:
        raise VaultFileRefused(f"the secret store has no {found.identity_ref} - dbabrain's own vault identity "
                               "(`backup_restore vault-key --dbabrain` makes it, once).")
    return V.check_identity(value)


def seal_row(app_config: Any, row: dict[str, Any], *, passphrase: str, secrets: dict[str, str], apply: bool,
             vault: CertificateVault | None = None) -> dict[str, Any]:
    """One row's file: ``proven`` (there, and it proves), ``written`` (now), or ``missing`` (dry run).
    A file there that does not prove raises - it is never overwritten."""
    found = _required(app_config)
    identity = _identity(found, secrets)
    path = folder(app_config, found) / V.relative_path(row["server_id"], row["certificate_name"], row["thumbprint"])
    if path.exists():
        issues = V.problems(V.unseal(path.read_bytes(), identity), row)
        if issues:
            raise VaultFileRefused(f"{path} does not prove ({'; '.join(issues)}) - never overwritten; "
                                   "look at it before going on.")
        return {"certificate_id": row["certificate_id"], "path": str(path), "status": "proven"}
    if not apply:
        return {"certificate_id": row["certificate_id"], "path": str(path), "status": "missing"}
    if not row.get("verified_at"):
        raise VaultFileRefused(f"vault row {row['certificate_id']} is not verified - its file is not written.")
    vault = vault or CertificateVault.from_config(app_config)
    cer, pvk, row = vault.pair(int(row["certificate_id"]), passphrase=passphrase)
    password = secrets.get(str(row["password_ref"])) or ""
    if not password:
        raise VaultFileRefused(f"the secret store has no {row['password_ref']} (the .pvk password of row "
                               f"{row['certificate_id']}) - a file without it opens nothing.")
    sealed = V.seal(V.payload(row, cer=cer, pvk=pvk, pvk_password=password),
                    [V.recipient_of(identity), *found.recovery_recipients])
    path.parent.mkdir(parents=True, exist_ok=True)
    partial = path.with_name(path.name + ".partial")
    partial.write_bytes(sealed)
    if path.exists():  # written by another run since the check above - that one stands
        partial.unlink()
        raise VaultFileRefused(f"{path} appeared while this one was written - run again to prove it.")
    os.replace(partial, path)
    issues = V.problems(V.unseal(path.read_bytes(), identity), row)
    if issues:
        raise VaultFileRefused(f"{path} was written and does not prove: {'; '.join(issues)}.")
    return {"certificate_id": row["certificate_id"], "path": str(path), "status": "written",
            "recipients": 1 + len(found.recovery_recipients)}


def seal_all(app_config: Any, *, passphrase: str, secrets: dict[str, str], apply: bool,
             vault: CertificateVault | None = None) -> dict[str, Any]:
    """Every verified row's file on this node - each one's answer, and the counts."""
    _identity(_required(app_config), secrets)  # not configured is said once, not once per row
    vault = vault or CertificateVault.from_config(app_config)
    lines = []
    for row in vault.listing():
        if not row.get("verified_at"):
            lines.append({"certificate_id": row["certificate_id"], "status": "skipped",
                          "reason": "not verified - take-certificate --apply proves it first"})
            continue
        try:
            lines.append(seal_row(app_config, row, passphrase=passphrase, secrets=secrets, apply=apply, vault=vault))
        except (VaultFileRefused, V.VaultFileError) as exc:
            lines.append({"certificate_id": row["certificate_id"], "status": "failed", "reason": str(exc)})
    counts: dict[str, int] = {}
    for line in lines:
        counts[line["status"]] = counts.get(line["status"], 0) + 1
    return {"files": lines, "counts": counts, "ok": not counts.get("failed")}


def require_proven_file(app_config: Any, row: dict[str, Any], *, secrets: dict[str, str]) -> str:
    """The row's file on this node, proven - or a refusal naming why (the gate before a delete)."""
    found = _required(app_config)
    path = folder(app_config, found) / V.relative_path(row["server_id"], row["certificate_name"], row["thumbprint"])
    if not path.exists():
        raise VaultFileRefused(f"{path} is not written - the server's copy is deleted only when the vault "
                               "row AND its file stand (`backup_restore certificate-files --apply`).")
    issues = V.problems(V.unseal(path.read_bytes(), _identity(found, secrets)), row)
    if issues:
        raise VaultFileRefused(f"{path} does not prove: {'; '.join(issues)}.")
    return str(path)


# --------------------------------------------------------------------------- #
# Keys
# --------------------------------------------------------------------------- #
def new_recovery_key(out: str | Path, *, created_at: str) -> dict[str, Any]:
    """A recovery key: the private half to ``out`` (never overwritten), the public half answered."""
    target = Path(out)
    if target.exists():
        raise VaultFileRefused(f"{target} exists - a recovery key is never overwritten.")
    identity, recipient = V.new_identity()
    target.parent.mkdir(parents=True, exist_ok=True)
    with open(target, "x", encoding="ascii", newline="\n") as handle:
        handle.write(V.identity_file_text(identity, recipient, created_at=created_at,
                                          purpose="dbabrain certificate vault - RECOVERY key. Keep it off every "
                                                  "machine dbabrain runs on (paper / USB in a safe)."))
    return {"recovery_key_file": str(target), "recipient": recipient,
            "next": f"add {recipient} to store_config.json {V.BLOCK}.recovery_recipients, then move "
                    f"{target} off this machine"}


def new_dbabrain_identity(app_config: Any, *, secrets: dict[str, str]) -> dict[str, Any]:
    """dbabrain's own identity, into the secret store - once."""
    from dbabrain.transport import common_cli

    found = _required(app_config)
    if secrets.get(found.identity_ref):
        raise VaultFileRefused(f"{found.identity_ref} is already in the secret store - never replaced: every file "
                               f"sealed for it needs it. Its public key is {V.recipient_of(secrets[found.identity_ref])}.")
    identity, recipient = V.new_identity()
    answer = common_cli.run("secret-set", {"ref": found.identity_ref, "value": identity, "also_plaintext": True})
    return {"identity_ref": found.identity_ref, "recipient": recipient,
            "plaintext_source": answer.get("plaintext_source")}
