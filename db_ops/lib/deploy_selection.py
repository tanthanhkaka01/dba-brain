"""Which files a *partial* deploy ships — the rules, with no transport in them.

A full deploy builds a 700 MB image, copies the whole bundle and restarts the worker container.
That is the right shape for a code change and the wrong one for the change that happens ten times
a day: an edited threshold in ``data/sql_targets.json``, or one new ``.sql`` under
``assets/tasks/``. Both directories are **bind mounts** on the worker
(``docker-compose.runtime.yml``), the scheduler re-reads ``app_commands.json`` on every scan, and
every app command is a fresh process — so a file dropped into the worker's ``data/`` or
``assets/`` is live on its next run with no image and no restart. The rebuild was never buying
anything for those changes; it was only what the one available command happened to do.

What a partial push must not become is "copy whatever the operator typed". The manifest
(:mod:`db_ops.lib.data_files`) already answers *which files travel* and it stays the answer here:
a ``local`` file is master-only and is refused **by name** rather than silently skipped, and a
file nobody listed does not become shippable by being named on a command line.

Two decisions worth stating, because both could have gone the other way:

**A pattern that matches nothing is an error.** The alternative — push the empty set and print
success — is the exact failure ``config_sync`` grew ``unknown_files`` to stop. A typo in a
filename must not read as a completed deploy.

**No directory is pruned.** :func:`db_ops.control.deploy.superseded_dirs` moves aside what a
*bundle* stopped carrying, and it can only do that because a bundle is the whole picture. A
partial push is a statement about the files it names and about nothing else, so it never concludes
that anything on the worker is stale.
"""

from __future__ import annotations

from dataclasses import dataclass
from fnmatch import fnmatch
from pathlib import Path

from db_ops.lib.data_files import DataFileError, load_manifest

#: The type that means "the catalogued ``data/*.json``" rather than a directory. ``data`` is
#: accepted for it too: the directory is called ``data/`` and the operator who types that means
#: this, not a tree walk of everything under it (which would sweep ``logs/`` and the ssh keys).
CONFIG_TYPES: frozenset[str] = frozenset({"config", "data"})

#: Directory trees a partial push may carry, as tool-root-relative posix paths. A ``--type`` is
#: either one of these or something **under** one of them, so ``assets/tasks`` and
#: ``assets/tasks/oracle`` are both addressable while ``logs`` and ``runtime`` are not.
#:
#: The list is the deploy bundle's own: ``assets/`` is the operator's SQL tree, and
#: ``data/ssh_keys`` is the credential material config files point at by path
#: (:data:`db_ops.lib.config_bundle.ESTATE_ASSET_DIRS` says the same thing for a config bundle).
PUSHABLE_DIRS: tuple[str, ...] = ("assets", "data/ssh_keys")

#: Never uploaded, whatever a pattern matches. Build litter and editor droppings.
SKIP_NAMES: frozenset[str] = frozenset({".DS_Store", "Thumbs.db"})
SKIP_DIRS: frozenset[str] = frozenset({"__pycache__"})
SKIP_SUFFIXES: tuple[str, ...] = (".pyc",)

#: ``database-inventory.json`` is read from ``runtime/reports/`` by the reports app and edited in
#: ``data/`` on the master, so a deploy writes it to both. A push of the master's copy that landed
#: in only one of them would leave the worker rendering yesterday's inventory out of the other —
#: the same asymmetry ``control.deploy.build_image`` handles for the bundle.
MIRRORED: dict[str, tuple[str, ...]] = {
    "data/database-inventory.json": ("runtime/reports/database-inventory.json",),
}


class PushSelectionError(ValueError):
    """The ``--type`` / ``--file-name`` pair does not name anything that may be pushed."""


@dataclass(frozen=True)
class PushFile:
    """One file to upload: where it is on the master, and where it goes on the worker.

    ``targets`` is plural because of ``database-inventory.json`` (see :data:`MIRRORED`); for every
    other file it holds exactly the one path.
    """

    local: Path
    targets: tuple[str, ...]

    @property
    def relative(self) -> str:
        """The tool-root-relative path this file was selected as."""
        return self.targets[0]


def normalise_type(value: str) -> str:
    """Read a ``--type`` the way an operator types it, or say what the choices are.

    Backslashes come in from PowerShell (``--type assets\\tasks``) and mean the same thing as the
    posix form the worker uses; a trailing separator likewise. Returning ``"config"`` or a
    normalised posix directory means every caller below compares one spelling.
    """
    raw = str(value or "").strip().replace("\\", "/").strip("/")
    if not raw:
        raise PushSelectionError(_type_help("--type was empty."))
    if raw.lower() in CONFIG_TYPES:
        return "config"
    for allowed in PUSHABLE_DIRS:
        if raw == allowed or raw.startswith(f"{allowed}/"):
            return raw
    raise PushSelectionError(_type_help(f"--type {value!r} is not a directory a push may carry."))


def _type_help(problem: str) -> str:
    return (f"{problem}\n"
            f"  --type config            the catalogued data/*.json\n"
            f"  --type assets            the whole operator SQL tree\n"
            f"  --type assets/tasks      one subtree of it (assets\\tasks also works)\n"
            f"  --type data/ssh_keys     the keys config files point at\n"
            "Anything else - logs/, runtime/, the package itself - needs a full deploy.")


def select_push_files(*, tool_root: str | Path, push_type: str,
                      names: tuple[str, ...] | list[str] = (),
                      data_dir: str | Path | None = None) -> tuple[PushFile, ...]:
    """The files a ``--type``/``--file-name`` pair names, in a stable order.

    ``names`` are matched against each candidate's path *relative to the type* and against its
    bare filename, through :func:`fnmatch.fnmatch` — so ``sql_targets.json``, ``oracle/*.sql`` and
    ``*.sql`` all read the way they look. Every name must match at least one file; see the module
    docstring for why an empty result is refused rather than shipped.
    """
    root = Path(tool_root)
    kind = normalise_type(push_type)
    candidates = (_config_candidates(root, data_dir=data_dir) if kind == "config"
                  else _directory_candidates(root, kind))
    if not candidates:
        raise PushSelectionError(
            f"Nothing to push: {kind} holds no file this master can ship."
            if kind == "config" else
            f"Nothing to push: {root / kind} is empty or does not exist on this master.")

    patterns = [str(name).strip().replace("\\", "/").strip("/") for name in names if str(name).strip()]
    if not patterns:
        chosen = list(candidates)
    else:
        chosen = []
        for pattern in patterns:
            matched = [item for item in candidates if _matches(item[0], pattern)]
            if not matched:
                raise PushSelectionError(_no_match_help(kind, pattern, candidates, root,
                                                        data_dir=data_dir))
            for item in matched:
                if item not in chosen:
                    chosen.append(item)

    return tuple(PushFile(local=local, targets=(relative, *MIRRORED.get(relative, ())))
                 for _key, relative, local in sorted(chosen))


def _matches(key: str, pattern: str) -> bool:
    """Does one candidate answer to this pattern, by its path under the type or by its name?"""
    return fnmatch(key, pattern) or fnmatch(Path(key).name, pattern)


def _config_candidates(root: Path, *, data_dir: str | Path | None) -> list[tuple[str, str, Path]]:
    """The catalogued ``data/*.json`` that exist on this master, as (key, relative, local).

    Read from the manifest, not from a directory listing. A file on disk that nobody listed is not
    configuration — that is the rule ``data_files.py`` exists to state, and the sweep that ignored
    it once put two deleted files back.
    """
    found: list[tuple[str, str, Path]] = []
    for item in load_manifest(data_dir):
        if not item.is_pushed:
            continue
        local = root / "data" / item.file
        if local.is_file():
            found.append((item.file, f"data/{item.file}", local))
    return found


def _directory_candidates(root: Path, relative_dir: str) -> list[tuple[str, str, Path]]:
    """Every shippable file under one pushable directory, as (key, relative, local)."""
    base = root / relative_dir
    if not base.is_dir():
        return []
    found: list[tuple[str, str, Path]] = []
    for local in base.rglob("*"):
        if not local.is_file() or _is_noise(local, base):
            continue
        key = local.relative_to(base).as_posix()
        found.append((key, f"{relative_dir}/{key}", local))
    return found


def _is_noise(local: Path, base: Path) -> bool:
    parts = set(local.relative_to(base).parts[:-1])
    return (local.name in SKIP_NAMES
            or local.suffix in SKIP_SUFFIXES
            or bool(parts & SKIP_DIRS))


def _no_match_help(kind: str, pattern: str, candidates: list[tuple[str, str, Path]],
                   root: Path, *, data_dir: str | Path | None) -> str:
    """Why a name matched nothing — and, when the answer is "on purpose", say so.

    A master-only file is named explicitly. Reporting it as "not found" would send the operator
    looking for a typo in a filename that is spelled correctly and is simply never allowed to
    leave this machine.
    """
    if kind == "config":
        try:
            local_only = sorted(item.file for item in load_manifest(data_dir)
                                if item.transfer == "local")
        except DataFileError:  # pragma: no cover - the manifest already loaded to get here.
            local_only = []
        if any(_matches(name, pattern) for name in local_only):
            return (f"--file-name {pattern!r} names a master-only file "
                    f"({', '.join(local_only)}). Those never travel to a worker: the manifest "
                    "marks them transfer=local. Nothing was pushed.")
        missing = root / "data" / pattern
        if not missing.exists():
            extra = ""
        else:
            extra = (f"\n{missing} exists but is not in data/data_files.json. Add it to the "
                     "manifest first: a file nobody listed does not travel, in either direction.")
        return (f"--file-name {pattern!r} matches no catalogued data file.{extra}\n"
                f"Available: {', '.join(sorted(key for key, _rel, _local in candidates))}")
    sample = sorted(key for key, _rel, _local in candidates)[:12]
    more = "" if len(candidates) <= 12 else f" (+{len(candidates) - 12} more)"
    return (f"--file-name {pattern!r} matches nothing under {kind}.\n"
            f"Present there: {', '.join(sample)}{more}")
