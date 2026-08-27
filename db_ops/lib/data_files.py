"""What is in ``data/``, and how each file moves — the list every transfer consults first.

Four places in this tree used to answer "which files are configuration", and none of them was the
whole answer: ``config_catalog.json`` (what syncs into the store), ``NOT_SYNCED`` in a *test file*
(what deliberately does not), ``REQUIRED_IN_BUNDLE`` in ``control.deploy`` (what a bundle must
carry), and — the one that actually cost something — ``sftp.listdir`` in ``control.worker_data``,
which is not a list at all.

**The defect that made this file exist.** On 2026-08-21 ``metric_groups.json`` and
``notify_levels.json`` were deleted: config nothing read, and `data/README.md` states the rule
they broke. On 2026-08-25 ``worker-pull-data-config --all-json`` put both back, because the sweep
enumerated the *worker's* directory and copied anything the master did not have. Without
``--overwrite`` that is the sweep's only possible effect: it creates files the master does not
have — which is precisely the set somebody just decided to remove. A deletion that the next sweep
undoes is not a deletion, and the guard test caught it four days later.

So the rule is stated once, here, as data: **a file that is not in the manifest does not travel,
in either direction.** A new config file arrives by being added on the master and listed, not by
appearing on a worker.

``transfer`` is written from the master's point of view, because the master is where the decision
is made:

==================  ==========================================================================
value               what it means
==================  ==========================================================================
``push``            the master owns it outright; the worker receives it and never sends it back
``merge``           the worker *appends records*; ``merge_worker_config`` unions before a deploy
``field_merge``     the worker *edits named leaves*; the master's record is the base
``secret_merge``    the encrypted store — merged, never taken wholesale; master is the truth
``pull``            the worker produces it and the master takes it back
``local``           it never leaves this machine (a sample, a test fixture)
==================  ==========================================================================

The merge *rules* — which key identifies a record, which leaves the worker owns — stay in
``control.worker_data`` where the merging happens. What lives here is the decision that a file
merges at all, so the two can be checked against each other rather than drifting; that is
``tests/test_data_files_manifest.py``.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from db_ops.lib.paths import DEFAULT_DATA_DIR


#: The manifest's own name. Spelled once, for the same reason everything else here is.
MANIFEST_FILENAME = "data_files.json"

TRANSFER_PUSH = "push"
TRANSFER_MERGE = "merge"
TRANSFER_FIELD_MERGE = "field_merge"
TRANSFER_SECRET_MERGE = "secret_merge"
TRANSFER_PULL = "pull"
TRANSFER_LOCAL = "local"

#: Every ``transfer`` this build understands. An unknown one is refused rather than guessed at:
#: the guess would be "do not move it", which reads as a working deploy that quietly ships less.
KNOWN_TRANSFERS: frozenset[str] = frozenset({
    TRANSFER_PUSH, TRANSFER_MERGE, TRANSFER_FIELD_MERGE, TRANSFER_SECRET_MERGE,
    TRANSFER_PULL, TRANSFER_LOCAL,
})

#: Transfers whose file the deploy bundle carries to the worker. ``local`` is the only one it does
#: not, and ``pull`` is included because the worker needs a copy to merge its own work into.
_PUSHED = frozenset(KNOWN_TRANSFERS - {TRANSFER_LOCAL})

#: Transfers whose file may come back from the worker. A ``push`` file must not: the master owns
#: it, and letting the worker's copy overwrite the master's is how an edit made on the master
#: disappears at the next sweep.
_PULLED = frozenset({TRANSFER_MERGE, TRANSFER_FIELD_MERGE, TRANSFER_SECRET_MERGE, TRANSFER_PULL})


class DataFileError(ValueError):
    """The manifest cannot be read, or does not describe a transfer that can be honoured."""


@dataclass(frozen=True)
class DataFile:
    """One file in ``data/``, as the manifest declares it."""

    file: str
    app_code: str
    kind: str
    transfer: str
    in_bundle: bool = False
    description: str = ""

    @property
    def is_pushed(self) -> bool:
        """Does the deploy bundle carry it to the worker?"""
        return self.transfer in _PUSHED

    @property
    def is_pulled(self) -> bool:
        """May ``worker-pull-data-config`` bring the worker's copy back?"""
        return self.transfer in _PULLED


#: The copy that ships inside the package. The manifest is **product data** — every name in it is
#: the toolkit's own — so this one is correct for any estate as written, and the operator's copy
#: in ``data/`` overrides it rather than replacing a blank.
#:
#: Added after a clean-room export caught the omission: ``control.deploy`` read the manifest at
#: *import* time, and a public checkout has only ``data_files.example.json``, so importing the
#: module raised before anything ran and took four test files down at collection. That is the
#: `20260822_audit_thin_slice_first_run.md` failure — shipped code reading a file the published
#: copy does not contain — and it is the second time this repository has met it.
PACKAGED_MANIFEST = Path(__file__).resolve().parents[1] / "control" / "catalogue" / MANIFEST_FILENAME


def manifest_path(data_dir: str | Path | None = None) -> Path:
    """Where the operator's copy lives — not necessarily where one exists."""
    return Path(data_dir if data_dir is not None else DEFAULT_DATA_DIR) / MANIFEST_FILENAME


def resolve_manifest(data_dir: str | Path | None = None) -> Path:
    """The manifest to read: the operator's copy first, the packaged one second.

    The same order :func:`db_ops.lib.paths.asset_candidates` uses, and for the same reason — an
    operator who has not customised something should still get a working tool, and one who has
    should win.
    """
    operator = manifest_path(data_dir)
    if operator.is_file():
        return operator
    return PACKAGED_MANIFEST


def load_manifest(data_dir: str | Path | None = None) -> tuple[DataFile, ...]:
    """Parse the manifest. Raises on one that cannot be honoured.

    Refused rather than defaulted, everywhere *except* a missing file, which falls back to the
    packaged copy. A manifest that half-parses would leave a transfer silently moving a subset of
    what it should — the failure this whole file exists to prevent, reintroduced one level up.
    """
    path = resolve_manifest(data_dir)
    if not path.is_file():
        raise DataFileError(
            f"No data-file manifest at {manifest_path(data_dir)}, and none packaged at "
            f"{PACKAGED_MANIFEST}. It is the list every transfer reads first; without it a deploy "
            "or a pull would be back to copying whatever it found.")
    try:
        document: Any = json.loads(path.read_bytes().decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise DataFileError(f"{MANIFEST_FILENAME} is not readable as JSON: {exc}") from exc
    if not isinstance(document, dict):
        raise DataFileError(f"{MANIFEST_FILENAME}: the root must be an object.")
    entries = document.get("data_files")
    if not isinstance(entries, list) or not entries:
        raise DataFileError(f"{MANIFEST_FILENAME} must hold a non-empty 'data_files' array.")

    files: list[DataFile] = []
    seen: set[str] = set()
    for entry in entries:
        if not isinstance(entry, dict):
            raise DataFileError(f"{MANIFEST_FILENAME}: every data_files entry must be an object.")
        name = str(entry.get("file") or "").strip()
        if not name:
            raise DataFileError(f"{MANIFEST_FILENAME}: an entry has no 'file'.")
        if "/" in name or "\\" in name or name != Path(name).name:
            raise DataFileError(
                f"{MANIFEST_FILENAME}: '{name}' must be a bare filename in data/, not a path.")
        if name in seen:
            raise DataFileError(f"{MANIFEST_FILENAME}: '{name}' is listed twice.")
        seen.add(name)
        transfer = str(entry.get("transfer") or "").strip()
        if transfer not in KNOWN_TRANSFERS:
            raise DataFileError(
                f"{MANIFEST_FILENAME}: {name} has transfer {transfer!r}; this build understands "
                f"{', '.join(sorted(KNOWN_TRANSFERS))}.")
        app_code = str(entry.get("app_code") or "").strip()
        if not app_code:
            raise DataFileError(f"{MANIFEST_FILENAME}: {name} has no 'app_code'.")
        in_bundle = entry.get("in_bundle", False)
        if not isinstance(in_bundle, bool):
            raise DataFileError(f"{MANIFEST_FILENAME}: {name}'s 'in_bundle' must be true or false.")
        files.append(DataFile(
            file=name,
            app_code=app_code,
            kind=str(entry.get("kind") or "").strip(),
            transfer=transfer,
            in_bundle=in_bundle,
            description=str(entry.get("description") or ""),
        ))
    return tuple(files)


def known_names(data_dir: str | Path | None = None) -> frozenset[str]:
    """Every filename the manifest lists — the allow-list, in one call."""
    return frozenset(item.file for item in load_manifest(data_dir))


def pushed_names(data_dir: str | Path | None = None) -> tuple[str, ...]:
    """What a deploy bundle carries to the worker, in manifest order."""
    return tuple(item.file for item in load_manifest(data_dir) if item.is_pushed)


def pullable_names(data_dir: str | Path | None = None) -> tuple[str, ...]:
    """What may come back from a worker, in manifest order.

    This is the answer ``worker-pull-data-config --all-json`` needs, and the reason it no longer
    asks the worker's filesystem.
    """
    return tuple(item.file for item in load_manifest(data_dir) if item.is_pulled)


def local_only_names(data_dir: str | Path | None = None) -> frozenset[str]:
    """Files that never leave the master — the bundle leaves these behind."""
    return frozenset(item.file for item in load_manifest(data_dir)
                     if item.transfer == TRANSFER_LOCAL)


def required_in_bundle(data_dir: str | Path | None = None) -> tuple[str, ...]:
    """Files a deploy bundle is checked for, as ``data/<name>`` paths.

    Named in the manifest rather than in ``control.deploy`` so that "this file must reach the
    worker" is stated beside "this file is pushed", where the two can be read together.
    """
    return tuple(f"data/{item.file}" for item in load_manifest(data_dir) if item.in_bundle)
