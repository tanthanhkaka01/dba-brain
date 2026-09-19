"""Prove, or fail to prove, that each secret in the store still logs in somewhere.

An audit that reports "not testable" is usually reporting a gap in the resolver, not a fact about
the estate. Almost every secret here *is* a login on something reachable — a database, a Windows
host over WinRM, or an Ubuntu host over SSH — and the reason an earlier pass could not check one
was nearly always that it looked in one config file and gave up.

So target resolution walks **every** place a ref can be named, in priority order:

1. ``db_instances.json`` — ``default_credential_name`` (a database login) or ``cmd_access``
   (an OS login, which also states ``method``: ssh or winrm, so the protocol is known, not guessed);
2. ``docker_db_connections.json`` — ``password_env``, which carries the real host and the
   non-default port a container publishes (5442, 1522, 5435 …). Missing this is why an earlier pass
   probed 5432 on a PostgreSQL container listening on 5442 and called the secret unusable;
3. ``restore_config.json`` — ``password_env`` / ``sql_password_env`` on a backup or restore job;
4. ``users.json`` ``remote_credentials`` — the host for an OS account no instance references;
5. the standard key name, which carries the IP — a label, so it is last and only with ``allow_name_host``.

Before all five sits ``db_instances.json`` ``sql_access`` — the block that says a target is
reached through the legacy Oracle tool rather than a driver. Its ``secret_ref`` (the shared
secret the bridge token is signed with) and ``connect_ref`` were named by no other source, so a
configured bridge secret resolved to ``unknown`` and was reported ``NO_TARGET`` while every
collection for that target failed on it. It is proven the way its own entry in
:data:`NOT_A_LOGIN` always claimed — by a bridge query.

When the protocol is not stated, it is **probed** rather than assumed: SSH on 22 and WinRM on
5985/5986 are tried in turn, because the estate is mixed and an Ubuntu host answered "unreachable"
only because something insisted on asking it over WinRM.

Not everything is a login, and those are reported as ``NOT_A_LOGIN`` with what they actually are —
a backup passphrase decrypts a file, not a session. That is a different statement from "could not
check", and the two must not be blurred.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from db_ops.common import data_sources, db_connect, host_probe, oracle_bridge, remote_exec, sql_run
from db_ops.common.password_rotation import target_from_ref_name
from db_ops.lib import sql_access

DEFAULT_TIMEOUT_SECONDS = 8

#: The statement a bridge check runs. Every Oracle release answers it, it reads nothing, and
#: it fails only for the reasons this check is about - a token the bridge will not accept, a
#: login it rejects, or a bridge that is not there.
BRIDGE_PROBE_SQL = "SELECT 1 FROM DUAL"
SSH_PORT = 22
WINRM_PORTS = (5985, 5986)

#: Refs that are key material or a service token rather than a login, keyed by the name fragment
#: that identifies them. Checking these means using the system they belong to, not opening a session.
NOT_A_LOGIN = {
    "BACKUP_ENC": "backup encryption passphrase - decrypts a backup set, there is no session to open",
    "ORACLE_BRIDGE": "shared secret for the Oracle 8i HTTP bridge - verified by a bridge query",
    "VAULT": "API token - verified by calling the certificate API, not by a database login",
    "TELEGRAM": "bot token - verified by calling the Telegram API (getMe)",
}

#: Web logins: a real credential, just not one you reach with a database driver or a shell. Checking
#: them over HTTP is what keeps "we cannot check this" honest - it should mean the estate, not a gap
#: in this module.
HTTP_LOGINS = {
    "GRAFANA": {"port": 8080, "path": "/api/user", "service": "Grafana"},
    "INFLUXDB": {"port": 8086, "path": "/api/v2/me", "service": "InfluxDB"},
}


class SecretCheckError(RuntimeError):
    """The check could not be set up: bad request, unreadable config."""


def _port_open(host: str, port: int, timeout: float = 3.0) -> bool:
    """Is anything listening there — the yes/no this module needs.

    Delegates to :func:`host_probe.probe_port`, which is the same socket open with the *reason*
    kept (refused vs timeout). This check only ever asked the yes/no half, and one implementation
    of "can I open a socket" is the point: the two answers must never be able to disagree.
    """
    return bool(host_probe.probe_port(host, port, timeout)["open"])


def _load(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_bytes().decode("utf-8-sig"))
    except Exception:  # noqa: BLE001 - a broken side-config must not stop the whole check.
        return {}


def resolve_check_target(
    ref: str,
    *,
    data_dir: str | Path | None = None,
    allow_name_host: bool = True,
) -> dict[str, Any]:
    """Where a ref can be proven, and over what protocol. Never raises; returns a ``kind``.

    ``kind`` is ``db``, ``remote``, ``not_a_login`` or ``unknown`` — the last meaning no config
    names this ref and its name is not the standard scheme, which is the only honest "cannot check".
    """
    # Config before name. A bridge secret matches the ``ORACLE_BRIDGE`` fragment below and would
    # stop there as "not a login" - true, and useless: it is provable, and the instance that names
    # it says how. Config is evidence; the fragment list is a guess about a name.
    named = _sql_access_target(ref, data_dir=data_dir)
    if named:
        return named

    for fragment, why in NOT_A_LOGIN.items():
        if fragment in ref:
            return {"kind": "not_a_login", "detail": why}

    for fragment, spec in HTTP_LOGINS.items():
        if ref.startswith(fragment + "_") or f"_{fragment}_" in ref:
            match = re.search(r"(\d{1,3})_(\d{1,3})_(\d{1,3})_(\d{1,3})", ref)
            if match:
                return {"kind": "http", "source": "key name", "service": spec["service"],
                        "host": ".".join(match.groups()), "port": spec["port"],
                        "path": spec["path"],
                        "username": ref.rsplit("_", 1)[-1].lower()}

    base = Path(data_sources._resolve_data_dir(data_dir))  # noqa: SLF001 - same package
    instances = data_sources.load_db_instances(data_dir)
    credentials = data_sources.load_all_credentials(data_dir)
    remotes = data_sources.load_remote_credentials(data_dir)

    db_creds, os_creds = set(), set()
    for groups in credentials.values():
        for group in groups:
            for cred in group.get("credentials", []):
                if str(cred.get("password_ref") or "") == ref:
                    db_creds.add(str(cred.get("credential_name") or ""))
    for group in remotes:
        for cred in group.get("credentials", []):
            if str(cred.get("password_ref") or "") == ref:
                os_creds.add(str(cred.get("credential_name") or ""))

    # 1a. a database login named by an instance
    for instance in instances:
        if str(instance.get("default_credential_name") or "") in db_creds and db_creds:
            try:
                target = sql_run.resolve_sqlserver_target(
                    str(instance.get("server_id") or ""), data_dir=data_dir)
            except Exception:  # noqa: BLE001 - fall through to the other sources.
                break
            target.update(kind="db", source="db_instances.json")
            return target

    # 1b. an OS login named by cmd_access - which states the protocol
    for instance in instances:
        access = instance.get("cmd_access") or {}
        if str(access.get("credential_name") or "") in os_creds and os_creds:
            username = ""
            for group in remotes:
                for cred in group.get("credentials", []):
                    if str(cred.get("password_ref") or "") == ref:
                        username = str(cred.get("username") or "")
            return {"kind": "remote", "source": "db_instances.json cmd_access",
                    "method": str(access.get("method") or ""),
                    "host": str(access.get("host") or instance.get("ip") or ""),
                    "port": access.get("port"), "username": username}

    # 2. a container connection - carries the published, non-default port
    for entry in _load(base / "docker_db_connections.json").get("docker_db_connections", []):
        if str(entry.get("password_env") or "") != ref:
            continue
        host = str(entry.get("host") or "")
        if not host:
            worker = _load(Path("config.json")).get("worker") or [{}]
            host = str(worker[0].get("host") or "")
        engine = db_connect.normalize_db_type(entry.get("db_type") or _engine_from_name(ref))
        return {"kind": "db", "source": "docker_db_connections.json", "db_type": engine,
                "ip": host, "port": entry.get("port"),
                "username": str(entry.get("username") or ""),
                "database_name": db_connect.default_database(engine),
                "service_name": "", "sqlserver_driver": "", "server_id": entry.get("id")}

    # 3. a backup/restore job
    hit = _restore_config_target(base, ref, remotes)
    if hit:
        return hit

    # 4. an OS account declared with a host but wired to no instance
    for group in remotes:
        for cred in group.get("credentials", []):
            if str(cred.get("password_ref") or "") == ref and group.get("host"):
                return {"kind": "remote", "source": "users.json remote_credentials",
                        "method": "", "host": str(group["host"]), "port": None,
                        "username": str(cred.get("username") or "")}

    # 5. the key name, last and only on request
    if allow_name_host:
        named = target_from_ref_name(ref)
        if named:
            named.update(kind="db", source="key name")
            return named
        match = re.match(r"^REMOTE_(\d{1,3})_(\d{1,3})_(\d{1,3})_(\d{1,3})_(.+)$", ref)
        if match:
            return {"kind": "remote", "source": "key name", "method": "",
                    "host": ".".join(match.groups()[:4]), "port": None,
                    "username": match.group(5).lower()}
    return {"kind": "unknown",
            "detail": "no config names this ref and its name is not the standard scheme"}


def _sql_access_target(ref: str, *, data_dir: str | Path | None = None) -> dict[str, Any] | None:
    """The legacy-Oracle target whose ``sql_access`` names this ref, or ``None``.

    Two fields can name one: ``secret_ref``, the shared secret the bridge token is signed with,
    and ``connect_ref``, a whole connect string. Both are proven the same way - by running a
    statement through the transport that uses them - so the target carries the field that named
    it rather than two kinds of target.
    """
    for instance in data_sources.load_db_instances(data_dir):
        access = instance.get("sql_access") or {}
        if not sql_access.is_legacy(access):
            continue
        for field, named in sql_access.secret_refs(access).items():
            if named != ref:
                continue
            return {
                "kind": "bridge", "source": f"db_instances.json sql_access.{field}",
                "field": field, "method": str(access.get("method") or ""),
                "sql_access": dict(access), "instance": dict(instance),
                "host": str(instance.get("ip") or instance.get("server_id") or ""),
                "server_id": str(instance.get("server_id") or ""),
                "_data_dir": str(data_dir) if data_dir else "",
            }
    return None


def _engine_from_name(ref: str) -> str:
    prefix = ref.split("_", 1)[0]
    return {"MSSQL": "sqlserver", "SQLSERVER": "sqlserver", "POSTGRE": "postgresql",
            "PG": "postgresql", "ORACLE": "oracle", "MYSQL": "mysql"}.get(prefix, "sqlserver")


def _restore_config_target(base: Path, ref: str, remotes: list[dict[str, Any]]) -> dict[str, Any] | None:
    config = _load(base / "restore_config.json").get("backup_restore", {})
    for job in list(config.get("backups", [])) + list(config.get("restores", [])):
        for side in ("source", "target"):
            block = job.get(side) or {}
            if str(block.get("password_env") or "") == ref:
                return {"kind": "remote", "source": f"restore_config.json {side}", "method": "",
                        "host": str(block.get("host") or ""), "port": None,
                        "username": str(block.get("username") or "")}
            if str(block.get("sql_password_env") or "") == ref:
                engine = _engine_from_name(ref)
                return {"kind": "db", "source": f"restore_config.json {side}", "db_type": engine,
                        "ip": str(block.get("host") or ""), "port": block.get("sql_port"),
                        "username": str(block.get("sql_username") or "sa"),
                        "database_name": db_connect.default_database(engine),
                        "service_name": "", "sqlserver_driver": "", "server_id": job.get("restore_id")}
    return None


def check_ref(ref: str, *, data_dir: str | Path | None = None, key: str | None = None,
              timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
              allow_name_host: bool = True) -> dict[str, Any]:
    """Try to authenticate with one secret. Returns a result that never contains the value."""
    secrets = data_sources.load_secret_text(data_dir, key=key)
    if ref not in secrets:
        return {"password_ref": ref, "status": "UNKNOWN_REF",
                "detail": "not present in the secret store"}

    target = resolve_check_target(ref, data_dir=data_dir, allow_name_host=allow_name_host)
    result: dict[str, Any] = {"password_ref": ref, "kind": target.get("kind"),
                              "source": target.get("source", "")}
    if target["kind"] in ("not_a_login", "unknown"):
        result.update(status="NOT_A_LOGIN" if target["kind"] == "not_a_login" else "NO_TARGET",
                      detail=target.get("detail", ""))
        return result

    password = secrets[ref]
    if target["kind"] == "db":
        return _check_db(result, target, password, timeout_seconds)
    if target["kind"] == "http":
        return _check_http(result, target, password, timeout_seconds)
    if target["kind"] == "bridge":
        # The whole store, not just this ref: a bridge query needs the token secret *and* the
        # login's password, and they are two different entries. The detail says which one was
        # under test, because a failure can belong to either.
        return _check_bridge(result, target, secrets, timeout_seconds, data_dir=data_dir)
    return _check_remote(result, target, password, timeout_seconds)


def _check_http(result: dict[str, Any], target: dict[str, Any], password: str,
                timeout_seconds: int) -> dict[str, Any]:
    """Basic-auth against the service's own "who am I" endpoint.

    401/403 is the service saying the credential is wrong, which is a real AUTH_FAILED - the same
    verdict a database would give. Anything else is a transport problem and says so.
    """
    import base64
    import urllib.error
    import urllib.request

    host, port = str(target.get("host") or ""), int(target.get("port") or 0)
    username = str(target.get("username") or "")
    url = f"http://{host}:{port}{target.get('path', '/')}"
    result.update(protocol="http", host=host, port=port, username=username,
                  service=target.get("service"))
    if not _port_open(host, port):
        result.update(status="UNREACHABLE", detail=f"{host}:{port} closed or filtered")
        return result
    token = base64.b64encode(f"{username}:{password}".encode()).decode()
    request = urllib.request.Request(url, headers={"Authorization": f"Basic {token}"})
    try:
        with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
            result.update(status="OK",
                          detail=f"authenticated to {target.get('service')} (HTTP {response.status})")
    except urllib.error.HTTPError as exc:
        if exc.code in (401, 403):
            result.update(status="AUTH_FAILED", detail=f"{target.get('service')} rejected the credential (HTTP {exc.code})")
        else:
            result.update(status="CONNECT_FAILED", detail=f"HTTP {exc.code} from {url}")
    except Exception as exc:  # noqa: BLE001
        result.update(status="CONNECT_FAILED", detail=str(exc)[:160])
    return result


def oracle_service_for_host(host: str, data_dir: str | Path | None = None) -> str:
    """The Oracle service/SID declared for an IP anywhere in the config, or ''.

    Oracle connects by service, not by database, so a target without one cannot build a DSN at all.
    The name-derived path has no service to offer, so it is borrowed from whatever entry already
    describes that host rather than guessed - guessing means repeated failed logins, and Oracle
    counts those against FAILED_LOGIN_ATTEMPTS.
    """
    for group in data_sources.load_all_credentials(data_dir).get("oracle", []):
        if host in str(group.get("server_id") or "").replace("-", "."):
            service = str(group.get("service_name") or group.get("sid") or "")
            if service:
                return service
    for instance in data_sources.load_db_instances(data_dir):
        if str(instance.get("ip") or "") == host and instance.get("service_name"):
            return str(instance["service_name"])
    return ""


def _check_db(result: dict[str, Any], target: dict[str, Any], password: str,
              timeout_seconds: int) -> dict[str, Any]:
    engine = db_connect.normalize_db_type(target.get("db_type"))
    host = str(target.get("ip") or "")
    port = int(target.get("port") or 0) or _default_port(engine)
    result.update(protocol=engine, host=host, port=port, username=target.get("username"))
    if not host:
        result.update(status="NO_TARGET", detail="config names this ref but carries no host")
        return result
    if engine == "oracle" and not str(target.get("service_name") or ""):
        service = oracle_service_for_host(host, target.get("_data_dir"))
        if not service:
            result.update(
                status="NO_TARGET",
                detail=(f"Oracle needs a service_name and none is declared for {host} in "
                        "users.json or db_instances.json - add one to make this checkable"))
            return result
        target = dict(target)
        target["service_name"] = service
        target["database_name"] = service
    if not _port_open(host, port):
        result.update(status="UNREACHABLE",
                      detail=f"{host}:{port} closed or filtered from this machine")
        return result
    attempt = dict(target)
    attempt["password"] = password
    attempt["port"] = port
    try:
        connection = sql_run.connect_target(attempt, timeout_seconds=timeout_seconds)
        try:
            connection.close()
        except Exception:  # noqa: BLE001
            pass
        result.update(status="OK", detail="authenticated")
    except Exception as exc:  # noqa: BLE001
        result.update(status=_classify(str(exc)), detail=str(exc)[:200].replace("\n", " "))
    return result


def _check_bridge(result: dict[str, Any], target: dict[str, Any], secrets: dict[str, str],
                  timeout_seconds: int, *, data_dir: str | Path | None = None) -> dict[str, Any]:
    """Prove a legacy-Oracle secret by running one statement through the transport that uses it.

    There is no session to open with the shared secret on its own - it signs a token, and the
    token is only worth anything to the bridge. So the check is the real call: sign a token,
    POST it, and read the answer. That is what :data:`NOT_A_LOGIN` promised for this ref all
    along while resolution was returning "no config names it".

    The verdict covers the *pair* - token secret and database login - because that is what the
    transport needs and there is no way to exercise one without the other. ``result["field"]``
    says which of the two this ref is.
    """
    import time
    import urllib.parse

    access = target.get("sql_access") or {}
    instance = target.get("instance") or {}
    method = str(target.get("method") or "")
    bridge_url = str(access.get("bridge_url") or "")
    result.update(protocol=method, host=target.get("host"), field=target.get("field"),
                  server_id=target.get("server_id"))

    credential: dict[str, Any] | None = None
    if target.get("field") != "connect_ref":
        # The connect string is built from the instance's own credential. Without one there is
        # nothing to sign a token *about*, and saying that is a different answer from a bridge
        # that refused us.
        db_type = db_connect.normalize_db_type(instance.get("db_type") or "oracle")
        try:
            credential = data_sources.find_database_credential(
                data_sources.load_all_credentials(data_dir).get(db_type, []),
                server_id=str(instance.get("server_id") or ""),
                credential_name=str(instance.get("default_credential_name") or ""),
                db_type=db_type,
                service_name=str(instance.get("service_name") or ""),
                instance_name=str(instance.get("instance_name") or ""),
            )
        except Exception as exc:  # noqa: BLE001 - report it, the same as any other missing target.
            result.update(status="NO_TARGET",
                          detail=f"{target.get('source')} names this ref, but the instance has no "
                                 f"credential to build a connect string with: {exc}")
            return result

    if method == "api":
        parsed = urllib.parse.urlsplit(bridge_url)
        host = parsed.hostname or ""
        port = int(parsed.port or (443 if parsed.scheme == "https" else 80))
        result.update(port=port)
        # The 8i bridge is started by hand on its host and nothing restarts it, so "down" is the
        # ordinary state to meet here - and it says nothing about the secret.
        if host and not _port_open(host, port):
            result.update(status="UNREACHABLE",
                          detail=f"the Oracle bridge at {bridge_url} is not listening - start it "
                                 "on that host before reading this as a bad secret")
            return result

    try:
        oracle_bridge.run_query(
            sql=BRIDGE_PROBE_SQL, sql_access=access, secrets=secrets, credential=credential,
            host=str(instance.get("ip") or instance.get("server_id") or ""),
            port=instance.get("port"),
            service_name=str(instance.get("service_name") or instance.get("instance_name") or ""),
            now=time.time(), limit=1, timeout_seconds=timeout_seconds,
        )
        result.update(status="OK",
                      detail=f"a statement ran over the '{method}' transport, so the token this "
                             "secret signs is one the bridge accepts")
    except Exception as exc:  # noqa: BLE001 - every failure here is a result, not a crash.
        result.update(status=_classify(str(exc)), detail=str(exc)[:200].replace("\n", " "))
    return result


def _default_port(engine: str) -> int:
    return {"sqlserver": 1433, "postgresql": 5432, "oracle": 1521, "mysql": 3306}.get(engine, 1433)


def _check_remote(result: dict[str, Any], target: dict[str, Any], password: str,
                  timeout_seconds: int) -> dict[str, Any]:
    host = str(target.get("host") or "")
    username = str(target.get("username") or "")
    result.update(host=host, username=username)
    if not host:
        result.update(status="NO_TARGET", detail="config names this ref but carries no host")
        return result

    stated = str(target.get("method") or "").lower()
    port = target.get("port")
    # The estate is mixed - Windows over WinRM and Ubuntu over SSH - so when cmd_access does not
    # state the method, ask the host instead of assuming one and calling the answer "unreachable".
    if stated == "ssh":
        candidates = [("ssh", int(port or SSH_PORT))]
    elif stated == "winrm":
        candidates = [("winrm", int(port or WINRM_PORTS[0]))]
    elif port:
        candidates = [("ssh" if int(port) == SSH_PORT else "winrm", int(port))]
    else:
        candidates = [("ssh", SSH_PORT)] + [("winrm", p) for p in WINRM_PORTS]

    tried = []
    for method, candidate_port in candidates:
        if not _port_open(host, candidate_port):
            tried.append(f"{method}:{candidate_port} closed")
            continue
        result.update(protocol=method, port=candidate_port)
        try:
            access = remote_exec.RemoteAccess.from_json(
                {"method": method, "host": host, "port": candidate_port, "username": username,
                 "password": password, "auth_type": "password",
                 "timeout_seconds": timeout_seconds}, resolve_key=False)
            with remote_exec.open_session(access) as session:
                session.run("echo db_ops_probe" if method == "ssh" else "Write-Output db_ops_probe")
            result.update(status="OK", detail=f"authenticated over {method}")
            return result
        except Exception as exc:  # noqa: BLE001
            result.update(status=_classify(str(exc)), detail=str(exc)[:200].replace("\n", " "))
            return result
    # A host can be up and still have nothing to authenticate against: plenty of Windows boxes here
    # are administered by RDP only, with WinRM never enabled. Saying "no management port" rather
    # than "unreachable" is the difference between "the host is gone" and "there is no way in from
    # a script", which lead to completely different follow-ups.
    rdp = _port_open(host, 3389, timeout=2.0)
    result.update(
        status="NO_MANAGEMENT_PORT" if rdp else "UNREACHABLE",
        detail=("host answers on RDP 3389 but neither SSH nor WinRM is listening - it is "
                "administered interactively, so this credential cannot be proven from a script"
                if rdp else "nothing answered (" + ", ".join(tried) + ")"),
    )
    return result


def _classify(message: str) -> str:
    lowered = message.lower()
    if any(token in lowered for token in (
            "login failed", "password authentication failed", "ora-01017", "access denied",
            "failed to authenticate", "authentication failed", "permission denied")):
        return "AUTH_FAILED"
    return "CONNECT_FAILED"


def check(request: dict[str, Any], *, data_dir: str | Path | None = None,
          key: str | None = None) -> dict[str, Any]:
    """Check every secret a request selects. ``{}`` checks the whole store."""
    if not isinstance(request, dict):
        raise SecretCheckError("request must be a JSON object.")
    secrets = data_sources.load_secret_text(data_dir, key=key)
    refs = request.get("refs") or request.get("password_refs") or []
    if isinstance(refs, str):
        refs = [refs]
    match = str(request.get("match") or "")
    if refs:
        selected = [r for r in refs]
    elif match:
        try:
            pattern = re.compile(match, re.IGNORECASE)
        except re.error as exc:
            raise SecretCheckError(f"match is not a valid regular expression: {exc}") from exc
        selected = [name for name in secrets if pattern.search(name)]
    else:
        selected = list(secrets)

    results = [
        check_ref(ref, data_dir=data_dir, key=key,
                  timeout_seconds=int(request.get("timeout_seconds") or DEFAULT_TIMEOUT_SECONDS),
                  allow_name_host=bool(request.get("allow_name_host", True)))
        for ref in sorted(selected)
    ]
    summary: dict[str, int] = {}
    for item in results:
        summary[item["status"]] = summary.get(item["status"], 0) + 1
    unresolved = [i["password_ref"] for i in results if i["status"] == "NO_TARGET"]
    return {"ok": not unresolved, "selected": len(results), "summary": summary,
            "results": results}
