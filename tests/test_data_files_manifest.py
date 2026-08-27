"""The manifest is the list every transfer reads first, so it has to be true.

A list that decides what moves is worth nothing the moment it stops matching the directory it
describes — and worse than nothing, because a deploy then ships less than it did last time and
says so nowhere. These tests hold three agreements:

* **the manifest and ``data/``** name the same files, in both directions;
* **the manifest and ``config_catalog.json``** agree about which of them are store-synced config;
* **the manifest and ``control.worker_data``** agree about which of them merge, and how.

The third is the one with a history. Four modules used to answer "which files are configuration"
and none of them was the whole answer; the fourth was ``sftp.listdir``, which is not a list at
all. That is the defect in :mod:`db_ops.lib.data_files`'s docstring, and the last test in this
file is the one that would have caught it.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from conftest import shipped_data_dir
from db_ops.db import config_sync
from db_ops.lib import data_files
from db_ops.lib.paths import DEFAULT_DATA_DIR
from db_ops.scaffold import PACKAGED_DEFAULTS
from db_ops.lib.data_files import (
    KNOWN_TRANSFERS,
    TRANSFER_FIELD_MERGE,
    TRANSFER_LOCAL,
    TRANSFER_MERGE,
    TRANSFER_SECRET_MERGE,
    DataFileError,
)

#: The shipped configuration set — the operator's ``data/`` where there is one, the examples
#: renamed otherwise. Read at import for the same reason ``test_config_sync.py`` does it.
DATA_DIR = shipped_data_dir()

MANIFEST = data_files.load_manifest(DATA_DIR)
BY_NAME = {item.file: item for item in MANIFEST}


# -- the manifest and the directory ----------------------------------------------------------- #

def test_every_data_file_on_disk_is_in_the_manifest() -> None:
    """A file nobody listed does not travel — so an unlisted one is a file that silently stays.

    This is the direction that bites on a deploy: somebody adds ``data/thing.json``, the apps read
    it here, and the worker never gets it. The failure is a worker running with a config file the
    master has and it does not, which looks like a code bug for as long as it takes to find.
    """
    on_disk = {path.name for path in DATA_DIR.glob("*.json") if ".example." not in path.name}

    missing = sorted(on_disk - set(BY_NAME))
    assert not missing, (
        f"data/*.json not listed in data/data_files.json: {missing}. Add it with its app_code and "
        "its transfer, or nothing will ever carry it to a worker.")


#: True on a public checkout, where `shipped_data_dir` hands back a temporary copy of the
#: `*.example.json` renamed to the names they are examples of, rather than a real `data/`.
ON_A_PUBLIC_TREE = DATA_DIR != DEFAULT_DATA_DIR

#: Manifest entries that ship **no example**, and correctly so: generated output, the secret
#: store, a worked sample and a test fixture. On a public tree none of them exists, and a test
#: that asserted otherwise would fail on a correct install — which is exactly the defect
#: `20260822_audit_thin_slice_first_run.md` recorded, and this file met it on its first export.
NO_EXAMPLE_SHIPS = frozenset(
    name for name in BY_NAME
    if not (DEFAULT_DATA_DIR / name.replace(".json", ".example.json")).is_file())

#: Files `db-ops init` writes from a copy the package carries. An estate that predates one simply
#: does not have it yet, and that is not a stale entry — the manifest says what *may* travel, and
#: whether a given host has a given file is reported separately (`missing_at_source` in a bundle,
#: `MISSING (not on worker)` in a pull).
INIT_WRITES = frozenset(path.split("/", 1)[1] for path in PACKAGED_DEFAULTS)


def test_the_manifest_lists_no_file_that_is_gone() -> None:
    """The other direction, and the one that let two deleted files come back.

    An entry for a file nobody has is an instruction to go on moving something that was removed on
    purpose — which is how ``metric_groups.json`` and ``notify_levels.json`` returned four days
    after they were deleted.

    On a public tree this narrows to the entries that ship an example, and that is the honest
    scope rather than a hole: "your manifest matches your estate" is a statement about an estate,
    and the published copy has none. What it still catches there is drift in the *shipped* set.
    """
    on_disk = {path.name for path in DATA_DIR.glob("*.json") if ".example." not in path.name}

    listed = set(BY_NAME) - INIT_WRITES - (NO_EXAMPLE_SHIPS if ON_A_PUBLIC_TREE else frozenset())
    gone = sorted(listed - on_disk)
    assert not gone, (
        f"data/data_files.json lists file(s) that no longer exist: {gone}. Delete the entry too — "
        "an entry outliving its file is what makes a deletion undo itself.")


def test_the_entries_with_no_example_are_the_four_that_should_not_have_one() -> None:
    """Pinned, so the exclusion above can never quietly grow.

    An exclusion list that anybody can add to is a way of turning a failing test off. Four of
    these are the only files in `data/` that are not configuration at all: generated output, the
    secret store, a worked sample and a test fixture. The fifth is configuration, and ships as a
    *packaged default* instead of an example — `db-ops init` writes it from the copy inside
    `db_ops.db`, so an example beside it would be a third copy of one small document.
    """
    assert NO_EXAMPLE_SHIPS == {
        "database-inventory.json",
        "encrypted_secret_text.json",
        "postgresql_sla_examples.json",
        "sre_test_config.json",
        "ops_status_request.json",
    }


def test_the_manifest_lists_itself() -> None:
    """It is a file in ``data/`` like any other, and it has to reach the worker.

    Left out, the worker's copy is whatever the last deploy that *did* carry it left behind, and
    the two machines then disagree about what may travel — the one disagreement with no symptom.
    """
    assert data_files.MANIFEST_FILENAME in BY_NAME
    assert BY_NAME[data_files.MANIFEST_FILENAME].is_pushed


def test_every_entry_names_an_app_and_a_transfer_this_build_understands() -> None:
    for item in MANIFEST:
        assert item.app_code, f"{item.file} has no app_code"
        assert item.transfer in KNOWN_TRANSFERS, f"{item.file}: {item.transfer!r}"


# -- the manifest and the catalog -------------------------------------------------------------- #

def test_every_catalogued_file_is_in_the_manifest() -> None:
    """The catalog says what the *store* holds; the manifest says what *moves*.

    A catalogued file missing from the manifest is config the console can edit and no deploy
    carries — the edit lands on the master and the worker keeps running the old value.
    """
    catalogued = {spec.file for spec in config_sync.load_catalog(DATA_DIR)}

    missing = sorted(catalogued - set(BY_NAME))
    assert not missing, f"catalogued but not in the manifest: {missing}"


def test_a_catalogued_file_is_config_and_travels() -> None:
    """Being in the catalog means an app reads it, so it cannot be master-only."""
    catalogued = {spec.file for spec in config_sync.load_catalog(DATA_DIR)}

    stranded = sorted(name for name in catalogued if BY_NAME[name].transfer == TRANSFER_LOCAL)
    assert not stranded, (
        f"catalogued config marked local, so no worker ever gets it: {stranded}")


def test_the_app_that_owns_a_file_is_the_same_in_both() -> None:
    """Two files naming an owner is two places to change it, and one of them gets forgotten."""
    disagreements = []
    for spec in config_sync.load_catalog(DATA_DIR):
        listed = BY_NAME[spec.file]
        if listed.app_code != spec.app_code:
            disagreements.append(f"{spec.file}: catalog says {spec.app_code}, "
                                 f"manifest says {listed.app_code}")
    assert not disagreements, disagreements


# -- the manifest and the code that does the merging ------------------------------------------- #

def test_every_merged_file_is_declared_as_merging() -> None:
    """``worker_data`` holds the merge *rules*; the manifest holds the decision that one exists.

    Split that way on purpose — which key identifies a record belongs beside the code that reads
    it — but the two halves have to describe the same set, or a file merges without the manifest
    knowing and the pull refuses to fetch what the merge needs.
    """
    from db_ops.control import worker_data

    for name, *_ in worker_data.MERGED_ON_DEPLOY:
        assert name in BY_NAME, f"{name} is merged on deploy but is not in the manifest"
        assert BY_NAME[name].transfer == TRANSFER_MERGE, (
            f"{name} is in MERGED_ON_DEPLOY but the manifest calls it "
            f"{BY_NAME[name].transfer!r}")

    for name, *_ in worker_data.FIELD_MERGED_ON_DEPLOY:
        assert name in BY_NAME, f"{name} is field-merged on deploy but is not in the manifest"
        assert BY_NAME[name].transfer == TRANSFER_FIELD_MERGE, (
            f"{name} is in FIELD_MERGED_ON_DEPLOY but the manifest calls it "
            f"{BY_NAME[name].transfer!r}")


def test_nothing_claims_to_merge_without_a_rule_to_merge_it_by() -> None:
    """The reverse: a manifest entry saying ``merge`` that no code merges is a silent no-op.

    Deploy would report nothing merged and overwrite the worker's records at the next bundle —
    the 2026-07-31 failure, arrived at from the other side.
    """
    from db_ops.control import worker_data

    has_a_rule = ({name for name, *_ in worker_data.MERGED_ON_DEPLOY}
                  | {name for name, *_ in worker_data.FIELD_MERGED_ON_DEPLOY})

    claimed = {item.file for item in MANIFEST
               if item.transfer in {TRANSFER_MERGE, TRANSFER_FIELD_MERGE}}
    assert not sorted(claimed - has_a_rule), (
        f"the manifest says these merge, but nothing merges them: {sorted(claimed - has_a_rule)}")


def test_the_secret_store_is_the_only_secret_merge() -> None:
    """It is the one file with its own merge path, and it must not acquire quiet company."""
    from db_ops.control import worker_data

    secret_merged = {item.file for item in MANIFEST
                     if item.transfer == TRANSFER_SECRET_MERGE}

    assert secret_merged == {worker_data.SECRET_STORE_FILENAME}


# -- what the transfers actually ask the manifest ---------------------------------------------- #

def test_a_pushed_file_the_master_owns_never_comes_back() -> None:
    """``push`` means the master decides. Pulling one back is how a master-side edit disappears.

    Without ``--overwrite`` a sweep skips files the master already has, so the only file it can
    take is one the master does not have — and for a ``push`` file that means one the master has
    just deleted.
    """
    pullable = set(data_files.pullable_names(DATA_DIR))

    for item in MANIFEST:
        if item.transfer == "push":
            assert item.file not in pullable, f"{item.file} is master-owned but is pullable"


def test_a_local_file_neither_leaves_nor_arrives() -> None:
    pushed = set(data_files.pushed_names(DATA_DIR))
    pullable = set(data_files.pullable_names(DATA_DIR))

    for name in data_files.local_only_names(DATA_DIR):
        assert name not in pushed and name not in pullable, f"{name} is local but travels"


def test_the_bundle_requirements_come_from_the_manifest() -> None:
    """``control.deploy`` used to write its own list of data files. Now it reads this one."""
    from db_ops.control import deploy

    required = data_files.required_in_bundle(DATA_DIR)

    assert required, "no file is marked in_bundle, so a thin bundle would pass the check"
    for path in required:
        assert path in deploy.required_in_bundle_paths()
        assert BY_NAME[path.removeprefix("data/")].is_pushed


# -- the manifest is read, not trusted ---------------------------------------------------------- #

def test_a_tool_root_without_one_falls_back_to_the_packaged_copy(tmp_path: Path) -> None:
    """The manifest is product data, so an install that has not run `init` yet still works.

    Written after a clean-room export caught the omission: `control.deploy` read the manifest at
    import, a public checkout has only the example, and importing the module raised before
    anything ran. The operator's copy still wins where there is one — the same order
    `paths.asset_candidates` uses.
    """
    assert data_files.PACKAGED_MANIFEST.is_file(), "the package must carry a manifest"

    assert data_files.resolve_manifest(tmp_path) == data_files.PACKAGED_MANIFEST
    assert data_files.load_manifest(tmp_path)


def test_the_packaged_manifest_and_the_estate_one_describe_the_same_files() -> None:
    """Two copies of a list is two lists, unless something says they agree.

    They are allowed to differ in their `notes` — the packaged one explains that it is packaged —
    but not in what they decide, or an install would move a different set from the tree it was
    exported out of.
    """
    packaged = data_files.load_manifest(data_files.PACKAGED_MANIFEST.parent)

    assert {item.file: (item.transfer, item.in_bundle) for item in packaged} == {
        item.file: (item.transfer, item.in_bundle) for item in MANIFEST}


def test_a_missing_manifest_is_refused_by_name(tmp_path: Path, monkeypatch) -> None:
    """With no packaged copy either, there is nothing to fall back to and it says so.

    Refused rather than defaulted: the default anybody reaches for is "move nothing", and that
    reads as a working deploy that quietly ships less.
    """
    monkeypatch.setattr(data_files, "PACKAGED_MANIFEST", tmp_path / "nowhere.json")

    with pytest.raises(DataFileError, match="No data-file manifest"):
        data_files.load_manifest(tmp_path)


@pytest.mark.parametrize("document, expected", [
    ({"data_files": []}, "non-empty"),
    ({"data_files": [{"file": "a.json", "app_code": "db", "transfer": "sideways"}]}, "transfer"),
    ({"data_files": [{"file": "a.json", "transfer": "push"}]}, "app_code"),
    ({"data_files": [{"app_code": "db", "transfer": "push"}]}, "no 'file'"),
    ({"data_files": [{"file": "../escape.json", "app_code": "db", "transfer": "push"}]},
     "bare filename"),
    ({"data_files": [{"file": "a.json", "app_code": "db", "transfer": "push"},
                     {"file": "a.json", "app_code": "db", "transfer": "push"}]}, "twice"),
    ({"data_files": [{"file": "a.json", "app_code": "db", "transfer": "push",
                      "in_bundle": "yes"}]}, "in_bundle"),
], ids=["empty", "unknown-transfer", "no-app", "no-file", "path", "duplicate", "in_bundle"])
def test_a_manifest_that_cannot_be_honoured_is_refused(
    tmp_path: Path, document: dict, expected: str
) -> None:
    """Refused, never defaulted.

    The default anybody would reach for is "do not move it", and that reads as a working deploy
    that quietly ships less — the failure mode this whole file exists to make impossible.
    """
    import json

    (tmp_path / data_files.MANIFEST_FILENAME).write_text(json.dumps(document), encoding="utf-8")

    with pytest.raises(DataFileError, match=expected):
        data_files.load_manifest(tmp_path)


def test_every_file_init_writes_is_in_the_manifest() -> None:
    """`db-ops init` writes twelve files. Every one of them has to be allowed to travel.

    Found by running the daemon from a clean install: `ops_status_request.json` is written by
    `init` and was in no manifest, so on any install that had run `init` it would have reached no
    worker and survived no bundle — the exact failure the manifest exists to prevent, introduced
    by building the manifest from *this estate's* `data/` instead of from what the product can
    produce. An estate that predates a packaged default simply does not have the file yet.
    """
    init_writes = {path.split("/", 1)[1] for path in PACKAGED_DEFAULTS}

    missing = sorted(init_writes - set(BY_NAME))
    assert not missing, (
        f"`db-ops init` writes {missing}, which the manifest does not list, so no deploy, pull or "
        "config bundle would ever carry them.")
