"""Rotate a database login's password, on the server and in the secret store, as one operation.

A rotation is only "done" when the server and the store agree. Doing it by hand drifts the two
apart in both directions: an ``ALTER LOGIN`` nobody records leaves db_ops authenticating with a dead
password, and a store edit nobody applies leaves a password the server never accepted. So this module
owns the whole sequence and refuses to record anything it has not proven.

The order per target is fixed and matters:

1. connect with the **current** password — a target whose current password already fails is skipped,
   never guessed at, because ``ALTER LOGIN ... OLD_PASSWORD`` needs it and because a failure here
   means the store was already wrong;
2. issue the engine's change statement;
3. re-authenticate on a **brand-new connection** — the session that issued the change stays valid
   afterwards, so checking on it proves nothing;
4. only then hand the new value back for storage.

If step 3 fails the change is rolled back with the new password, which this process still holds. That
is the one window where a target could be left with a password nothing has recorded, and closing it
is why rollback is not optional.

Each target gets its own generated password. Sharing one new value across an estate reproduces the
weakness a rotation is usually run to remove: with a shared password, a leak anywhere is a leak
everywhere, and the blast radius of the next incident is the whole estate rather than one host.

Secrets are never logged, echoed, or returned. Results carry a status and a server name, nothing more.
"""

from __future__ import annotations

import re
import secrets as _secrets
import string
from pathlib import Path
from typing import Any, Callable, Iterable

from db_ops.common import db_connect, sql_run

from db_ops.lib import data_sources
from db_ops.lib.data_sources import request_fill

# Symbols restricted to ones that survive an ODBC connection string, a SQL string literal and shell
# quoting without escaping. No quote, semicolon, backslash or brace: those are what turn a generated
# password into a syntax error or a truncated connection string weeks later, on one unlucky draw.
PASSWORD_SYMBOLS = "-_.!#%^*+="
PASSWORD_ALPHABET = string.ascii_letters + string.digits + PASSWORD_SYMBOLS
DEFAULT_PASSWORD_LENGTH = 28
MIN_PASSWORD_LENGTH = 12
DEFAULT_TIMEOUT_SECONDS = 10

# Engines whose change statement is implemented below. Others are reported as unsupported rather
# than attempted, so a rotation never half-applies against a syntax the module does not know.
SUPPORTED_ENGINES = ("sqlserver", "postgresql", "oracle", "mysql")


class PasswordRotationError(RuntimeError):
    """A rotation could not be attempted: bad request, unknown ref, unsupported engine."""


def generate_password(length: int = DEFAULT_PASSWORD_LENGTH) -> str:
    """A random password that satisfies the usual four-class complexity policy.

    Rejection-samples until every class is present rather than placing one character of each and
    shuffling: the latter leaks structure (position 0 is always a letter) to anyone who knows how
    the generator works. Never starts with a digit, which some ODBC parsers mis-handle unquoted.
    """
    length = int(length or DEFAULT_PASSWORD_LENGTH)
    if length < MIN_PASSWORD_LENGTH:
        raise PasswordRotationError(
            f"password_length must be at least {MIN_PASSWORD_LENGTH}; got {length}."
        )
    while True:
        candidate = "".join(_secrets.choice(PASSWORD_ALPHABET) for _ in range(length))
        if (
            any(c.islower() for c in candidate)
            and any(c.isupper() for c in candidate)
            and any(c.isdigit() for c in candidate)
            and any(c in PASSWORD_SYMBOLS for c in candidate)
            and not candidate[0].isdigit()
        ):
            return candidate


def _quote_literal(value: str) -> str:
    """A single-quoted SQL literal with embedded quotes doubled."""
    return "'" + str(value).replace("'", "''") + "'"


def _quote_identifier(value: str, style: str) -> str:
    if style == "bracket":
        return "[" + str(value).replace("]", "]]") + "]"
    if style == "backtick":
        return "`" + str(value).replace("`", "``") + "`"
    return '"' + str(value).replace('"', '""') + '"'


def build_change_statement(db_type: str, username: str, new_password: str, old_password: str) -> str:
    """The engine's own "change my password" statement, as a literal string.

    Literals, not bound parameters: these are DDL, and every one of these engines rejects a
    parameter marker in this position — SQL Server answers ``Incorrect syntax near '@P1'``. The
    quoting helpers double any embedded quote so a widened alphabet cannot break out of the literal.

    SQL Server and Oracle are given the old password too, so the statement is the *self-service*
    form and works for a login changing its own password without ``ALTER ANY LOGIN`` / ``ALTER USER``.
    """
    engine = db_connect.normalize_db_type(db_type)
    if engine not in SUPPORTED_ENGINES:
        raise PasswordRotationError(
            f"Password rotation is not implemented for db_type={db_type!r}; "
            f"supported: {', '.join(SUPPORTED_ENGINES)}."
        )
    if engine == "sqlserver":
        return (
            f"ALTER LOGIN {_quote_identifier(username, 'bracket')} "
            f"WITH PASSWORD = N{_quote_literal(new_password)} "
            f"OLD_PASSWORD = N{_quote_literal(old_password)}"
        )
    if engine == "postgresql":
        return (
            f"ALTER USER {_quote_identifier(username, 'double')} "
            f"WITH PASSWORD {_quote_literal(new_password)}"
        )
    if engine == "oracle":
        # Oracle takes the password as an identifier, and REPLACE makes it the self-service form.
        return (
            f"ALTER USER {_quote_identifier(username, 'double')} "
            f"IDENTIFIED BY {_quote_identifier(new_password, 'double')} "
            f"REPLACE {_quote_identifier(old_password, 'double')}"
        )
    return (
        f"ALTER USER {_quote_identifier(username, 'backtick')}@'%' "
        f"IDENTIFIED BY {_quote_literal(new_password)}"
    )


def select_refs(
    *,
    refs: Iterable[str] | None = None,
    match: str = "",
    data_dir: str | Path | None = None,
    key: str | None = None,
) -> list[str]:
    """The password_refs a request names, either explicitly or by regex over the ref names.

    ``match`` is matched against the ref **name** only — never a value — so selecting a set to
    rotate never requires decrypting and comparing secrets.
    """
    store = data_sources.load_secret_text(data_dir, key=key)
    chosen: list[str] = []
    if refs:
        missing = [r for r in refs if r not in store]
        if missing:
            raise PasswordRotationError(
                "password_ref not found in the secret store: " + ", ".join(sorted(missing))
            )
        chosen.extend(refs)
    if match:
        try:
            pattern = re.compile(match, re.IGNORECASE)
        except re.error as exc:
            raise PasswordRotationError(f"match is not a valid regular expression: {exc}") from exc
        chosen.extend(name for name in store if pattern.search(name))
    if not chosen:
        raise PasswordRotationError(
            "No password_ref selected: pass 'refs' (a list) and/or 'match' (a regex on ref names)."
        )
    return sorted(dict.fromkeys(chosen))


def resolve_ref_target(
    ref: str,
    *,
    data_dir: str | Path | None = None,
    key: str | None = None,
    host_overrides: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Find the database instance and login a password_ref belongs to.

    Walks the same chain the rest of db_ops walks — ``db_instances.json`` names a
    ``credential_name``, ``users.json`` maps that to a ``password_ref`` — rather than parsing the
    ref name, because the name is a label and the config is the truth.

    A ref used by more than one instance resolves to the first that is reachable-looking; the
    caller can pin it with ``host_overrides``. That happens with a clustered instance reached
    through two node addresses, where only one answers.
    """
    users_credentials = data_sources.load_all_credentials(data_dir)
    instances = data_sources.load_db_instances(data_dir)

    credential_names = {
        str(cred.get("credential_name") or "")
        for groups in users_credentials.values()
        for group in groups
        for cred in group.get("credentials", [])
        if str(cred.get("password_ref") or "") == ref
    }
    credential_names.discard("")

    matches = [
        instance
        for instance in instances
        if str(instance.get("default_credential_name") or "") in credential_names
    ]
    if not matches:
        # No guess from the ref's name (owner decision G3.4): the name is a label, and a password
        # changed on the host a label points at is a password changed on whoever holds that IP now.
        reason = (
            "is not referenced by any credential in users.json"
            if not credential_names
            else "has a credential but no db_instance uses it as default_credential_name"
        )
        raise PasswordRotationError(
            f"password_ref {ref!r} {reason}, so there is no server to change it on. "
            "Add the instance to db_instances.json with this credential as its "
            "default_credential_name - the rotation changes a password only where the inventory "
            "says it lives."
        )

    override = (host_overrides or {}).get(ref, "")
    chosen = matches[0]
    if override:
        for instance in matches:
            if str(instance.get("ip") or "") == override:
                chosen = instance
                break

    # The instance's own login, filled the way every app fills a request (rules R09): run-sql's
    # resolver stopped reading the inventory, and this module - whose job is the store - reads it.
    try:
        connection = request_fill.sql_connection(
            str(chosen.get("server_id") or ""), data_dir=data_dir,
            secrets=data_sources.load_secret_text(data_dir, key=key))
    except request_fill.RequestFillError as exc:
        raise PasswordRotationError(str(exc)) from exc
    target = sql_run.resolve_stated_connection({"connection": connection}, what="rotate-password")
    if override:
        target["ip"] = override
    target["password_ref"] = ref
    target["instance_count"] = len(matches)
    return target


def rotate_ref(
    ref: str,
    *,
    data_dir: str | Path | None = None,
    key: str | None = None,
    password_length: int = DEFAULT_PASSWORD_LENGTH,
    new_password: str = "",
    timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
    dry_run: bool = False,
    host_overrides: dict[str, str] | None = None,
    persist: Callable[[str, str], None] | None = None,
) -> dict[str, Any]:
    """Rotate one password_ref. Returns a result dict that never contains the password.

    ``persist(ref, value)`` stores the new value as soon as it is verified, before the next ref is
    touched. If storing fails the change is rolled back, so the database and the store never
    disagree about a password (review 0.25.0, F11.1).

    ``new_password`` lets an operator supply a value that has to match an external policy; omit it
    and one is generated. On success the value is under ``_new_password`` for the caller to persist —
    the only place it appears, and the CLI strips it before printing.
    """
    result: dict[str, Any] = {"password_ref": ref, "status": "FAILED", "detail": ""}
    try:
        target = resolve_ref_target(ref, data_dir=data_dir, key=key,
                                    host_overrides=host_overrides)
    except PasswordRotationError as exc:
        result.update(status="SKIPPED", detail=str(exc))
        return result

    result.update(
        server_id=target.get("server_id"),
        db_type=target.get("db_type"),
        host=target.get("ip"),
        port=target.get("port"),
        username=target.get("username"),
    )
    engine = db_connect.normalize_db_type(target.get("db_type"))
    if engine not in SUPPORTED_ENGINES:
        result.update(status="SKIPPED", detail=f"db_type {engine} is not supported for rotation")
        return result

    old_password = str(target.get("password") or "")
    if not old_password:
        result.update(status="SKIPPED", detail="the store holds no current password for this ref")
        return result

    # Step 1 - prove the current password before changing anything.
    try:
        connection = sql_run.connect_target(target, timeout_seconds=timeout_seconds)
    except Exception as exc:  # noqa: BLE001 - an unreachable host is an operator message.
        result.update(status="SKIPPED",
                      detail=f"current password/host not usable: {str(exc)[:200]}")
        return result

    if dry_run:
        db_connect.close_quietly(connection)
        result.update(status="READY", detail="reachable and the current password works")
        return result

    generated = new_password or generate_password(password_length)
    statement = build_change_statement(engine, str(target["username"]), generated, old_password)

    # Step 2 - change it.
    try:
        cursor = connection.cursor()
        cursor.execute(statement)
        if engine in ("postgresql", "mysql"):
            # Those two run the change inside the ambient transaction; the others auto-commit DDL.
            connection.commit()
    except Exception as exc:  # noqa: BLE001
        db_connect.close_quietly(connection)
        result.update(status="FAILED", detail=f"change statement rejected: {str(exc)[:200]}")
        return result
    db_connect.close_quietly(connection)

    # Step 3 - prove it on a new connection, because the old session is still authenticated.
    verify_target = dict(target)
    verify_target["password"] = generated
    try:
        verify = sql_run.connect_target(verify_target, timeout_seconds=timeout_seconds)
        db_connect.close_quietly(verify)
    except Exception as exc:  # noqa: BLE001
        result.update(status="FAILED", detail=f"verify failed: {str(exc)[:160]}")
        result["rollback"] = _rollback(verify_target, engine, generated, old_password,
                                       timeout_seconds)
        return result

    if persist is not None:
        try:
            persist(ref, generated)
        except Exception as exc:  # noqa: BLE001 - any store failure means: undo the change.
            rollback = _rollback(verify_target, engine, generated, old_password, timeout_seconds)
            result.update(status="FAILED", store_failed=True, rollback=rollback,
                          detail=f"changed and verified, but storing it failed ({str(exc)[:160]}); "
                                 f"{rollback}")
            if rollback.startswith("ROLLBACK FAILED"):
                # The one state with no good answer: the new value is in neither the store nor
                # the database's old password. It is handed back (never printed) so the caller
                # can still write it somewhere; the CLI says so loudly.
                result["_new_password"] = generated
            return result
        result.update(status="SUCCESS", stored=True,
                      detail="changed, re-authenticated on a new connection, and stored")
        return result

    result.update(status="SUCCESS", detail="changed and re-authenticated on a new connection")
    result["_new_password"] = generated
    return result


def _rollback(target: dict[str, Any], engine: str, current: str, previous: str,
              timeout_seconds: int) -> str:
    """Put the previous password back after a failed verify, using the value we just set."""
    try:
        connection = sql_run.connect_target(target, timeout_seconds=timeout_seconds)
    except Exception as exc:  # noqa: BLE001
        return f"ROLLBACK FAILED - could not reconnect: {str(exc)[:120]}"
    try:
        cursor = connection.cursor()
        cursor.execute(build_change_statement(engine, str(target["username"]), previous, current))
        if engine in ("postgresql", "mysql"):
            connection.commit()
        return "rolled back to the previous password"
    except Exception as exc:  # noqa: BLE001
        return f"ROLLBACK FAILED: {str(exc)[:120]}"
    finally:
        db_connect.close_quietly(connection)


def rotate(request: dict[str, Any], *, data_dir: str | Path | None = None,
           key: str | None = None,
           persist: Callable[[str, str], None] | None = None) -> dict[str, Any]:
    """Rotate every password_ref a request selects. Returns results with no secret values.

    Persisting is the caller's job (:func:`persist_rotated`) so a caller that only wants to test
    reachability, or that stores secrets somewhere else, is not forced through this module's writer.
    """
    if not isinstance(request, dict):
        raise PasswordRotationError("request must be a JSON object.")
    if request.get("allow_name_host"):
        # Refused, not ignored: a caller that asked for it believes some ref will be rotated on the
        # host its name spells, and silently doing nothing for that ref would read as done.
        raise PasswordRotationError(
            "allow_name_host was removed (owner decision G3.4): a password is changed only on the "
            "instance db_instances.json names for it. Add the instance, then rotate.")

    refs = request.get("refs") or request.get("password_refs") or []
    if isinstance(refs, str):
        refs = [refs]
    selected = select_refs(refs=refs, match=str(request.get("match") or ""),
                           data_dir=data_dir, key=key)
    explicit = request.get("passwords") or {}
    if not isinstance(explicit, dict):
        raise PasswordRotationError("'passwords' must be a JSON object of {password_ref: value}.")

    results: list[dict[str, Any]] = []
    stopped = ""
    for ref in selected:
        if stopped:
            # Storing an earlier password failed: the store is not to be trusted with another
            # change until somebody has looked (review 0.25.0, F11.1).
            results.append({"password_ref": ref, "status": "SKIPPED",
                            "detail": f"not started - storing {stopped}'s new password failed"})
            continue
        item = rotate_ref(
            ref,
            data_dir=data_dir,
            key=key,
            password_length=int(request.get("password_length") or DEFAULT_PASSWORD_LENGTH),
            new_password=str(explicit.get(ref) or ""),
            timeout_seconds=int(request.get("timeout_seconds") or DEFAULT_TIMEOUT_SECONDS),
            dry_run=bool(request.get("dry_run")),
            host_overrides=request.get("host_overrides") or {},
            persist=persist,
        )
        results.append(item)
        if item.get("store_failed"):
            stopped = ref
    counts: dict[str, int] = {}
    for item in results:
        counts[item["status"]] = counts.get(item["status"], 0) + 1
    return {"ok": not counts.get("FAILED"), "selected": len(selected),
            "summary": counts, "results": results}


def store_writer(*, data_dir: str | Path | None = None, key: str | None = None,
                 plaintext_store: str | Path | None = None) -> Callable[[str, str], None]:
    """``persist(ref, value)`` for :func:`rotate`: one ref into the secret store(s), at once."""
    from db_ops.lib import secret_text as _secret_text

    resolved_dir = Path(data_sources._resolve_data_dir(data_dir))  # noqa: SLF001 - same package

    def persist(ref: str, value: str) -> None:
        _secret_text.set_secret_everywhere(resolved_dir, ref, value, key=key,
                                           plaintext_store=plaintext_store, overwrite=True)
    return persist


def persist_rotated(outcome: dict[str, Any], *, data_dir: str | Path | None = None,
                    key: str | None = None,
                    plaintext_store: str | Path | None = None) -> int:
    """Write every SUCCESS result's new password into the secret store(s). Returns how many.

    Both stores are written when ``plaintext_store`` exists — see
    :func:`db_ops.lib.secret_text.set_secret_everywhere` for why updating only the encrypted blob
    is silently undone by the next deploy.
    """
    from db_ops.lib import secret_text as _secret_text

    resolved_dir = Path(data_sources._resolve_data_dir(data_dir))  # noqa: SLF001 - same package
    written = 0
    for item in outcome.get("results", []):
        value = item.pop("_new_password", "")
        if item.get("status") != "SUCCESS" or not value:
            continue
        _secret_text.set_secret_everywhere(resolved_dir, item["password_ref"], value,
                                           key=key, plaintext_store=plaintext_store,
                                           overwrite=True)
        written += 1
    return written


def strip_secrets(outcome: dict[str, Any]) -> dict[str, Any]:
    """Drop any residual password from a result before it is printed or logged."""
    for item in outcome.get("results", []):
        item.pop("_new_password", None)
    return outcome
