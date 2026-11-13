"""Who can take a database's files is read where each engine and host says it, and shown as read.

The operator, 2026-11-11: *the backups are protected now, but anyone who gets onto the VM can copy
the database's own data files and open them on their machine.* dbabrain's part in 0.38.0 is to see
and report it, read-only: whether each database's data is encrypted at rest
(``SECURITY_DATA_ENCRYPTION``), who can be an administrator on the host (``SECURITY_HOST_ADMINS``),
and a production instance on an edition not meant for production.

Both pages render one block (``reports.protection``), so these tests hold what a reader relies on:
a state is the engine's own word; a certificate is *in the vault* only when the vault was read and
holds that thumbprint; a section the collector could not read is listed with its reason; the edition
rule is the estate's configuration; and nothing here is graded - the rows are OK, and what alerts is
a change between two reads (``test_a_change_between_two_reads_is_an_event.py``).
"""
from __future__ import annotations

import datetime
import json
from pathlib import Path

from conftest import shipped_config

from dbabrain.lib import inventory_render
from dbabrain.metrics.definitions import load_metric_definitions
from dbabrain.reports import inventory_health, inventory_report, protection, server_report
from dbabrain.reports.server_sections import build_server_detail

AT = "2026-11-11T09:00:00Z"
RULE = {"production_environments": ["prod"], "non_production_editions": ["Developer", "Express", "Evaluation"]}
THUMB = "0A1B2C3D4E5F60718293A4B5C6D7E8F901234567"


def _map(code, rows):
    """A current snapshot as the inventory indexes it: {(code, item): row}."""
    return {(code, item): {"metric_code": code, "metric_item": item, "metric_value": value,
                           "metric_unit": unit, "status": "OK", "message": message, "collected_at": AT}
            for item, value, unit, message in rows}


SQLSERVER = _map(protection.ENCRYPTION_CODE, [
    ("SalesDb", "encrypted", "tde",
     "engine=sqlserver, state=3, database_state=ONLINE, algorithm=AES_256, percent_complete=0.0, "
     f"encryptor_type=certificate, encryptor=TDE_Cert, thumbprint={THUMB}, certificate_expires=2030-01-01, "
     "private_key_backed_up=never"),
    ("Scratch", "not encrypted", "tde", "engine=sqlserver, state=none, database_state=ONLINE"),
])
ORACLE = _map(protection.ENCRYPTION_CODE, [
    ("wallet", "OPEN", "keystore",
     "engine=oracle, wallet_type=AUTOLOGIN, wrl_type=FILE, location=/u01/app/oracle/wallet, opens_without_password=yes"),
    ("tablespace:USERS", "encrypted", "tde", "engine=oracle, contents=PERMANENT, status=ONLINE, algorithm=AES256"),
    ("tablespace:SYSTEM", "not encrypted", "tde", "engine=oracle, contents=PERMANENT, status=ONLINE, algorithm=-"),
])
POSTGRESQL = _map(protection.ENCRYPTION_CODE, [
    ("appdb", "not encrypted", "tde",
     "engine=postgresql, provider=none, pg_tde_available=no, tables_encrypted=0, tables_total=12, "
     "data_checksums=on, server_version=18.6"),
])
LINUX = _map(protection.HOST_ADMINS_CODE, [
    ("read_as", "dbadmin", "summary", "account=dbadmin, root_access=no, groups_read=sudo wheel admin docker"),
    ("group:sudo:dbadmin", "member", "account", "group=sudo, gid=27, account=dbadmin"),
    ("group:docker:dbadmin", "member", "account", "group=docker, gid=107, account=dbadmin"),
    ("group:sudo:ops", "member", "account", "group=sudo, gid=27, account=ops"),
    ("uid0:root", "uid 0", "account", "account=root, uid=0"),
    ("sshd:PermitRootLogin", "not set", "setting", "source=/etc/ssh/sshd_config and its includes"),
    ("sshd:PasswordAuthentication", "yes", "setting", "source=sshd -T (the effective configuration)"),
    ("root_key", "unreadable", "unreadable", "/root/.ssh/authorized_keys needs root: dbadmin is not root"),
])
WINDOWS = _map(protection.HOST_ADMINS_CODE, [
    ("read_as", "CORP\\svc-monitor", "summary", "account=CORP\\svc-monitor"),
    ("group:Administrators:DBHOST\\Administrator", "User", "account",
     "group=Administrators, local_name=Administrators, account=DBHOST\\Administrator, class=User, source=local, sid=S-1-5-21-1-2-3-500"),
    ("group:Administrators:CORP\\Domain Admins", "Group", "account",
     "group=Administrators, local_name=Administrators, account=CORP\\Domain Admins, class=Group, source=domain, sid=S-1-5-21-9-9-9-512"),
    ("check:domain_admins_in_administrators", "yes", "setting", "the domain group with RID 512"),
    ("rdp:enabled", "yes", "setting", "fDenyTSConnections=0"),
])


# ------------------------------------------------------------------------------- data-file encryption
def test_each_database_says_the_engines_own_state_and_the_unencrypted_ones_come_first():
    block = protection.build_encryption(SQLSERVER, vault=set())
    assert [(row["name"], row["state"], row["encrypted"]) for row in block["items"]] == [
        ("Scratch", "not encrypted", False), ("SalesDb", "encrypted", True)]
    assert (block["encrypted"], block["total"], block["engine"], block["asOf"]) == (1, 2, "sqlserver", AT)
    sales = block["items"][1]
    assert (sales["algorithm"], sales["protector"], sales["protectorType"], sales["thumbprint"]) == (
        "AES_256", "TDE_Cert", "certificate", THUMB)


def test_a_certificate_is_in_the_vault_only_when_the_vault_was_read_and_holds_its_thumbprint():
    def vault_word(vault):
        return protection.build_encryption(SQLSERVER, vault=vault)["items"][1]["vault"]

    assert vault_word({THUMB.lower()}) == "kept"
    assert vault_word(set()) == "not kept"
    assert vault_word(None) == ""            # the vault was not read: nothing is claimed either way
    # A database with no thumbprint to hold has nothing to say about the vault, read or not.
    assert protection.build_encryption(SQLSERVER, vault=set())["items"][0]["vault"] == ""


def test_an_encrypted_database_whose_certificate_the_vault_lacks_is_a_finding():
    block = protection.build_protection(SQLSERVER, vault=set())
    assert any("SalesDb is encrypted by TDE_Cert" in line and "vault does not hold" in line
               for line in block["findings"])
    kept = protection.build_protection(SQLSERVER, vault={THUMB.lower()})
    assert not any("vault does not hold" in line for line in kept["findings"])


def test_databases_without_encryption_are_one_finding_with_the_engines_way_to_turn_it_on():
    block = protection.build_protection(SQLSERVER, vault=None)
    plain = [line for line in block["findings"] if line.startswith("data files not encrypted")]
    assert len(plain) == 1
    assert "1 of 2 databases (Scratch)" in plain[0] and "SET ENCRYPTION ON" in plain[0]


def test_an_oracle_auto_login_keystore_on_the_host_is_named_as_such():
    block = protection.build_protection(ORACLE)
    store = block["encryption"]["keystores"][0]
    assert (store["status"], store["walletType"], store["opensWithoutPassword"]) == ("OPEN", "AUTOLOGIN", "yes")
    assert [(row["name"], row["scope"], row["encrypted"]) for row in block["encryption"]["items"]] == [
        ("SYSTEM", "tablespace", False), ("USERS", "tablespace", True)]
    assert any("auto-login keystore (AUTOLOGIN) at /u01/app/oracle/wallet" in line for line in block["findings"])
    assert any("1 of 2 tablespaces (SYSTEM)" in line for line in block["findings"])


def test_postgresql_says_it_has_no_provider_rather_than_assuming_one():
    block = protection.build_protection(POSTGRESQL)
    row = block["encryption"]["items"][0]
    assert (row["state"], row["provider"], row["tablesTotal"]) == ("not encrypted", "none", "12")
    assert any("community PostgreSQL has no data-file encryption" in line for line in block["findings"])


def test_a_read_that_failed_says_so_and_is_never_shown_as_not_encrypted():
    failed = {(protection.ENCRYPTION_CODE, None): {
        "metric_code": protection.ENCRYPTION_CODE, "metric_item": None, "metric_value": None,
        "metric_unit": None, "status": "WARNING", "message": "login failed for user monitor",
        "collected_at": AT}}
    block = protection.build_protection(failed)
    assert block["encryption"]["error"] == "login failed for user monitor"
    assert block["encryption"]["items"] == [] and block["findings"] == []


# ------------------------------------------------------------------------------- the host's administrators
def test_a_linux_host_lists_its_admin_accounts_settings_and_what_it_could_not_read():
    block = protection.build_admins(LINUX)
    assert [(row["group"], row["account"]) for row in block["accounts"]] == [
        ("docker", "dbadmin"), ("sudo", "dbadmin"), ("sudo", "ops"), ("uid 0", "root")]
    assert block["distinctAccounts"] == 3                      # dbadmin is in two groups, counted once
    assert (block["readAs"], block["rootAccess"]) == ("dbadmin", "no")
    marked = {row["name"]: row["marked"] for row in block["settings"]}
    assert marked == {"sshd:PasswordAuthentication": True, "sshd:PermitRootLogin": False}
    assert block["keys"] == []
    assert block["unreadable"] == [{"section": "root_key",
                                    "why": "/root/.ssh/authorized_keys needs root: dbadmin is not root"}]


def test_a_windows_host_lists_administrators_with_their_source_and_the_domain_admins_answer():
    block = protection.build_admins(WINDOWS)
    assert [(row["group"], row["account"], row["kind"], row["source"]) for row in block["accounts"]] == [
        ("Administrators", "CORP\\Domain Admins", "Group", "domain"),
        ("Administrators", "DBHOST\\Administrator", "User", "local")]
    settings = {row["name"]: (row["value"], row["marked"]) for row in block["settings"]}
    # Remote Desktop being on is what a Windows server is; it is shown, not marked.
    assert settings == {"check:domain_admins_in_administrators": ("yes", False), "rdp:enabled": ("yes", False)}


def test_a_root_key_is_listed_by_its_fingerprint_with_its_comment():
    rows = _map(protection.HOST_ADMINS_CODE, [
        ("root_key:SHA256:abcDEF", "ED25519", "key", "file=/root/.ssh/authorized_keys, comment=ops@jump"),
    ])
    assert protection.build_admins(rows)["keys"] == [{
        "fingerprint": "SHA256:abcDEF", "type": "ED25519", "comment": "ops@jump",
        "file": "/root/.ssh/authorized_keys"}]


def test_a_change_row_of_the_newest_read_is_listed_as_a_change_not_as_an_account():
    rows = dict(LINUX)
    rows.update(_map(protection.HOST_ADMINS_CODE, [
        ("change:added:group:sudo:mallory", "member", "change",
         "added: group:sudo:mallory (member) | between the read of T1 and the read of T2")]))
    rows[(protection.HOST_ADMINS_CODE, "change:added:group:sudo:mallory")]["status"] = "ERROR"
    block = protection.build_protection(rows)
    assert [(change["item"], change["status"]) for change in block["changes"]] == [
        ("added:group:sudo:mallory", "ERROR")]
    assert all(row["account"] != "mallory" for row in block["admins"]["accounts"])


# ------------------------------------------------------------------------------- edition against environment
def test_a_production_instance_on_developer_edition_is_a_finding():
    line = protection.edition_finding(edition="Developer Edition (64-bit)", environment="prod", rule=RULE)
    assert line == "Developer Edition (64-bit) on a prod instance: Developer is an edition not meant for production"
    block = protection.build_protection({}, environment="prod", edition="Developer Edition (64-bit)",
                                        edition_rule=RULE)
    assert block["editionFinding"] == line and block["findings"] == [line]


def test_the_same_edition_on_a_lab_or_a_production_edition_is_not():
    assert protection.edition_finding(edition="Developer Edition (64-bit)", environment="lab", rule=RULE) == ""
    assert protection.edition_finding(edition="Standard Edition (64-bit)", environment="prod", rule=RULE) == ""
    assert protection.edition_finding(edition="", environment="prod", rule=RULE) == ""


def test_which_editions_and_environments_count_is_configuration_not_code():
    # Without the estate's rule nothing is a finding: the module holds no list of its own.
    assert protection.edition_finding(edition="Developer Edition", environment="prod", rule=None) == ""
    shipped = {m.metric_code: m for m in load_metric_definitions(shipped_config("metric_definitions.json"))}
    assert shipped[protection.ENCRYPTION_CODE].report_policy["edition_rule"]["production_environments"] == ["prod"]
    assert "Developer" in shipped[protection.ENCRYPTION_CODE].report_policy["edition_rule"]["non_production_editions"]


def test_a_server_with_neither_metric_and_no_finding_has_no_block():
    assert protection.build_protection({}) == {}
    assert protection.build_protection({}, environment="prod", edition="Enterprise Edition", edition_rule=RULE) == {}


# ------------------------------------------------------------------------------- wired through both pages
def test_the_overlay_builds_the_block_with_the_environment_and_the_instances_edition():
    code_map = dict(SQLSERVER)
    code_map[("INSTANCE_STATUS", "instance")] = {
        "metric_code": "INSTANCE_STATUS", "metric_item": "instance", "metric_value": "1", "status": "OK",
        "collected_at": AT,
        "message": "SQL Server connection is available. server=SALESDB01; version=16.0.4135.4; "
                   "edition=Developer Edition (64-bit); engine=Enterprise"}
    overlay = inventory_health.build_server_overlay(
        "ACME-192-0-2-20", "192.0.2.20", code_map, environment="prod", edition_rule=RULE, vault=set())
    block = overlay["protection"]
    assert block["environment"] == "prod" and "Developer" in block["editionFinding"]
    assert block["encryption"]["total"] == 2


def test_the_block_survives_the_merge_into_the_inventory_and_reaches_both_models():
    assert "protection" in inventory_render.HEALTH_BLOCKS
    assert {"SECURITY_DATA_ENCRYPTION", "SECURITY_HOST_ADMINS"} <= set(inventory_health.HEALTH_CODES)
    block = protection.build_protection(SQLSERVER, environment="prod", edition="Developer Edition",
                                        edition_rule=RULE, vault=set())
    # Collected just now: a server showing last known values is named as such and not carded.
    now = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    data = {"servers": [{"server_id": "ACME-192-0-2-20", "ip": "192.0.2.20", "protection": block,
                         "health_as_of": now, "databases": [{"db_type": "sqlserver"}]}]}
    _scope, models = inventory_report.build_models(data)
    assert models[0]["protection"] == block
    # The server page carries the inventory's own block, whole.
    assert build_server_detail(models[0])["protection"] == block
    cards = inventory_report.build_triage(models)
    titles = [card["title"] for card in cards]
    assert "1 production instance(s) on an edition not meant for production" in titles
    assert "Data files not encrypted at rest on 1 server(s)" in titles


def test_both_pages_render_the_block():
    server_page = Path(server_report.TEMPLATE_HTML).read_text(encoding="utf-8")
    fleet_page = Path(inventory_report.TEMPLATE_HTML).read_text(encoding="utf-8")
    assert "function protectionBlock(p)" in server_page
    assert "protectionBlock((d.serverDetail || {}).protection)" in server_page
    assert "const protectionBlock = s =>" in fleet_page
    assert "${protectionBlock(s)}" in fleet_page


# ------------------------------------------------------------------------------- the metrics as shipped
def test_each_engine_and_platform_has_its_collector_and_the_rest_say_why_not():
    shipped = {m.metric_code: m for m in load_metric_definitions(shipped_config("metric_definitions.json"))}
    encryption = shipped[protection.ENCRYPTION_CODE]
    files = {variant.name: variant for variant in encryption.variants}
    for name in ("sqlserver_2008_plus", "oracle_12c_plus", "oracle_11g", "postgresql_10_plus"):
        assert files[name].supported and files[name].path and files[name].path.exists(), name
    assert files["postgresql_10_plus"].per_database
    for name in ("sqlserver_2005", "oracle_before_11g", "mysql_todo"):
        assert not files[name].supported and files[name].unsupported_reason, name
    admins = shipped[protection.HOST_ADMINS_CODE]
    assert sorted(variant.platform for variant in admins.variants) == ["linux", "windows"]
    assert all(variant.path and variant.path.exists() for variant in admins.variants)
    assert (encryption.category, admins.category, admins.collector_type) == ("security", "security", "cmd")


def test_the_rows_are_never_graded_by_the_collectors_themselves():
    root = Path(inventory_health.__file__).resolve().parents[1] / "metrics" / "collectors"
    sql = [root / "sqlserver" / "082_sqlserver_data_encryption.sql",
           root / "oracle" / "082_oracle_data_encryption.sql",
           root / "oracle" / "082_oracle_data_encryption_11g.sql",
           root / "postgresql" / "076_postgresql_data_encryption.sql"]
    for path in sql:
        text = "\n".join(line for line in path.read_text(encoding="utf-8").splitlines()
                         if not line.lstrip().startswith("--"))
        assert "'OK'" in text and "WARNING" not in text and "CRITICAL" not in text, path.name
    linux = (root / "os" / "linux" / "016_security_host_admins.sh").read_text(encoding="utf-8")
    assert '"status":"OK"' in linux and "\r" not in linux
    windows = (root / "os" / "windows" / "017_security_host_admins.ps1").read_text(encoding="utf-8")
    assert "status = 'OK'" in windows
    # The host collectors only read: nothing that writes, adds or removes on the host.
    for word in ("usermod", "gpasswd", "rm -", "Add-LocalGroupMember", "Remove-LocalGroupMember", "Set-ItemProperty"):
        assert word not in linux and word not in windows, word


def test_the_three_copies_of_the_catalogue_carry_both_metrics_alike():
    root = Path(inventory_health.__file__).resolve().parents[2]
    copies = [root / "dbabrain" / "metrics" / "catalogue" / "metric_definitions.json",
              root / "data" / "metric_definitions.example.json"]
    entries = []
    for path in copies:
        metrics = {m["metric_code"]: m for m in json.loads(path.read_text(encoding="utf-8"))["metrics"]}
        entries.append((metrics[protection.ENCRYPTION_CODE], metrics[protection.HOST_ADMINS_CODE]))
    assert entries[0] == entries[1]
