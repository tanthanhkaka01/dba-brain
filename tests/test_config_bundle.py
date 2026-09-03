"""Moving an estate to a machine that has never seen this project, and losing nothing on the way.

The sentence these tests defend is the one in :mod:`db_ops.lib.config_bundle`: after
``pip install dbabrain`` and ``db-ops import-data <bundle>``, the new machine runs the same estate
as the old one. Three things have to hold for that to be true rather than nearly true, and each
one is a group below:

* **The right files travel.** Derived from ``data/config_catalog.json``, so a config file added
  next month crosses with no edit here — and one the catalog does not name does not cross, which
  is what keeps generated output and test fixtures out.
* **What arrives is what left.** Every entry carries a checksum, and the whole bundle is verified
  before a single byte is written. A truncated transfer must leave the target untouched, not
  half-applied — a half-applied estate is the one outcome worse than a failed transfer.
* **A bundle is untrusted input.** It arrived from another machine. A path inside it is data, and
  the tests for ``..``, absolute paths and drive letters are there because extracting a received
  archive to wherever it asks is a defect with a name.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from db_ops.lib import config_bundle
from db_ops.lib.json_io import dump_json_text
from db_ops.lib.config_bundle import (
    BUNDLE_FORMAT,
    ROLE_CONFIG_CATALOG,
    ROLE_CONFIG_SOURCE,
    ROLE_ESTATE_ASSET,
    ROLE_SECRET_STORE,
    ROLE_TOOL_CONFIG,
    BundleError,
)

from conftest import write_catalogued_data


@pytest.fixture
def source_root(tmp_path: Path) -> Path:
    """A tool root shaped like a real one: config, catalogued data, secrets, assets, and noise.

    The noise is deliberate. ``database-inventory.json`` is generated output and
    ``sre_test_config.json`` is a fixture; both live in ``data/`` beside the config that matters,
    and "copy the data folder" takes them. The catalog is what tells the two apart, so an estate
    without something for the catalog to exclude would not test the rule at all.
    """
    root = tmp_path / "source"
    data = root / "data"
    write_catalogued_data(data)

    (root / "config.json").write_text(json.dumps({
        "app_name": "db_ops",
        "log_dir": "logs",
        "runtime_dir": "runtime",
        "store_config_file": "data/store_config.json",
    }, indent=2) + "\n", encoding="utf-8")

    (data / "encrypted_secret_text.json").write_text(
        json.dumps({"schema_version": 1, "secrets": {"A_REF": "Y2lwaGVydGV4dA=="}}, indent=2),
        encoding="utf-8")

    (data / "database-inventory.json").write_text('{"generated": true}', encoding="utf-8")
    (data / "sre_test_config.json").write_text('{"fixture": true}', encoding="utf-8")

    keys = data / "ssh_keys"
    keys.mkdir()
    (keys / "worker.key").write_text("-----BEGIN PRIVATE KEY-----\nabc\n", encoding="utf-8")

    tasks = root / "assets" / "tasks" / "sqlserver"
    tasks.mkdir(parents=True)
    (tasks / "001_daily.sql").write_bytes(b"SELECT 1;\r\nGO\r\n")
    return root


def _entries(root: Path, **kwargs) -> tuple:
    return config_bundle.read_bundle(config_bundle.build_bundle(root, **kwargs))


def _source_of(entry) -> str:
    """Where an entry was read from, which is not always where it lands.

    Only ``seed_inventory`` has one source and two destinations; everything else is one to one.
    """
    if entry.role == config_bundle.ROLE_SEED_INVENTORY:
        return config_bundle.INVENTORY_SOURCE_RELATIVE
    return entry.path


def _roles(entries) -> dict[str, list[str]]:
    grouped: dict[str, list[str]] = {}
    for entry in entries:
        grouped.setdefault(entry.role, []).append(entry.path)
    return grouped


# -- what travels ---------------------------------------------------------------------------- #

def test_every_catalogued_file_travels_and_nothing_else_from_data(source_root: Path) -> None:
    grouped = _roles(_entries(source_root))
    catalogued = config_bundle.catalogued_files(source_root / "data")

    assert grouped[ROLE_CONFIG_SOURCE] == [f"data/{name}" for name in catalogued]
    assert grouped[ROLE_TOOL_CONFIG] == ["config.json"]
    assert grouped[ROLE_CONFIG_CATALOG] == ["data/config_catalog.json"]


def test_fixtures_and_samples_do_not_travel(source_root: Path) -> None:
    """The whole reason the catalog is the allow-list rather than a directory listing."""
    paths = {entry.path for entry in _entries(source_root)}

    assert "data/sre_test_config.json" not in paths


def test_the_inventory_travels_to_both_paths(source_root: Path) -> None:
    """It was excluded as "generated output" and that was wrong — twice over.

    `inventory-workflow` *reads* `runtime/reports/database-inventory.json`, merges a health
    overlay into it and writes it back; its `sqlserver_resources` and `deployment` blocks are
    hand-authored. A machine without one fails every ten minutes with `No such file or
    directory`, forever — measured on a clean install running the real estate for an afternoon,
    and found by nothing else.

    Both paths, because that is what `control.deploy` has always done for a worker: `data/` is
    the master's authoritative copy and `runtime/reports/` is where the reports app opens it.
    """
    grouped = _roles(_entries(source_root))

    assert sorted(grouped[config_bundle.ROLE_SEED_INVENTORY]) == [
        "data/database-inventory.json",
        "runtime/reports/database-inventory.json",
    ]


def test_the_two_inventory_copies_are_the_same_document(source_root: Path) -> None:
    """One source, two destinations. A bundle carrying two *different* inventories would put the
    master's copy and the copy its reports are rendered from permanently out of step."""
    seeds = [e for e in _entries(source_root) if e.role == config_bundle.ROLE_SEED_INVENTORY]

    assert len({entry.sha256 for entry in seeds}) == 1


def test_an_estate_with_no_inventory_yet_is_named_not_fatal(source_root: Path) -> None:
    (source_root / "data" / "database-inventory.json").unlink()

    bundle = config_bundle.build_bundle(source_root)

    assert "data/database-inventory.json" in bundle["missing_at_source"]
    assert not any(e["role"] == config_bundle.ROLE_SEED_INVENTORY
                   for e in bundle["files"].values())


def test_the_catalog_itself_travels(source_root: Path) -> None:
    """It is not listed among its own ``config_sources``, so walking the catalog cannot find it.

    Without it the new machine keeps the shipped example, which names a different set of files —
    and then ``sync-config`` mirrors a different estate than the one that was exported.
    """
    paths = {entry.path for entry in _entries(source_root)}

    assert "data/config_catalog.json" in paths


def test_assets_and_ssh_keys_travel_because_config_names_them_by_path(source_root: Path) -> None:
    grouped = _roles(_entries(source_root))

    assert sorted(grouped[ROLE_ESTATE_ASSET]) == [
        "assets/tasks/sqlserver/001_daily.sql",
        "data/ssh_keys/worker.key",
    ]


def test_a_catalogued_file_that_does_not_exist_is_named_not_fatal(source_root: Path) -> None:
    """The catalog is an allow-list, not a requirements list.

    An estate that runs no Oracle has no ``docker_db_connections.json``, and refusing to export
    until every catalogued file exists would make a normal estate unexportable. The names travel
    so the person importing sees what was absent at the source rather than meeting it later as a
    missing-file error.
    """
    (source_root / "data" / "reports_config.json").unlink()

    bundle = config_bundle.build_bundle(source_root)

    assert "data/reports_config.json" in bundle["missing_at_source"]
    assert "data/reports_config.json" not in bundle["files"]


def test_no_secrets_leaves_the_store_out_and_says_so(source_root: Path) -> None:
    bundle = config_bundle.build_bundle(source_root, include_secrets=False)

    assert bundle["includes_secret_store"] is False
    assert config_bundle.SECRET_STORE_RELATIVE not in bundle["files"]


def test_the_secret_store_travels_as_ciphertext_and_the_passphrase_does_not(
    source_root: Path,
) -> None:
    """A bundle carries the store so a new machine inherits the estate's credentials.

    There is no field for the passphrase and no code path that reads one: the importing machine
    supplies it through ``DB_OPS_SECRET_KEY``. Asserted as an absence because that is what the
    guarantee is.
    """
    bundle = config_bundle.build_bundle(source_root)
    text = config_bundle.bundle_text(bundle).lower()

    assert bundle["files"][config_bundle.SECRET_STORE_RELATIVE]["role"] == ROLE_SECRET_STORE
    assert "db_ops_secret_key" in text  # named in the notes, as the thing to set
    assert "passphrase" not in json.dumps(
        bundle["files"][config_bundle.SECRET_STORE_RELATIVE]).lower()


def test_a_tool_root_with_no_catalog_is_refused_by_name(tmp_path: Path) -> None:
    (tmp_path / "data").mkdir()

    with pytest.raises(BundleError, match="No config catalog"):
        config_bundle.build_bundle(tmp_path)


# -- what arrives is what left --------------------------------------------------------------- #

def test_round_trip_reproduces_every_non_json_file_byte_for_byte(
    source_root: Path, tmp_path: Path
) -> None:
    """SQL and keys arrive byte-identical, newlines included.

    ``\\r\\n`` in the SQL fixture is not incidental: a text file round-tripped through a
    line-ending-normalising read comes back different, and a SQL script that changed on the way is
    a script the new machine runs differently. A key that changed does not authenticate at all.
    """
    target = tmp_path / "target"
    entries = _entries(source_root)

    config_bundle.apply_bundle(entries, target)

    for entry in entries:
        if entry.kind == config_bundle.CONTENT_JSON:
            continue
        assert (target / entry.path).read_bytes() == (source_root / _source_of(entry)).read_bytes(), (
            f"{entry.path} did not survive the round trip")


def test_round_trip_reproduces_every_json_file_byte_for_byte(
    source_root: Path, tmp_path: Path
) -> None:
    """A config file arrives byte-identical, in the layout the source machine wrote it in.

    The trade the format makes is that a JSON entry keeps the *parsed document* rather than the
    source text, so a bundle stays readable and two estates can be diffed by a person. Byte
    fidelity is then bought back by `layout`, and it has to be: measured on the real tool root,
    all 26 catalogued files are CRLF and two-space indented, so re-emitting the canonical form
    would have put 26 whole-file diffs in the next commit anybody made.
    """
    target = tmp_path / "target"
    entries = _entries(source_root)

    config_bundle.apply_bundle(entries, target)

    for entry in entries:
        if entry.kind != config_bundle.CONTENT_JSON:
            continue
        assert (target / entry.path).read_bytes() == (source_root / _source_of(entry)).read_bytes(), (
            f"{entry.path} did not survive the round trip")


@pytest.mark.parametrize("written", [
    b'{\r\n  "a": 1\r\n}\r\n',            # CRLF, two-space - what this estate actually holds
    b'{\n    "a": 1\n}\n',                # the canonical form dump_json_text writes
    b'{\n\t"a": 1\n}',                    # a tab indent, and no trailing newline
    b'{"a": 1}',                          # one line, no indent at all
    '{\n  "a": "\u00e0"\n}\n'.encode("utf-8"),   # non-ASCII, written as itself
    b'{\n  "a": "\\u00e0"\n}\n',          # the same document, written with ensure_ascii
    '\ufeff{\n  "a": 1\n}\n'.encode("utf-8"),   # a BOM, which an editor on Windows adds
])
def test_a_json_file_arrives_written_the_way_it_left(tmp_path: Path, written: bytes) -> None:
    """Each of these is a way a `data/*.json` in a real estate has been written.

    Parametrised rather than asserted once because the failure is invisible: every one of these
    round-trips to the *same document*, so nothing breaks and nothing is reported — the estate
    just quietly reformats, and whoever commits next sees a diff they did not make.
    """
    root = tmp_path / "source"
    data = root / "data"
    write_catalogued_data(data)
    (data / "db_instances.json").write_bytes(written)
    target = tmp_path / "target"

    entries = _entries(root)
    config_bundle.apply_bundle(entries, target)

    assert (target / "data" / "db_instances.json").read_bytes() == written


def test_a_bundle_from_a_build_before_layouts_still_imports(
    source_root: Path, tmp_path: Path
) -> None:
    """An entry with no `layout` gets the canonical one, which is what that build wrote.

    A missing block is a version difference, not corruption, and refusing it would make every
    bundle written by an earlier build unreadable by a later one — the opposite of what a format
    with a `schema_version` is for.
    """
    bundle = config_bundle.build_bundle(source_root)
    entry = bundle["files"]["data/db_instances.json"]
    entry.pop("layout")
    entry["sha256"] = hashlib.sha256(
        dump_json_text(entry["content"]).encode("utf-8")).hexdigest()

    entries = config_bundle.read_bundle(bundle)

    carried = next(e for e in entries if e.path == "data/db_instances.json")
    assert carried.layout is None
    assert carried.rendered_bytes() == dump_json_text(entry["content"]).encode("utf-8")


@pytest.mark.parametrize("layout", [
    {"indent": 1_000_000},
    {"indent": "four"},
    {"indent": -1},
    {"newline": "</textarea>"},
    {"newline": ""},
    {"trailing_newline": "yes"},
    {"unknown_key": 1},
    "not an object",
])
def test_a_layout_this_build_does_not_understand_is_refused(
    source_root: Path, layout: object
) -> None:
    """`layout` arrives from another machine, so it is checked like every other field.

    An indent of a million renders a four-line config file into gigabytes; a `newline` of
    arbitrary text rewrites the document's own separators into whatever it says. Neither is a
    thing a JSON file can have been written with, so neither is accepted as a description of one.
    """
    bundle = config_bundle.build_bundle(source_root)
    bundle["files"]["data/db_instances.json"]["layout"] = layout

    with pytest.raises(BundleError, match="layout"):
        config_bundle.read_bundle(bundle)


def test_a_non_utf8_file_crosses_as_base64(source_root: Path, tmp_path: Path) -> None:
    payload = b"\x00\x01\xff\xfe binary key material"
    (source_root / "data" / "ssh_keys" / "binary.key").write_bytes(payload)
    target = tmp_path / "target"

    entries = _entries(source_root)
    config_bundle.apply_bundle(entries, target)

    carried = next(e for e in entries if e.path.endswith("binary.key"))
    assert carried.kind == config_bundle.CONTENT_BASE64
    assert (target / "data" / "ssh_keys" / "binary.key").read_bytes() == payload


def test_a_tampered_entry_is_refused_and_nothing_is_written(
    source_root: Path, tmp_path: Path
) -> None:
    """Verification happens before any write, and this is the test that says so.

    Half-applying a bundle leaves a tree that is neither the old estate nor the new one. So the
    assertion is not only that ``read_bundle`` raises — it is that the target is still empty.
    """
    bundle = config_bundle.build_bundle(source_root)
    bundle["files"]["data/db_instances.json"]["content"] = {"db_instances": [{"tampered": True}]}
    target = tmp_path / "target"
    target.mkdir()

    with pytest.raises(BundleError, match="checksum mismatch"):
        config_bundle.read_bundle(bundle)

    assert list(target.iterdir()) == []


def test_a_document_that_is_not_a_bundle_is_refused_by_name() -> None:
    with pytest.raises(BundleError, match="Not a configuration bundle"):
        config_bundle.read_bundle({"schema_version": 1, "files": {}})


def test_a_newer_schema_version_is_refused_rather_than_partly_read(source_root: Path) -> None:
    """A newer bundle may carry a role this build does not know how to place.

    Placing it wrongly is worse than declining, and declining names the fix: upgrade the tool.
    """
    bundle = config_bundle.build_bundle(source_root)
    bundle["schema_version"] = config_bundle.SCHEMA_VERSION + 1

    with pytest.raises(BundleError, match="Upgrade the tool"):
        config_bundle.read_bundle(bundle)


def test_an_unknown_role_is_refused(source_root: Path) -> None:
    bundle = config_bundle.build_bundle(source_root)
    bundle["files"]["config.json"]["role"] = "anything_at_all"

    with pytest.raises(BundleError, match="unknown role"):
        config_bundle.read_bundle(bundle)


# -- a bundle is untrusted input ------------------------------------------------------------- #

@pytest.mark.parametrize("path", [
    "../outside.json",
    "data/../../outside.json",
    "/etc/passwd",
    "//server/share/x.json",
    "C:/Windows/System32/drivers/etc/hosts",
    "C:\\Windows\\hosts",
    "",
])
def test_a_path_that_leaves_the_tool_root_is_refused(source_root: Path, path: str) -> None:
    """Writing wherever a received file asks to be written is the archive-extraction defect.

    Each form here is one a real archive has used to escape: a relative climb, a climb hidden
    behind a directory, a POSIX absolute, a UNC root and a Windows drive letter in both slash
    styles.
    """
    bundle = config_bundle.build_bundle(source_root)
    entry = bundle["files"].pop("config.json")
    bundle["files"][path] = entry

    with pytest.raises(BundleError):
        config_bundle.read_bundle(bundle)


# -- importing into a machine that is already somebody's install ----------------------------- #

def test_an_existing_different_file_stops_the_whole_import(
    source_root: Path, tmp_path: Path
) -> None:
    """The target may already be a working install, and its config may be the only copy.

    The refusal is whole-bundle rather than per-file for the same reason verification is:
    skipping the conflicts and writing the rest produces a mixture of two estates.
    """
    target = tmp_path / "target"
    (target / "data").mkdir(parents=True)
    (target / "data" / "db_instances.json").write_text('{"db_instances": []}', encoding="utf-8")

    with pytest.raises(BundleError, match="already exist here with different content"):
        config_bundle.apply_bundle(_entries(source_root), target)

    assert not (target / "config.json").exists()


def test_force_replaces_and_reports_what_it_replaced(source_root: Path, tmp_path: Path) -> None:
    target = tmp_path / "target"
    (target / "data").mkdir(parents=True)
    (target / "data" / "db_instances.json").write_text('{"db_instances": []}', encoding="utf-8")

    result = config_bundle.apply_bundle(_entries(source_root), target, force=True)

    assert result.overwritten == ("data/db_instances.json",)
    assert json.loads((target / "data" / "db_instances.json").read_text(encoding="utf-8")) == (
        json.loads((source_root / "data" / "db_instances.json").read_text(encoding="utf-8-sig")))


def test_importing_twice_is_not_a_conflict(source_root: Path, tmp_path: Path) -> None:
    """Re-running an import is how a half-finished one is finished.

    A file that already matches is identical, not a conflict — treating it as one would make the
    second run of an interrupted import require ``--force``, and ``--force`` is the flag that
    also destroys somebody's real config.
    """
    target = tmp_path / "target"
    entries = _entries(source_root)

    first = config_bundle.apply_bundle(entries, target)
    second = config_bundle.apply_bundle(entries, target)

    assert len(first.created) == len(entries)
    assert second.unchanged == tuple(entry.path for entry in entries)
    assert second.created == () and second.overwritten == ()


def test_plan_reports_every_action_without_writing(source_root: Path, tmp_path: Path) -> None:
    target = tmp_path / "target"

    planned = config_bundle.plan_import(_entries(source_root), target)

    assert {item.action for item in planned} == {"create"}
    assert not target.exists()


def test_import_side_flags_can_keep_this_machine_s_own_keys(source_root: Path) -> None:
    """"Give me that estate's config but keep my own keys" should not need editing JSON by hand.

    Whoever imports a bundle is not always whoever exported it, which is why the filter exists on
    both sides rather than only at export.
    """
    entries = _entries(source_root)

    kept = config_bundle.select_entries(entries, include_secrets=False, include_assets=False)

    assert not any(e.role in {ROLE_SECRET_STORE, ROLE_ESTATE_ASSET} for e in kept)
    assert any(e.role == ROLE_CONFIG_SOURCE for e in kept)


# -- the exported bundle round-trips through the file it is written to ----------------------- #

def test_the_written_file_is_the_bundle(source_root: Path, tmp_path: Path) -> None:
    """Exported text parses back into the same entries, checksums intact.

    Written out because the export path serialises through ``dump_json_text`` — the same writer
    ``data/*.json`` uses — and a bundle that only survives in memory is not a bundle.
    """
    path = tmp_path / "estate.json"
    path.write_text(config_bundle.bundle_text(config_bundle.build_bundle(source_root)),
                    encoding="utf-8")

    entries = config_bundle.read_bundle(json.loads(path.read_text(encoding="utf-8")))

    assert {e.path for e in entries} == set(config_bundle.build_bundle(source_root)["files"])
    assert path.read_text(encoding="utf-8").startswith("{\n")


def test_the_format_is_named_in_the_file(source_root: Path) -> None:
    bundle = config_bundle.build_bundle(source_root)

    assert bundle["bundle_format"] == BUNDLE_FORMAT


# -- the two commands, end to end ------------------------------------------------------------ #

def test_export_then_import_stands_a_second_machine_up(
    source_root: Path, tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    """The whole feature, through the entry point a person actually types.

    Exercised at the CLI rather than only through the library because the value the operator is
    promised is a *command*, and every defect this project has shipped in a first-run path was in
    the layer between a working function and the words that call it.
    """
    from db_ops import cli

    bundle = tmp_path / "prod-bundle.json"
    target = tmp_path / "second-machine"

    assert cli.main(["export-data", str(bundle), "--root", str(source_root)]) == 0
    assert cli.main(["import-data", str(bundle), "--root", str(target)]) == 0

    capsys.readouterr()
    for entry in _entries(source_root):
        assert (target / entry.path).exists(), f"{entry.path} did not arrive"


def test_export_refuses_to_clobber_an_existing_bundle(
    source_root: Path, tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    from db_ops import cli

    bundle = tmp_path / "prod-bundle.json"
    bundle.write_text("{}", encoding="utf-8")

    assert cli.main(["export-data", str(bundle), "--root", str(source_root)]) == 2
    assert "--force" in capsys.readouterr().err
    assert bundle.read_text(encoding="utf-8") == "{}"


def test_export_warns_when_the_filename_is_one_git_would_take(
    source_root: Path, tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    """A bundle is an entire estate sitting in the tool root as ordinary-looking JSON.

    The convention is suggested rather than enforced — writing one outside any repository is
    legitimate — but departing from it earns a line, because committing an estate is silent and
    cannot be undone once pushed.
    """
    from db_ops import cli

    assert cli.main(["export-data", str(tmp_path / "estate.json"),
                     "--root", str(source_root)]) == 0
    assert "WARNING" in capsys.readouterr().out

    assert cli.main(["export-data", str(tmp_path / "prod-bundle.json"),
                     "--root", str(source_root)]) == 0
    assert "WARNING" not in capsys.readouterr().out


def test_plan_changes_nothing_and_names_every_action(
    source_root: Path, tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    from db_ops import cli

    bundle = tmp_path / "prod-bundle.json"
    target = tmp_path / "second-machine"
    cli.main(["export-data", str(bundle), "--root", str(source_root)])
    capsys.readouterr()

    assert cli.main(["import-data", str(bundle), "--root", str(target), "--plan"]) == 0

    printed = capsys.readouterr().out
    assert "create" in printed and "data/db_instances.json" in printed
    assert list(target.iterdir()) == []


def test_a_bundle_that_is_not_json_is_a_sentence_not_a_traceback(
    tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    from db_ops import cli

    broken = tmp_path / "prod-bundle.json"
    broken.write_text("{ this is not json", encoding="utf-8")

    assert cli.main(["import-data", str(broken), "--root", str(tmp_path / "t")]) == 1
    assert "not readable as JSON" in capsys.readouterr().err


def test_both_commands_are_reachable_and_named_in_the_usage(capsys: pytest.CaptureFixture) -> None:
    from db_ops import cli

    usage = cli._usage()

    assert "export-data" in usage and "import-data" in usage
    assert cli.main(["export-data", "--help"]) == 0
    assert cli.main(["import-data", "--help"]) == 0


# -- the file no serialiser wrote ------------------------------------------------------------ #

HAND_FORMATTED = (
    '{\r\n'
    '  "schema_version": 1,\r\n'
    '  "db_instances": [\r\n'
    '    {"db_instance_name": "one", "port": 1433},\r\n'
    '    {"db_instance_name": "two", "port": 1434}\r\n'
    '  ]\r\n'
    '}\r\n'
).encode("utf-8")


def test_a_hand_formatted_file_arrives_exactly_as_written(tmp_path: Path) -> None:
    """Records on one line inside an indented array - readable, deliberate, and not any indent.

    This estate's `config_catalog.json` is written this way, and it is the one file of 74 that
    `layout` cannot describe, because `json.dumps` emits no such form at any setting. The bundle
    carries the source text beside the parsed document rather than reformatting somebody's file.
    """
    root = tmp_path / "source"
    write_catalogued_data(root / "data")
    (root / "data" / "db_instances.json").write_bytes(HAND_FORMATTED)
    target = tmp_path / "target"

    entries = _entries(root)
    config_bundle.apply_bundle(entries, target)

    carried = next(e for e in entries if e.path == "data/db_instances.json")
    assert carried.verbatim is not None, "a file no layout describes must carry its source text"
    assert (target / "data" / "db_instances.json").read_bytes() == HAND_FORMATTED


def test_the_readable_half_of_a_verbatim_entry_is_still_the_document(tmp_path: Path) -> None:
    """`content` is what a person reads in a bundle; `verbatim` is what lands on disk.

    A field that is only decorative is one somebody eventually reads and believes, so the two are
    required to be the same document. This is the check that makes the readable half a faithful
    summary rather than a claim.
    """
    root = tmp_path / "source"
    write_catalogued_data(root / "data")
    (root / "data" / "db_instances.json").write_bytes(HAND_FORMATTED)
    bundle = config_bundle.build_bundle(root)
    entry = bundle["files"]["data/db_instances.json"]

    assert entry["content"] == json.loads(HAND_FORMATTED.decode("utf-8"))

    entry["content"] = {"db_instances": [{"db_instance_name": "somewhere else"}]}
    with pytest.raises(BundleError, match="different documents"):
        config_bundle.read_bundle(bundle)


def test_only_a_json_entry_may_carry_a_source_text(source_root: Path) -> None:
    bundle = config_bundle.build_bundle(source_root)
    sql = "assets/tasks/sqlserver/001_daily.sql"
    bundle["files"][sql]["verbatim"] = "SELECT 1;"

    with pytest.raises(BundleError, match="only a 'json' entry"):
        config_bundle.read_bundle(bundle)


# -- the catalog and the manifest answer different questions ----------------------------------- #

def test_a_file_the_manifest_carries_but_the_catalog_does_not_still_travels(
    source_root: Path,
) -> None:
    """The bundle walked the catalog, and the catalog is not the list of what travels.

    `config_catalog.json` decides what enters the runtime *store*; `data_files.json` decides what
    *moves*. Most files are in both, so the difference stayed invisible until one file was in the
    manifest and not the catalog — `ops_status_request.json`, deliberately uncatalogued because it
    holds one saved request rather than records the console could edit.

    A clean install therefore had no copy, and its `APP-CONTROL` failed every thirty seconds with
    `Request file not found` — running the command that had *just* been fixed to read that file.
    The unit tests could not see it, because the two lists agreed about everything they shared.
    """
    (source_root / "data" / "saved_request.json").write_text('{"mode": "auto"}', encoding="utf-8")
    manifest = source_root / "data" / "data_files.json"
    manifest.write_text(json.dumps({"schema_version": 1, "data_files": [
        {"file": name, "app_code": "db", "transfer": "push"}
        for name in sorted(p.name for p in (source_root / "data").glob("*.json"))
    ]}), encoding="utf-8")

    paths = {entry.path for entry in _entries(source_root)}

    assert "data/saved_request.json" in paths, (
        "a manifest entry the catalog does not list must still travel")


def test_a_local_only_file_still_does_not_travel(source_root: Path) -> None:
    """The manifest decides both ways: `transfer: local` keeps a fixture off every other machine."""
    (source_root / "data" / "fixture_only.json").write_text('{"x": 1}', encoding="utf-8")
    (source_root / "data" / "data_files.json").write_text(json.dumps({
        "schema_version": 1,
        "data_files": [{"file": "fixture_only.json", "app_code": "sre", "transfer": "local"}],
    }), encoding="utf-8")

    paths = {entry.path for entry in _entries(source_root)}

    assert "data/fixture_only.json" not in paths


# --------------------------------------------------------------------------- #
# The two ways a first import went wrong on 2026-09-03
# --------------------------------------------------------------------------- #
def _bundle_file(root: Path, destination: Path) -> Path:
    """A real bundle, written by the real writer, so these tests exercise the real reader."""
    destination.write_text(dump_json_text(config_bundle.build_bundle(root)), encoding="utf-8")
    return destination


def test_an_import_into_site_packages_is_refused_rather_than_reported_as_success(
    source_root: Path, tmp_path: Path, monkeypatch, capsys
) -> None:
    """The tool root falls back to the package's own location, which for a pip install is
    site-packages. On 2026-09-03 an import run from a new empty directory unpacked a whole estate
    in there and printed `imported into .../site-packages` - which reads like success, and left a
    configuration the next command could not find.

    Refusing is the whole fix, and the message has to name the directory and both ways out,
    because the operator's next move is to type one of them."""
    from db_ops import cli

    bundle = _bundle_file(source_root, tmp_path / "estate-bundle.json")
    site_packages = tmp_path / "venv" / "lib" / "python3.14" / "site-packages"
    site_packages.mkdir(parents=True)
    monkeypatch.setattr("db_ops.lib.paths.TOOL_ROOT", site_packages)

    code = cli._import_data_command([str(bundle)])
    printed = "".join(capsys.readouterr())

    assert code == 2, f"an import into site-packages must fail, not succeed: {printed}"
    assert "site-packages" in printed
    assert "--root" in printed and "db-ops init" in printed
    assert not (site_packages / "config.json").exists(), "nothing may be written on a refusal"


def test_stating_the_root_still_allows_an_unusual_destination(
    source_root: Path, tmp_path: Path, capsys
) -> None:
    """The guard is about the *fallback*, not about policing where an operator may keep an estate.
    `--root` said it on purpose, so it is honoured."""
    from db_ops import cli

    bundle = _bundle_file(source_root, tmp_path / "estate-bundle.json")
    destination = tmp_path / "lib" / "site-packages"

    code = cli._import_data_command([str(bundle), "--root", str(destination)])

    assert code == 0, "".join(capsys.readouterr())
    assert (destination / "config.json").exists()


def test_an_estate_whose_commands_are_all_worker_says_so_after_importing(
    source_root: Path, tmp_path: Path, capsys
) -> None:
    """A daemon that was not told otherwise is `master`. Import a schedule exported from a worker,
    every command is `worker`, and the daemon starts and runs nothing - correctly, and for a reason
    the operator has not been given. It reaches the log on the first tick; that is too late and in
    the wrong place, so the import says it while they are still typing."""
    from db_ops import cli

    (source_root / "data" / "app_commands.json").write_text(dump_json_text({"app_commands": [
        {"app_command_id": "APP-METRICS", "node_role": "worker", "active": True},
        {"app_command_id": "APP-TELEGRAM", "node_role": "worker", "active": True},
    ]}), encoding="utf-8")
    bundle = _bundle_file(source_root, tmp_path / "estate-bundle.json")

    code = cli._import_data_command([str(bundle), "--root", str(tmp_path / "estate")])
    printed = "".join(capsys.readouterr())

    assert code == 0, printed
    assert "DB_OPS_NODE_ROLE=worker" in printed
    assert "run nothing" in printed


def test_an_estate_that_would_run_here_says_nothing_about_roles(
    source_root: Path, tmp_path: Path, capsys
) -> None:
    """The note is only worth printing when it names a real mismatch. A schedule carrying `all`
    runs on a default process, so saying anything about roles would be noise."""
    from db_ops import cli

    (source_root / "data" / "app_commands.json").write_text(dump_json_text({"app_commands": [
        {"app_command_id": "APP-METRICS", "node_role": "all", "active": True},
    ]}), encoding="utf-8")
    bundle = _bundle_file(source_root, tmp_path / "estate-bundle.json")

    cli._import_data_command([str(bundle), "--root", str(tmp_path / "estate")])

    assert "DB_OPS_NODE_ROLE" not in "".join(capsys.readouterr())
