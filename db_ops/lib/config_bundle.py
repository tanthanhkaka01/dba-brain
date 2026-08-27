"""What a portable configuration bundle *is* — one JSON file that carries an estate.

The problem it solves is stated as a sentence, and the sentence is the acceptance test: on a
machine that has never seen this project, ``pip install dbabrain`` then ``db-ops import-data
<bundle>`` must leave the tool running **identically** to the machine the bundle came from.

Until this module existed the only way to move an estate was to copy directories by hand, and
that is wrong for three separate reasons rather than merely tedious:

* **The set of files is not obvious.** ``data/`` holds generated output, examples and test
  fixtures alongside the config that actually matters. Somebody copying by eye takes
  ``sre_test_config.json`` (a test fixture) and misses ``store_config.json`` (four lines, and
  nothing starts without it).
* **Nothing checks what arrived.** A truncated copy, a file saved as CP-1252, a half-finished
  transfer — each produces a tree that looks complete and fails later, somewhere else.
* **The secret store travels differently from everything else.** It is ciphertext, so it *can*
  cross, and the passphrase must not.

So the bundle is derived, never enumerated by hand: ``data/config_catalog.json`` is already the
allow-list that decides which files are configuration at all (``db_ops.db.config_sync`` reads the
same file to decide what may enter the store), and this module walks it. A config file added
tomorrow and catalogued lands in the bundle with no edit here — and one that is *not* catalogued
does not travel, which is the same rule the runtime store enforces, stated once.

What crosses, and why each is in rather than out:

===================  ==========================================================================
role                 what it is
===================  ==========================================================================
``tool_config``      ``config.json`` — log/runtime dirs, store pointer, master/worker nodes.
                     Every path inside it is *relative to the tool root* by design, so a copied
                     root stays self-consistent and nothing has to be rewritten on arrival.
``config_catalog``   ``data/config_catalog.json`` itself. It is not listed among its own
                     ``config_sources``, so walking the catalog cannot pick it up — and without
                     it the new machine has only the shipped example, which names a different
                     set of files.
``config_source``    The catalogued ``data/*.json``. The estate proper.
``secret_store``     ``data/encrypted_secret_text.json`` — **ciphertext**. The passphrase is
                     never in the bundle and there is no field for it to hide in.
``estate_asset``     ``assets/**`` and ``data/ssh_keys/**`` — the SQL the task runner executes
                     and the keys it connects with. Config names these files by path, so config
                     without them is config that points at nothing.
``seed_inventory``   ``database-inventory.json``, at both the paths below. Read-then-merged, not
                     generated; see the paragraph under this table.
===================  ==========================================================================

**``database-inventory.json`` is a seed, and the first version of this module got that wrong.**
It was excluded as "generated output, rebuilt on the new machine" — and that sentence is false.
``inventory-workflow`` *reads* it, merges a health overlay into it and writes it back
(``db_ops.reports.inventory_summary``), and its static blocks — ``sqlserver_resources``,
``deployment`` — are hand-authored. Without it a fresh install fails every ten minutes with
``No such file or directory: runtime/reports/database-inventory.json``, forever. Measured on a
clean install running the real estate for an afternoon; nothing else found it.

It travels to **both** paths, which is what ``control.deploy`` has always done for the worker:
``data/database-inventory.json`` is the master's authoritative copy, and
``runtime/reports/database-inventory.json`` is where the reports app reads it once a config is
present.

Two rules the format exists to enforce:

1. **Every entry carries a checksum, and import verifies all of them before writing anything.**
   A bundle is a file that arrived from somewhere else. Half-applying one leaves a tree that is
   neither the old estate nor the new one, which is the worst of the three outcomes.
2. **A path in a bundle is data, not an instruction.** :func:`_safe_relative` refuses absolutes,
   drive letters, roots and any ``..``. Writing wherever a received file asks to be written is
   the archive-extraction defect, and a config bundle is an archive.
"""

from __future__ import annotations

import base64
import codecs
import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any

from db_ops.lib.json_io import dump_json_text
from db_ops.lib.paths import CONFIG_CATALOG_FILENAME


#: Names this file format, so a JSON document that is *not* a bundle is refused by name rather
#: than by the first key that happens to be missing.
BUNDLE_FORMAT = "dbabrain-config-bundle"

#: Bumped when the layout changes in a way an older reader cannot honour. An importer refuses a
#: version above its own — a newer bundle may carry a role this build does not know how to place,
#: and placing it wrongly is worse than declining.
SCHEMA_VERSION = 1

#: The tool root file that says where the tool keeps its own state.
TOOL_CONFIG_FILENAME = "config.json"

#: Ciphertext. Carried so that a new machine inherits the estate's credentials; useless without
#: the passphrase, which travels in a person's head or a password manager and never here.
SECRET_STORE_RELATIVE = "data/encrypted_secret_text.json"

#: The master's authoritative inventory, and the copy the reports app actually opens. Both are
#: written from the first, exactly as ``control.deploy`` builds a worker bundle: the runtime copy
#: is what ``inventory-workflow`` merges health into, and on a machine that has never run one
#: there is nothing there to merge into.
INVENTORY_SOURCE_RELATIVE = "data/database-inventory.json"
INVENTORY_SEED_RELATIVES: tuple[str, ...] = (
    "data/database-inventory.json",
    "runtime/reports/database-inventory.json",
)

#: Directories whose whole contents are estate data referenced by config, by path, from a config
#: file. ``assets/`` is the operator's own tree — the package's shipped SQL lives with the
#: component that owns it (see ``db_ops.lib.paths``) and is installed by pip, so it must not be
#: duplicated into a bundle.
ESTATE_ASSET_DIRS: tuple[str, ...] = ("assets", "data/ssh_keys")

#: Files under an estate asset directory that are noise on any machine.
_ASSET_SKIP_NAMES = frozenset({".DS_Store", "Thumbs.db"})

ROLE_TOOL_CONFIG = "tool_config"
ROLE_CONFIG_CATALOG = "config_catalog"
ROLE_CONFIG_SOURCE = "config_source"
ROLE_SECRET_STORE = "secret_store"
ROLE_ESTATE_ASSET = "estate_asset"
ROLE_SEED_INVENTORY = "seed_inventory"

#: Every role this build can place. An entry with any other role is refused rather than written
#: to its stated path: the role is what decides whether ``--no-secrets`` applies to it.
KNOWN_ROLES: frozenset[str] = frozenset({
    ROLE_TOOL_CONFIG, ROLE_CONFIG_CATALOG, ROLE_CONFIG_SOURCE, ROLE_SECRET_STORE,
    ROLE_ESTATE_ASSET, ROLE_SEED_INVENTORY,
})

CONTENT_JSON = "json"
CONTENT_TEXT = "text"
CONTENT_BASE64 = "base64"


class BundleError(ValueError):
    """A bundle cannot be built, read, or applied as written."""


@dataclass(frozen=True)
class BundleEntry:
    """One file inside a bundle.

    ``kind`` decides how ``content`` is read back, and the three are not interchangeable:

    * ``json`` keeps the parsed document, so a bundle stays readable and a diff between two
      estates is a diff a person can review — and ``layout`` carries how the source file was
      written, so the file that lands is byte-identical to the one that left. Keeping only the
      document was the first design and it was wrong: measured against this estate, every one of
      its 26 config files is CRLF and two-space indented, so "round-tripped faithfully" would
      have meant 26 whole-file diffs the first time anybody committed after an import.
    * ``text`` keeps the file's characters exactly, newlines included, and its checksum covers
      the UTF-8 bytes. SQL and PEM keys go here: they are readable and they must not be
      reformatted.
    * ``base64`` is the fallback for bytes that are not UTF-8. It exists so that one binary file
      cannot make a whole bundle unbuildable.
    """

    path: str
    role: str
    kind: str
    content: Any
    sha256: str
    layout: dict[str, Any] | None = None
    verbatim: str | None = None

    def to_json(self) -> dict[str, Any]:
        entry: dict[str, Any] = {
            "role": self.role,
            "kind": self.kind,
            "sha256": self.sha256,
        }
        if self.layout is not None:
            entry["layout"] = self.layout
        entry["content"] = self.content
        if self.verbatim is not None:
            entry["verbatim"] = self.verbatim
        return entry

    def rendered_bytes(self) -> bytes:
        """The bytes this entry becomes on disk."""
        if self.kind == CONTENT_JSON:
            if not isinstance(self.content, dict):
                raise BundleError(f"{self.path}: a 'json' entry's content must be an object.")
            if self.verbatim is not None:
                return self.verbatim.encode("utf-8")
            return _render_json(self.content, self.layout)
        if self.kind == CONTENT_TEXT:
            if not isinstance(self.content, str):
                raise BundleError(f"{self.path}: a 'text' entry's content must be a string.")
            return self.content.encode("utf-8")
        if self.kind == CONTENT_BASE64:
            try:
                return base64.b64decode(str(self.content), validate=True)
            except Exception as exc:  # noqa: BLE001 - any decode failure is the same answer
                raise BundleError(f"{self.path}: base64 content is not decodable ({exc}).") from exc
        raise BundleError(f"{self.path}: unknown content kind {self.kind!r}.")


@dataclass(frozen=True)
class PlannedWrite:
    """What importing one entry would do to the tree it is being imported into."""

    path: str
    role: str
    action: str  # "create" | "overwrite" | "identical"
    size: int


@dataclass(frozen=True)
class ImportResult:
    created: tuple[str, ...]
    overwritten: tuple[str, ...]
    unchanged: tuple[str, ...]


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


#: What ``dump_json_text`` produces, and what a JSON entry is written as when it carries no
#: ``layout`` — a bundle hand-written by somebody, or one from a build before layouts existed.
DEFAULT_LAYOUT: dict[str, Any] = {
    "indent": 4,
    "newline": "\n",
    "trailing_newline": True,
    "bom": False,
    "ascii_only": False,
}

#: The opening brace and the first indented key, which is where a JSON file states its indent.
#: Tabs as well as spaces, because an editor writes what it is configured to write.
_FIRST_INDENT = re.compile(r"^\{\n([ \t]+)\S")

#: The widest indent this build will honour from a received bundle. An indent is a description of
#: how a file was written, and no file was written with a million-space one; without a ceiling
#: that number is a way to turn a four-line config into gigabytes on the importing machine.
MAX_INDENT = 16


def _is_ascii(value: bytes | str) -> bool:
    return value.isascii()


def _detect_layout(raw: bytes, text: str, document: dict[str, Any]) -> dict[str, Any]:
    """How this JSON file was written, in the ways that change its bytes and not its meaning.

    Detected rather than assumed, because ``data/*.json`` in a real tool root is not written by
    one hand: some files came from ``dump_json_text``, some from an editor, some from an app that
    called ``json.dump`` with its own indent. Measured on this estate, all 26 catalogued files are
    CRLF and two-space indented — so a bundle that re-emitted the canonical form would land 26
    whole-file diffs in the next commit anybody made, with no change of meaning in any of them.

    The bundle's job is to move an estate, not to reformat one.
    """
    newline = "\r\n" if "\r\n" in text else "\n"
    body = text.replace("\r\n", "\n")
    match = _FIRST_INDENT.search(body)
    if match:
        # A number of spaces where that is what it is, and the literal string otherwise, because
        # ``json.dumps`` accepts either and a tab is not "one space".
        indent: int | str | None = (len(match.group(1)) if match.group(1) == " " * len(match.group(1))
                                    else match.group(1))
    else:
        # No indented first key at all: either the document is written on one line, or it is
        # written with `indent=0`, which still puts each key on its own line. ``None`` is the
        # compact form and is not the same value as ``0``.
        indent = 0 if body.lstrip().startswith("{\n") else None
    return {
        "indent": indent,
        "newline": newline,
        "trailing_newline": body.endswith("\n"),
        "bom": raw.startswith(codecs.BOM_UTF8),
        # A file that is pure ASCII on disk while its document holds non-ASCII characters was
        # written with ``ensure_ascii=True``. Re-emitting it unescaped is the same document and
        # still valid JSON — and still a diff on every line carrying a Vietnamese service name.
        "ascii_only": _is_ascii(raw) and not _is_ascii(json.dumps(document, ensure_ascii=False)),
    }


def _is_valid_indent(value: Any) -> bool:
    """Whether *value* describes an indent some editor or serialiser actually produces.

    ``None`` is the compact one-line form, an integer is that many spaces, and a string is the
    literal run — a tab, most often. Everything else, including ``True`` (which Python would
    otherwise take for ``1``), is a description of nothing.
    """
    if value is None:
        return True
    if isinstance(value, bool):
        return False
    if isinstance(value, int):
        return 0 <= value <= MAX_INDENT
    return (isinstance(value, str) and 0 < len(value) <= MAX_INDENT
            and all(character in " \t" for character in value))


def _validated_layout(relative: str, raw: Any) -> dict[str, Any] | None:
    """A ``layout`` block from a received bundle, or ``None`` when it carries none.

    Checked rather than trusted for the same reason the paths are: this is a file from another
    machine. An indent of a million would render a config file into memory exhaustion, and a
    newline of anything at all would rewrite the document's own separators — so both are held to
    what a JSON file can actually have been written with. An absent block is not an error: a
    bundle from an older build has none and gets :data:`DEFAULT_LAYOUT`, which is what that build
    wrote.
    """
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise BundleError(f"{relative}: 'layout' must be an object.")
    unknown = sorted(set(raw) - set(DEFAULT_LAYOUT))
    if unknown:
        raise BundleError(f"{relative}: 'layout' has key(s) this build does not know: {unknown}.")
    indent = raw.get("indent", DEFAULT_LAYOUT["indent"])
    if not _is_valid_indent(indent):
        raise BundleError(
            f"{relative}: 'layout.indent' must be null (one line), a whole number of spaces "
            f"0-{MAX_INDENT}, or a short run of spaces and tabs.")
    newline = raw.get("newline", DEFAULT_LAYOUT["newline"])
    if newline not in {"\n", "\r\n"}:
        raise BundleError(f"{relative}: 'layout.newline' must be a line ending, not {newline!r}.")
    for flag in ("trailing_newline", "bom", "ascii_only"):
        if flag in raw and not isinstance(raw[flag], bool):
            raise BundleError(f"{relative}: 'layout.{flag}' must be true or false.")
    return {**DEFAULT_LAYOUT, **raw}


def _render_json(document: dict[str, Any], layout: dict[str, Any] | None) -> bytes:
    """The document as bytes, written the way *layout* says the source file was written."""
    settings = {**DEFAULT_LAYOUT, **(layout or {})}
    text = json.dumps(document, ensure_ascii=bool(settings["ascii_only"]),
                      indent=settings["indent"])
    if settings["trailing_newline"]:
        text += "\n"
    newline = str(settings["newline"])
    if newline != "\n":
        # Only separators are real newlines at this point — a newline inside a string value is
        # escaped by ``json.dumps`` as two characters — so this cannot reach into the data.
        text = text.replace("\n", newline)
    payload = text.encode("utf-8")
    return codecs.BOM_UTF8 + payload if settings["bom"] else payload


def _read_entry(path: Path, relative: str, role: str) -> BundleEntry:
    """Read one file into an entry, choosing the kind that loses the least."""
    raw = path.read_bytes()
    if relative.endswith(".json"):
        try:
            document = json.loads(raw.decode("utf-8-sig"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise BundleError(f"{relative} is not readable as JSON: {exc}") from exc
        if not isinstance(document, dict):
            raise BundleError(f"{relative}: a config document's root must be an object.")
        layout = _detect_layout(raw, raw.decode("utf-8-sig"), document)
        rendered = _render_json(document, layout)
        if rendered == raw:
            return BundleEntry(relative, role, CONTENT_JSON, document, _sha256(rendered), layout)
        # No serialiser wrote this file, so no description of one reproduces it. In this estate
        # that is `config_catalog.json`, which is hand-formatted with each collection on a single
        # line — readable, deliberate, and not something `json.dumps` emits at any indent. Rather
        # than reformat somebody's file, carry the source text beside the parsed document: the
        # document is what makes the bundle readable, and `verbatim` is what actually lands.
        return BundleEntry(relative, role, CONTENT_JSON, document, _sha256(raw),
                           verbatim=raw.decode("utf-8"))
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return BundleEntry(relative, role, CONTENT_BASE64,
                           base64.b64encode(raw).decode("ascii"), _sha256(raw))
    return BundleEntry(relative, role, CONTENT_TEXT, text, _sha256(raw))


def catalogued_files(data_dir: Path) -> tuple[str, ...]:
    """The ``data/*.json`` names the catalog declares, in the order it declares them.

    Read here rather than through ``db_ops.db.config_sync.load_catalog`` on purpose: that
    function belongs to the ``db`` app and validates a catalog against what the *store* can hold
    — key fields, collisions, document collections. A bundle does not need any of that. It needs
    the list of filenames, and asking a heavier module for it would make exporting an estate
    depend on the store being loadable, which on a machine being rebuilt it may not be.
    """
    path = Path(data_dir) / CONFIG_CATALOG_FILENAME
    if not path.is_file():
        raise BundleError(
            f"No config catalog at {path}. Without it there is no allow-list, and a bundle "
            "assembled by guessing which files are config is exactly what this format replaces.")
    try:
        document = json.loads(path.read_bytes().decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise BundleError(f"{CONFIG_CATALOG_FILENAME} is not readable as JSON: {exc}") from exc
    entries = document.get("config_sources")
    if not isinstance(entries, list):
        raise BundleError(f"{CONFIG_CATALOG_FILENAME} must hold a 'config_sources' array.")
    names: list[str] = []
    for entry in entries:
        if not isinstance(entry, dict):
            raise BundleError(f"{CONFIG_CATALOG_FILENAME}: every config_sources entry must be an object.")
        name = str(entry.get("file") or "").strip()
        if not name:
            raise BundleError(f"{CONFIG_CATALOG_FILENAME}: an entry has no 'file'.")
        if name not in names:
            names.append(name)
    return tuple(names)


def travelling_data_files(data_dir: Path) -> tuple[str, ...]:
    """Which ``data/*.json`` a bundle carries — the **manifest**, not the catalog.

    These are two different questions and this module asked the wrong one for a day.
    ``config_catalog.json`` decides what enters the *runtime store*; ``data_files.json`` decides
    what *travels*. Most files are in both, so the difference is invisible until a file is in one
    and not the other — and ``ops_status_request.json`` is exactly that. It is deliberately not
    catalogued (it holds one saved request, not a collection of records, so there is nothing for
    the console to list or edit), and it is in the manifest, so ``deploy`` and
    ``worker-pull-data-config`` carry it while this bundle did not.

    The result was a clean install whose ``APP-CONTROL`` failed every thirty seconds with
    ``Request file not found: data/ops_status_request.json`` — the same command that had just
    been fixed to read that file instead of inline JSON. Measured on a running install; the
    unit tests could not see it because both lists were right about everything they shared.

    The manifest's own rule is the one that applies: *a file that is not in the manifest does not
    travel, in either direction.* The three files with a role of their own are excluded here and
    added by the caller — the catalog, the secret store, and the inventory seed.
    """
    from db_ops.lib.data_files import TRANSFER_LOCAL, load_manifest

    handled_separately = {
        CONFIG_CATALOG_FILENAME,
        Path(SECRET_STORE_RELATIVE).name,
        Path(INVENTORY_SOURCE_RELATIVE).name,
    }
    return tuple(
        item.file for item in load_manifest(data_dir)
        if item.transfer != TRANSFER_LOCAL and item.file not in handled_separately
    )


def _asset_files(tool_root: Path) -> tuple[tuple[Path, str], ...]:
    """Every estate asset, as (absolute path, tool-root-relative posix path)."""
    found: list[tuple[Path, str]] = []
    for directory in ESTATE_ASSET_DIRS:
        base = tool_root / directory
        if not base.is_dir():
            continue
        for path in sorted(base.rglob("*")):
            if not path.is_file() or path.name in _ASSET_SKIP_NAMES:
                continue
            if "__pycache__" in path.parts:
                continue
            found.append((path, path.relative_to(tool_root).as_posix()))
    return tuple(found)


def build_bundle(
    tool_root: Path,
    *,
    data_dir: Path | None = None,
    include_secrets: bool = True,
    include_assets: bool = True,
    tool_version: str = "",
) -> dict[str, Any]:
    """Assemble the bundle document for the estate rooted at *tool_root*.

    A catalogued file that does not exist is **skipped and named** in ``missing_at_source``, not
    invented and not fatal: an estate that runs no Oracle has no reason to hold
    ``docker_db_connections.json``, and refusing to export until every catalogued file exists
    would make the catalog a requirements list instead of an allow-list. The names travel inside
    the bundle so that the person importing it can see what was absent at the source rather than
    discovering it as a missing-file error weeks later.
    """
    tool_root = Path(tool_root).expanduser().resolve()
    data = Path(data_dir).expanduser().resolve() if data_dir is not None else tool_root / "data"

    entries: list[BundleEntry] = []
    missing: list[str] = []

    config_path = tool_root / TOOL_CONFIG_FILENAME
    if config_path.is_file():
        entries.append(_read_entry(config_path, TOOL_CONFIG_FILENAME, ROLE_TOOL_CONFIG))
    else:
        missing.append(TOOL_CONFIG_FILENAME)

    # The catalog is read for its *contents* first and carried second, so that a tool root
    # without one is refused by the sentence in `catalogued_files` rather than by a
    # FileNotFoundError on the same path. The order looks arbitrary and is not.
    # The catalog is read for its *contents* first and carried second, so that a tool root
    # without one is refused by the sentence in `catalogued_files` rather than by a
    # FileNotFoundError on the same path. The bundle no longer takes its file list from it — see
    # `travelling_data_files` — but it still ships it, and a tree missing it is still broken.
    catalogued_files(data)
    names = travelling_data_files(data)
    catalog_relative = f"data/{CONFIG_CATALOG_FILENAME}"
    entries.append(_read_entry(data / CONFIG_CATALOG_FILENAME, catalog_relative, ROLE_CONFIG_CATALOG))

    for name in names:
        source = data / name
        relative = f"data/{name}"
        if source.is_file():
            entries.append(_read_entry(source, relative, ROLE_CONFIG_SOURCE))
        else:
            missing.append(relative)

    if include_secrets:
        secret_path = tool_root / SECRET_STORE_RELATIVE
        if secret_path.is_file():
            entries.append(_read_entry(secret_path, SECRET_STORE_RELATIVE, ROLE_SECRET_STORE))
        else:
            missing.append(SECRET_STORE_RELATIVE)

    if include_assets:
        for path, relative in _asset_files(tool_root):
            entries.append(_read_entry(path, relative, ROLE_ESTATE_ASSET))

    # One source, two destinations — the same thing `control.deploy` does when it builds a worker
    # bundle. Written as two entries rather than one entry plus a mirroring step so that the
    # conflict rule in `apply_bundle` covers both: on a machine that has already merged health
    # into its runtime copy, that copy is a real file and must not be silently replaced.
    inventory = tool_root / INVENTORY_SOURCE_RELATIVE
    if inventory.is_file():
        for relative in INVENTORY_SEED_RELATIVES:
            entries.append(_read_entry(inventory, relative, ROLE_SEED_INVENTORY))
    else:
        missing.append(INVENTORY_SOURCE_RELATIVE)

    return {
        "bundle_format": BUNDLE_FORMAT,
        "schema_version": SCHEMA_VERSION,
        "exported_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "exported_by_version": tool_version,
        "includes_secret_store": any(item.role == ROLE_SECRET_STORE for item in entries),
        "notes": [
            "One estate's configuration, as one file. Import it with `db-ops import-data`.",
            "The secret store here is CIPHERTEXT. The passphrase is not in this file and cannot "
            "be recovered from it: set DB_OPS_SECRET_KEY on the importing machine.",
            "This file describes a real estate - hosts, accounts, chat ids, ciphertext. Treat it "
            "as a credential: never commit it, never attach it to an issue.",
            "database-inventory.json travels to BOTH data/ and runtime/reports/ - the reports "
            "app reads the second and merges health into it, so a machine without one fails "
            "inventory-workflow every cycle. Test fixtures and samples do not travel.",
            "Every entry carries a sha256 of the bytes it becomes on disk; import verifies all "
            "of them before it writes any of them.",
        ],
        "missing_at_source": missing,
        "files": {item.path: item.to_json() for item in entries},
    }


def bundle_text(bundle: dict[str, Any]) -> str:
    """The bundle as the text written to disk — the same writer ``data/*.json`` uses."""
    return dump_json_text(bundle)


def _safe_relative(raw: str) -> str:
    """*raw* as a tool-root-relative posix path, or raise.

    A bundle arrives from another machine, so its paths are untrusted input. Absolute paths,
    drive letters, UNC roots and ``..`` are each refused by name: extracting a received archive
    to wherever it asks is how an import turns into a write to ``~/.ssh/authorized_keys``.
    """
    text = raw.strip().replace("\\", "/")
    if not text:
        raise BundleError("A bundle entry has an empty path.")
    if len(text) > 1 and text[1] == ":":
        raise BundleError(f"Bundle entry path must be relative to the tool root: {raw!r}.")
    pure = PurePosixPath(text)
    if pure.is_absolute() or text.startswith("//"):
        raise BundleError(f"Bundle entry path must be relative to the tool root: {raw!r}.")
    if any(part == ".." for part in pure.parts):
        raise BundleError(f"Bundle entry path may not contain '..': {raw!r}.")
    if not pure.name:
        raise BundleError(f"Bundle entry path names no file: {raw!r}.")
    return pure.as_posix()


def _validated_verbatim(relative: str, kind: str, raw_entry: dict[str, Any]) -> str | None:
    """The source text of a JSON entry that no layout describes, checked against its document.

    ``verbatim`` is what lands on disk when it is present, so the readable ``content`` beside it
    would otherwise be decorative — and a decorative field is one somebody eventually reads and
    believes. The two are required to be the same document, which makes the readable half a
    faithful summary of the written half rather than a claim about it.
    """
    if "verbatim" not in raw_entry:
        return None
    verbatim = raw_entry["verbatim"]
    if kind != CONTENT_JSON:
        raise BundleError(f"{relative}: only a 'json' entry may carry 'verbatim'.")
    if not isinstance(verbatim, str):
        raise BundleError(f"{relative}: 'verbatim' must be the source file's text.")
    try:
        written = json.loads(verbatim.lstrip("﻿"))
    except json.JSONDecodeError as exc:
        raise BundleError(f"{relative}: 'verbatim' is not readable as JSON: {exc}") from exc
    if written != raw_entry.get("content"):
        raise BundleError(
            f"{relative}: 'verbatim' and 'content' are different documents. The text that would "
            "be written is not the one the bundle shows; nothing has been written.")
    return verbatim


def read_bundle(document: Any) -> tuple[BundleEntry, ...]:
    """Validate a bundle document and return its entries.

    Everything checkable is checked **here**, before a caller can start writing: the format name,
    the schema version, every path, every role, every kind and every checksum. That ordering is
    the whole safety property — see the module docstring, rule 1.
    """
    if not isinstance(document, dict):
        raise BundleError("A bundle's root must be a JSON object.")
    fmt = document.get("bundle_format")
    if fmt != BUNDLE_FORMAT:
        raise BundleError(
            f"Not a configuration bundle: 'bundle_format' is {fmt!r}, expected {BUNDLE_FORMAT!r}.")
    version = document.get("schema_version")
    if not isinstance(version, int) or isinstance(version, bool):
        raise BundleError("'schema_version' must be an integer.")
    if version > SCHEMA_VERSION:
        raise BundleError(
            f"This bundle is schema_version {version}; this build reads up to {SCHEMA_VERSION}. "
            "Upgrade the tool rather than importing part of it.")
    files = document.get("files")
    if not isinstance(files, dict) or not files:
        raise BundleError("'files' must be a non-empty object.")

    entries: list[BundleEntry] = []
    seen: set[str] = set()
    for raw_path, raw_entry in files.items():
        relative = _safe_relative(str(raw_path))
        if relative in seen:
            raise BundleError(f"{relative}: listed twice in one bundle.")
        seen.add(relative)
        if not isinstance(raw_entry, dict):
            raise BundleError(f"{relative}: every entry must be an object.")
        role = str(raw_entry.get("role") or "")
        if role not in KNOWN_ROLES:
            raise BundleError(
                f"{relative}: unknown role {role!r}. This build places "
                f"{', '.join(sorted(KNOWN_ROLES))}.")
        kind = str(raw_entry.get("kind") or "")
        if kind not in {CONTENT_JSON, CONTENT_TEXT, CONTENT_BASE64}:
            raise BundleError(f"{relative}: unknown content kind {kind!r}.")
        checksum = str(raw_entry.get("sha256") or "")
        if len(checksum) != 64:
            raise BundleError(f"{relative}: 'sha256' is missing or not a sha256 digest.")
        entry = BundleEntry(relative, role, kind, raw_entry.get("content"), checksum,
                            _validated_layout(relative, raw_entry.get("layout")),
                            _validated_verbatim(relative, kind, raw_entry))
        actual = _sha256(entry.rendered_bytes())
        if actual != checksum:
            raise BundleError(
                f"{relative}: checksum mismatch - the bundle says {checksum[:12]} and its own "
                f"content hashes to {actual[:12]}. The file was altered or truncated in "
                "transit; nothing has been written.")
        entries.append(entry)
    return tuple(entries)


def select_entries(
    entries: tuple[BundleEntry, ...],
    *,
    include_secrets: bool = True,
    include_assets: bool = True,
) -> tuple[BundleEntry, ...]:
    """The subset of *entries* an import with these flags would place.

    Filtering on the way **in** as well as on the way out is deliberate: whoever imports a bundle
    is not always whoever exported it, and "give me this estate's config but keep my own keys" is
    a real request that should not require editing a JSON file by hand.
    """
    kept = []
    for entry in entries:
        if entry.role == ROLE_SECRET_STORE and not include_secrets:
            continue
        if entry.role == ROLE_ESTATE_ASSET and not include_assets:
            continue
        kept.append(entry)
    return tuple(kept)


def plan_import(entries: tuple[BundleEntry, ...], tool_root: Path) -> tuple[PlannedWrite, ...]:
    """What applying *entries* to *tool_root* would do, without doing any of it."""
    tool_root = Path(tool_root).expanduser().resolve()
    planned: list[PlannedWrite] = []
    for entry in entries:
        payload = entry.rendered_bytes()
        target = tool_root / entry.path
        if not target.exists():
            action = "create"
        elif target.is_file() and target.read_bytes() == payload:
            action = "identical"
        else:
            action = "overwrite"
        planned.append(PlannedWrite(entry.path, entry.role, action, len(payload)))
    return tuple(planned)


def apply_bundle(
    entries: tuple[BundleEntry, ...],
    tool_root: Path,
    *,
    force: bool = False,
) -> ImportResult:
    """Write *entries* into *tool_root*.

    **An existing file with different content stops the whole import unless ``force``.** The
    files this writes are an estate's configuration, and the machine being imported into may
    already be somebody's working install; silently replacing ``db_instances.json`` there
    destroys the only copy. A file that already matches is not a conflict — re-running an import
    is how a half-finished one is finished.
    """
    tool_root = Path(tool_root).expanduser().resolve()
    planned = plan_import(entries, tool_root)
    conflicts = [item.path for item in planned if item.action == "overwrite"]
    if conflicts and not force:
        listed = "\n  ".join(conflicts[:10])
        more = f"\n  ... and {len(conflicts) - 10} more" if len(conflicts) > 10 else ""
        raise BundleError(
            f"{len(conflicts)} file(s) already exist here with different content:\n  "
            f"{listed}{more}\nNothing was written. Re-run with --force to replace them, or "
            "import into an empty tool root.")

    by_path = {item.path: item for item in planned}
    created: list[str] = []
    overwritten: list[str] = []
    unchanged: list[str] = []
    for entry in entries:
        action = by_path[entry.path].action
        if action == "identical":
            unchanged.append(entry.path)
            continue
        target = tool_root / entry.path
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_name(target.name + ".dbabrain-import")
        temporary.write_bytes(entry.rendered_bytes())
        temporary.replace(target)
        (created if action == "create" else overwritten).append(entry.path)
    return ImportResult(tuple(created), tuple(overwritten), tuple(unchanged))
