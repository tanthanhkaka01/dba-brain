"""Does any file that ships contain a value from the secret store?

The scanners already in the gate answer two different questions, and neither answers this one:

- `gitleaks` matches credential *shapes*. A password that looks like a password is reported, and a
  string that has been allowlisted as a test placeholder is not looked at again.
- :mod:`db_ops.common.identifier_scan` matches configured *identifiers* — addresses, hostnames,
  accounts. A password is not an identifier, so it never appears in its term list.

Between the two there is a gap the width of one string: a real password written into a test as an
example. It is shaped like a credential, so it gets allowlisted as a placeholder; it is not an
identifier, so the identifier scan ignores it. `MSSQL_..._SA` sat in a shipped test from v0.2.0
through v0.4.1 — eight tags and every sdist — because nobody compared the literal against the
store.

So this compares them directly: decrypt the store, and look for its values in the files that ship.
No pattern, no judgement about what a password looks like. A value is a secret because the store
says it is.

**It never prints a secret.** A finding names the ref, the file and the line. A checker that echoes
the value it found has moved the leak into the log.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Iterable

from db_ops.common.identifier_scan import DEFAULT_EXTENSIONS, DEFAULT_PATHS, SKIP_DIRS
from db_ops.lib.paths import TOOL_ROOT

#: Below this, a "secret" is too short to be found reliably and too common to be meaningful — a
#: four-character value collides with ordinary words and would report every file in the tree.
MIN_SECRET_LENGTH = 8

#: Values that are real entries in the store and still not worth searching for, because they are
#: what somebody types when a field is required and unused. Finding these proves nothing.
PLACEHOLDER_VALUES: frozenset[str] = frozenset({
    "changeme", "change_me", "password", "Password1", "placeholder", "not-set", "unset", "none",
    "template", "example", "your-token-here", "xxxxxxxx",
})


class SecretLiteralError(RuntimeError):
    """The store could not be read, so nothing can be said about the tree."""


def _files(paths: Iterable[str], root: Path) -> list[Path]:
    wanted = {ext.lower() for ext in DEFAULT_EXTENSIONS}
    found: list[Path] = []
    for name in paths:
        target = (root / name).resolve()
        if not str(target).startswith(str(root.resolve())):
            raise SecretLiteralError(f"{name} is outside {root}")
        if target.is_file():
            found.append(target)
            continue
        if not target.is_dir():
            continue
        for child in sorted(target.rglob("*")):
            # Relative to what is being scanned, not the absolute path: the root itself may sit
            # under a directory named in SKIP_DIRS - a temporary tree under `.pytest_tmp` is the
            # normal case - and matching on absolute parts silently skips every file in it, which
            # is a checker that reports clean because it looked at nothing.
            if any(part in SKIP_DIRS for part in child.relative_to(target).parts):
                continue
            if child.is_file() and child.suffix.lower() in wanted:
                found.append(child)
    return found


def _searchable(secrets: dict[str, str]) -> dict[str, str]:
    """``value -> ref``, dropping what cannot be searched for usefully."""
    out: dict[str, str] = {}
    for ref, value in secrets.items():
        if not isinstance(value, str):
            continue
        candidate = value.strip()
        if len(candidate) < MIN_SECRET_LENGTH:
            continue
        if candidate.lower() in PLACEHOLDER_VALUES:
            continue
        out.setdefault(candidate, ref)
    return out


def scan(request: dict[str, Any] | None = None, *, data_dir: str | Path | None = None,
         key: str | None = None) -> dict[str, Any]:
    """Report every shipped file holding a value the secret store contains.

    ``request`` takes ``paths`` (default: the shipping surface) and ``store`` (default: the
    encrypted store under ``data_dir``). The key comes from ``key`` or ``DB_OPS_SECRET_KEY``.
    """
    from db_ops.lib.secret_text import load_secret_text_file, resolve_key

    request = dict(request or {})
    root = Path(request.get("root") or TOOL_ROOT)
    paths = tuple(request.get("paths") or DEFAULT_PATHS)

    resolved_key = key or os.environ.get("DB_OPS_SECRET_KEY") or None
    try:
        resolved_key = resolve_key(resolved_key)
    except Exception as exc:  # noqa: BLE001 - a missing key is a usage error, not a crash.
        raise SecretLiteralError(
            "no passphrase: pass --key/--key-base64 or set DB_OPS_SECRET_KEY. Without it the "
            "store cannot be read, and a scan with nothing to search for would report every tree "
            "clean - which is the one answer this check must never give."
        ) from exc

    store = Path(request.get("store") or (Path(data_dir or (root / "data"))
                                          / "encrypted_secret_text.json"))
    try:
        secrets = load_secret_text_file(store, key=resolved_key)
    except Exception as exc:  # noqa: BLE001
        raise SecretLiteralError(f"cannot read the secret store at {store}: {exc}") from exc

    # An *empty* store means the read went wrong, and reporting a tree clean on the strength of
    # having looked for nothing is the one answer this check must never give. A store that decrypts
    # to real entries which are all too short or all placeholders is a different, legitimate state:
    # there is genuinely nothing to search for, and 0 hits is the honest answer.
    if not secrets:
        raise SecretLiteralError(
            f"{store} decrypted to no entries at all, so there is nothing to look for."
        )
    searchable = _searchable(secrets)

    findings: list[dict[str, Any]] = []
    scanned = 0
    for path in _files(paths, root):
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        scanned += 1
        if not text:
            continue
        for value, ref in searchable.items():
            if value not in text:
                continue
            line = next((n for n, content in enumerate(text.splitlines(), start=1)
                         if value in content), 0)
            findings.append({
                "file": str(path.relative_to(root)).replace("\\", "/"),
                "secret_ref": ref,
                "line": line,
            })

    return {
        "root": str(root),
        "paths": list(paths),
        "store": str(store),
        "secrets_searched": len(searchable),
        "files_scanned": scanned,
        "hits": len(findings),
        "files_with_findings": len({item["file"] for item in findings}),
        "findings": findings,
    }


def format_report(outcome: dict[str, Any]) -> str:
    """A report that names the ref and never the value."""
    lines = [
        f"secret literals: {outcome['hits']} hit(s) in {outcome['files_with_findings']} file(s), "
        f"searching {outcome['secrets_searched']} stored value(s) across "
        f"{outcome['files_scanned']} file(s)"
    ]
    for item in outcome["findings"]:
        lines.append(f"  {item['file']}:{item['line']}  holds the value of {item['secret_ref']}")
    if outcome["hits"]:
        lines.append("")
        lines.append("A stored value in a file that ships is published the moment the tree is. "
                     "Rotate it, then remove the literal - in that order.")
    return "\n".join(lines)
