"""The runtime store's copy of ``data/*.json`` — one row per configured record.

Until now the only way to change what db_ops runs was to edit a file under ``data/`` and deploy
it. That works for an operator at a shell; it does not work for a web UI, which needs to list what
is configured, change one record, and show who changed it — none of which a JSON file on a
worker's disk can answer.

So the config is mirrored into the store, **record by record rather than file by file**. A metric
definition, a Telegram group, a SQL target: each is its own row, keyed by the identity the file
already gives it (``metric_code``, ``group_id``, ``sql_id`` + ``target_no``), declared in
``data/config_catalog.json``. Everything in a file that is *not* a keyed record — scalars, notes,
nested policy objects — is kept as one ``__document__`` row, so nothing in the file is dropped and
the file can be rebuilt from the store.

These tables live in the **same schema as the rest of the store** (``db_ops``), beside
``metric_results``, ``job_runs`` and the Telegram queue. They are prefixed ``config_`` rather than
separated, so one connection, one backup and one migration cover the whole store.

**The shape is fully keyed, three levels deep**, because the web UI navigates exactly that way —
app -> file -> record:

* ``config_sources``      one row per synced file. PK ``config_source_id``, UNIQUE ``source_file``.
* ``config_collections``  one row per keyed array inside a file (plus the ``__document__``
  pseudo-collection). PK ``config_collection_id``, UNIQUE ``(config_source_id, collection)``,
  FK to ``config_sources``.
* ``config_items``        one row per record. PK ``config_item_id``, FK to ``config_collections``,
  and the partial unique below.
* ``config_item_revisions``  the trail. PK ``config_revision_id``, FK to ``config_items``,
  UNIQUE ``(config_item_id, revision)``.

Three rules shape it, and all three come from one requirement: **config is never deleted.**

* ``is_active`` is the only delete. A record removed from the file (or from the UI) is flagged
  ``is_active = 0`` and keeps its row, its json and its history. Nothing here issues a ``DELETE``.
* **One active row per key, enforced by the database.** ``ux_config_items_active`` is a *partial*
  unique index over ``(config_collection_id, item_key) WHERE is_active = 1``. Both engines support
  that, and it is what makes the next rule safe rather than merely intended.
* **A retired key can come back.** Because the uniqueness covers only active rows, re-adding a
  ``metric_code`` that was switched off inserts a **new** row beside the old one rather than
  resurrecting it. The old record stays readable exactly as it was when it was retired — which is
  the whole point of not deleting it.

Every write also appends to ``config_item_revisions``. The current row answers "what is
configured"; the trail answers "what did it say last week, and who changed it" — the question that
made the file-only model painful whenever a schedule was wrong overnight.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from db_ops.config import StoreConfig
from db_ops.db import backend as backend_mod
from db_ops.db.backend import StoreTarget

#: Bumped when these tables or their additive migrations change, so
#: db_ops.db.backend.remote_schema_is_current can skip the DDL when nothing has.
CONFIG_SCHEMA_VERSION = 1

#: The collection and key used for the part of a file that is not a keyed record. A file with no
#: record collections at all (``backup_policy.json``, ``store_config.json``) is entirely this one
#: row; a file with them keeps its leftover top-level keys here, so a rebuild loses nothing.
DOCUMENT_COLLECTION = "__document__"
DOCUMENT_KEY = "__document__"

#: What a write was. Recorded per revision so the trail reads without diffing json blobs.
CHANGE_INSERT = "insert"
CHANGE_UPDATE = "update"
CHANGE_DEACTIVATE = "deactivate"

#: The columns a read returns, with the source/collection each row belongs to joined back on. The
#: UI and the file rebuild both need "which file, which array, which position", and a bare
#: ``SELECT *`` on ``config_items`` answers none of the three now that they are normalised out.
_ITEM_COLUMNS = """
    i.config_item_id, i.config_collection_id, c.collection, c.key_fields_json, c.label_field,
    s.config_source_id, s.source_file, s.app_code, s.display_name AS source_display_name,
    i.item_key, i.item_ord, i.label, i.item_json, i.content_hash, i.metadata_json,
    i.revision, i.is_active, i.created_at, i.updated_at, i.deactivated_at, i.updated_by, i.note
"""


class ConfigStoreError(RuntimeError):
    """A config row could not be written as asked."""


def canonical_json(payload: Any) -> str:
    """The one text form of a config record: compact, and **in the author's key order**.

    Not sorted, and that is the decision. The store is not the final destination of these records
    — :func:`db_ops.db.config_sync.export` writes them back into ``data/*.json``, which people read
    and review as diffs. Sorting here looked harmless and turned every export into a whole-file
    reordering: a one-field edit to ``telegram_users.json`` came back as every key in every record
    moved, which is how a review stops being a review.

    Stability is not lost by dropping the sort, because the store keeps *exactly the text it was
    given*: the sync serialises what it parsed out of the file, so a file nobody touched
    re-serialises byte for byte and :func:`content_hash` agrees. A genuine reorder of the file is
    then a genuine change, recorded as one — which it is, since the file is what ships.
    """
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def content_hash(payload: Any) -> str:
    """SHA-256 of the stored text. Settles "did this change?" without comparing blobs."""
    return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class ConfigItem:
    """One configured record, as it is about to be written.

    ``item_ord`` is the record's position in its collection in the source file. It is carried
    because JSON arrays are ordered and some of them are read in order (``app_commands`` by
    ``app_ord``, the report sections); rebuilding a file from an unordered set of rows would
    reshuffle them.
    """

    source_file: str
    collection: str
    item_key: str
    payload: Any
    item_ord: int = 0
    label: str = ""
    note: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def hash(self) -> str:
        return content_hash(self.payload)


class ConfigStore:
    """Persistence for the config mirror (SQLite or PostgreSQL, per ``data/store_config.json``).

    Constructed like every other store class — from a path, a :class:`StoreTarget`, a
    :class:`StoreConfig`, or :meth:`from_config` — so it runs on whichever backend the node
    declares without the caller naming one.
    """

    def __init__(
        self,
        source: "str | Path | StoreTarget | StoreConfig",
        *,
        key: str | None = None,
        password: str | None = None,
    ) -> None:
        self.target = StoreTarget.coerce(source, key=key, password=password)
        self.sqlite_path = self.target.sqlite_path
        # (source_file, collection) -> config_collection_id. Safe to memo for the life of the
        # store object: collection rows are upserted and never deleted, so an id cannot change.
        self._collection_ids: dict[tuple[str, str], int] = {}

    @classmethod
    def from_config(cls, config, *, key: str | None = None, password: str | None = None) -> "ConfigStore":
        return cls(StoreTarget.from_config(config, key=key, password=password))

    @property
    def backend(self) -> str:
        return self.target.store.backend

    def connect(self):
        return self.target.connect()

    def initialize(self, *, force: bool = False) -> None:
        """Create/upgrade this store's schema.

        ``force`` skips both the in-process memo and the recorded schema version, so an explicit
        ``db_ops.db.cli init`` builds the tables even when everything looks current.
        """
        if not force and backend_mod.schema_is_ready("ConfigStore", self.target):
            return
        if not force and backend_mod.remote_schema_is_current(
                self.target, "ConfigStore", CONFIG_SCHEMA_VERSION):
            backend_mod.mark_schema_ready("ConfigStore", self.target)
            return
        self.target.prepare()
        with self.connect() as conn:
            backend_mod.acquire_schema_lock(conn)
            conn.executescript(SCHEMA_SQL)
            backend_mod.record_schema_version(conn, "ConfigStore", CONFIG_SCHEMA_VERSION)
        backend_mod.mark_schema_ready("ConfigStore", self.target)

    # ------------------------------------------------------------------ #
    # Catalog rows (the two parent levels)
    # ------------------------------------------------------------------ #
    def ensure_source(
        self, *, source_file: str, app_code: str, display_name: str = "",
        description: str = "", source_ord: int = 0,
    ) -> int:
        """Upsert the row for one synced file and return its id.

        Idempotent by ``source_file``, which is the file's identity everywhere else in db_ops
        (the deploy merge, the catalog, the docs all name files, not numbers).
        """
        self.initialize()
        with self.connect() as conn:
            row = conn.execute(
                "SELECT config_source_id FROM config_sources WHERE source_file = ?",
                (str(source_file),),
            ).fetchone()
            if row is None:
                cursor = conn.execute(
                    """
                    INSERT INTO config_sources
                        (source_file, app_code, display_name, description, source_ord,
                         is_active, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, 1, ?, ?)
                    """,
                    (str(source_file), str(app_code), str(display_name), str(description),
                     int(source_ord), utc_now_text(), utc_now_text()),
                )
                return int(cursor.lastrowid)
            source_id = int(row["config_source_id"])
            conn.execute(
                """
                UPDATE config_sources
                SET app_code = ?, display_name = ?, description = ?, source_ord = ?,
                    is_active = 1, updated_at = ?
                WHERE config_source_id = ?
                """,
                (str(app_code), str(display_name), str(description), int(source_ord),
                 utc_now_text(), source_id),
            )
            return source_id

    def ensure_collection(
        self, *, source_file: str, collection: str, key_fields: tuple[str, ...] | list[str] = (),
        label_field: str = "", collection_ord: int = 0,
    ) -> int:
        """Upsert the row for one keyed array inside a file and return its id.

        ``key_fields`` is stored rather than recomputed because it is what a UI needs to build the
        "add a record" form: it says which fields the operator may not leave blank and may not
        collide on.
        """
        self.initialize()
        source_row = self._source_row(source_file)
        if source_row is None:
            raise ConfigStoreError(
                f"No config source registered for '{source_file}'; call ensure_source first.")
        source_id = int(source_row["config_source_id"])
        fields_text = canonical_json([str(item) for item in key_fields])
        with self.connect() as conn:
            row = conn.execute(
                """
                SELECT config_collection_id FROM config_collections
                WHERE config_source_id = ? AND collection = ?
                """,
                (source_id, str(collection)),
            ).fetchone()
            if row is None:
                cursor = conn.execute(
                    """
                    INSERT INTO config_collections
                        (config_source_id, collection, key_fields_json, label_field,
                         collection_ord, is_active, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, 1, ?, ?)
                    """,
                    (source_id, str(collection), fields_text, str(label_field),
                     int(collection_ord), utc_now_text(), utc_now_text()),
                )
                collection_id = int(cursor.lastrowid)
            else:
                collection_id = int(row["config_collection_id"])
                conn.execute(
                    """
                    UPDATE config_collections
                    SET key_fields_json = ?, label_field = ?, collection_ord = ?, is_active = 1,
                        updated_at = ?
                    WHERE config_collection_id = ?
                    """,
                    (fields_text, str(label_field), int(collection_ord), utc_now_text(),
                     collection_id),
                )
        self._collection_ids[(str(source_file), str(collection))] = collection_id
        return collection_id

    def collection_id(self, source_file: str, collection: str) -> int:
        """The id for one collection, from the memo or the store. Raises if it was never made."""
        cached = self._collection_ids.get((str(source_file), str(collection)))
        if cached is not None:
            return cached
        self.initialize()
        with self.connect() as conn:
            row = conn.execute(
                """
                SELECT c.config_collection_id
                FROM config_collections c
                JOIN config_sources s ON s.config_source_id = c.config_source_id
                WHERE s.source_file = ? AND c.collection = ?
                """,
                (str(source_file), str(collection)),
            ).fetchone()
        if row is None:
            raise ConfigStoreError(
                f"Collection '{collection}' of '{source_file}' is not registered; "
                "call ensure_collection first.")
        collection_id = int(row["config_collection_id"])
        self._collection_ids[(str(source_file), str(collection))] = collection_id
        return collection_id

    def deactivate_source(self, *, source_file: str, actor: str = "") -> bool:
        """Retire the file itself, after its records. Returns whether a row was there to retire.

        The row stays, like everything else here: it is the record that this file *was* config,
        which is what an operator asks about when a page they used disappears.
        """
        self.initialize()
        with self.connect() as conn:
            cursor = conn.execute(
                "UPDATE config_sources SET is_active = 0, updated_at = ? WHERE source_file = ? "
                "AND is_active = 1",
                (utc_now_text(), str(source_file)),
            )
            return bool(getattr(cursor, "rowcount", 0))

    def list_sources(self, *, app_code: str | None = None, include_inactive: bool = False) -> list[Any]:
        """The synced files, for the app blocks the web UI draws."""
        self.initialize()
        where = [] if include_inactive else ["is_active = 1"]
        params: list[Any] = []
        if app_code:
            where.append("app_code = ?")
            params.append(str(app_code))
        clause = f"WHERE {' AND '.join(where)}" if where else ""
        with self.connect() as conn:
            return list(conn.execute(
                f"SELECT * FROM config_sources {clause} ORDER BY app_code, source_ord, source_file",
                tuple(params),
            ))

    def list_collections(self, *, source_file: str | None = None,
                         include_inactive: bool = False) -> list[Any]:
        self.initialize()
        where = [] if include_inactive else ["c.is_active = 1"]
        params: list[Any] = []
        if source_file:
            where.append("s.source_file = ?")
            params.append(str(source_file))
        clause = f"WHERE {' AND '.join(where)}" if where else ""
        with self.connect() as conn:
            return list(conn.execute(
                f"""
                SELECT c.*, s.source_file, s.app_code
                FROM config_collections c
                JOIN config_sources s ON s.config_source_id = c.config_source_id
                {clause}
                ORDER BY s.source_file, c.collection_ord, c.collection
                """,
                tuple(params),
            ))

    # ------------------------------------------------------------------ #
    # Item reads
    # ------------------------------------------------------------------ #
    def list_items(
        self,
        *,
        app_code: str | None = None,
        source_file: str | None = None,
        collection: str | None = None,
        include_inactive: bool = False,
    ) -> list[Any]:
        """Config rows in file order.

        Inactive rows are excluded by default: "what is configured" is the question almost every
        caller has, and a retired record answering it would put a switched-off metric back on a
        dashboard. Pass ``include_inactive`` for the history view.
        """
        self.initialize()
        where = [] if include_inactive else ["i.is_active = 1"]
        params: list[Any] = []
        for column, value in (("s.app_code", app_code), ("s.source_file", source_file),
                              ("c.collection", collection)):
            if value:
                where.append(f"{column} = ?")
                params.append(str(value))
        clause = f"WHERE {' AND '.join(where)}" if where else ""
        with self.connect() as conn:
            return list(conn.execute(
                f"""
                SELECT {_ITEM_COLUMNS}
                FROM config_items i
                JOIN config_collections c ON c.config_collection_id = i.config_collection_id
                JOIN config_sources s ON s.config_source_id = c.config_source_id
                {clause}
                ORDER BY s.app_code, s.source_file, c.collection_ord, c.collection,
                         i.item_ord, i.item_key, i.config_item_id
                """,
                tuple(params),
            ))

    def get_item(self, *, source_file: str, collection: str, item_key: str) -> Any | None:
        """The **active** row for one key, or ``None``. Retired rows are reached by id."""
        self.initialize()
        with self.connect() as conn:
            return conn.execute(
                f"""
                SELECT {_ITEM_COLUMNS}
                FROM config_items i
                JOIN config_collections c ON c.config_collection_id = i.config_collection_id
                JOIN config_sources s ON s.config_source_id = c.config_source_id
                WHERE s.source_file = ? AND c.collection = ? AND i.item_key = ? AND i.is_active = 1
                """,
                (str(source_file), str(collection), str(item_key)),
            ).fetchone()

    def revisions(self, config_item_id: int) -> list[Any]:
        """Every recorded state of one row, oldest first."""
        self.initialize()
        with self.connect() as conn:
            return list(conn.execute(
                """
                SELECT * FROM config_item_revisions
                WHERE config_item_id = ?
                ORDER BY revision, config_revision_id
                """,
                (int(config_item_id),),
            ))

    # ------------------------------------------------------------------ #
    # Item writes
    # ------------------------------------------------------------------ #
    def upsert_item(self, item: ConfigItem, *, actor: str = "", note: str = "") -> tuple[int, str]:
        """Write one record. Returns ``(config_item_id, action)``.

        ``action`` is ``inserted``, ``updated`` or ``unchanged``. **Unchanged is the common case**
        — a sync of an unedited file must touch no rows at all, or every run would rewrite
        ``updated_at`` on hundreds of records and drown the revision trail in noise. The comparison
        is the content hash plus the two presentation fields (order, label), so a record that
        merely moved within its file is still recorded as a change.
        """
        collection_id = self.collection_id(item.source_file, item.collection)
        payload_text = canonical_json(item.payload)
        digest = content_hash(item.payload)
        metadata_text = canonical_json(item.metadata or {})
        with self.connect() as conn:
            existing = conn.execute(
                """
                SELECT config_item_id, content_hash, revision, item_ord, label
                FROM config_items
                WHERE config_collection_id = ? AND item_key = ? AND is_active = 1
                """,
                (collection_id, str(item.item_key)),
            ).fetchone()

            if existing is None:
                cursor = conn.execute(
                    """
                    INSERT INTO config_items
                        (config_collection_id, item_key, item_ord, label, item_json, content_hash,
                         metadata_json, revision, is_active, created_at, updated_at, updated_by, note)
                    VALUES (?, ?, ?, ?, ?, ?, ?, 1, 1, ?, ?, ?, ?)
                    """,
                    (collection_id, str(item.item_key), int(item.item_ord), str(item.label),
                     payload_text, digest, metadata_text, utc_now_text(), utc_now_text(),
                     str(actor), note or item.note),
                )
                item_id = int(cursor.lastrowid)
                self._record_revision(conn, config_item_id=item_id, revision=1,
                                      payload_text=payload_text, digest=digest, is_active=1,
                                      change_type=CHANGE_INSERT, actor=actor,
                                      note=note or item.note)
                return item_id, "inserted"

            item_id = int(existing["config_item_id"])
            unchanged = (
                str(existing["content_hash"]) == digest
                and int(existing["item_ord"]) == int(item.item_ord)
                and str(existing["label"] or "") == str(item.label or "")
            )
            if unchanged:
                return item_id, "unchanged"

            revision = int(existing["revision"]) + 1
            conn.execute(
                """
                UPDATE config_items
                SET item_ord = ?, label = ?, item_json = ?, content_hash = ?, metadata_json = ?,
                    revision = ?, updated_at = ?, updated_by = ?, note = ?
                WHERE config_item_id = ?
                """,
                (int(item.item_ord), str(item.label), payload_text, digest, metadata_text,
                 revision, utc_now_text(), str(actor), note or item.note, item_id),
            )
            self._record_revision(conn, config_item_id=item_id, revision=revision,
                                  payload_text=payload_text, digest=digest, is_active=1,
                                  change_type=CHANGE_UPDATE, actor=actor, note=note or item.note)
            return item_id, "updated"

    def deactivate_item(
        self, *, source_file: str, collection: str, item_key: str,
        actor: str = "", note: str = "",
    ) -> int | None:
        """Retire the active row for one key. Returns its id, or ``None`` if there was none.

        This is the delete. The row keeps its json, its revision number and its history; only
        ``is_active`` moves — which is also what frees the key for a new record later.
        """
        collection_id = self.collection_id(source_file, collection)
        with self.connect() as conn:
            existing = conn.execute(
                """
                SELECT config_item_id, item_json, content_hash, revision
                FROM config_items
                WHERE config_collection_id = ? AND item_key = ? AND is_active = 1
                """,
                (collection_id, str(item_key)),
            ).fetchone()
            if existing is None:
                return None
            item_id = int(existing["config_item_id"])
            revision = int(existing["revision"]) + 1
            conn.execute(
                """
                UPDATE config_items
                SET is_active = 0, revision = ?, deactivated_at = ?, updated_at = ?,
                    updated_by = ?, note = ?
                WHERE config_item_id = ?
                """,
                (revision, utc_now_text(), utc_now_text(), str(actor), str(note), item_id),
            )
            self._record_revision(conn, config_item_id=item_id, revision=revision,
                                  payload_text=str(existing["item_json"]),
                                  digest=str(existing["content_hash"]), is_active=0,
                                  change_type=CHANGE_DEACTIVATE, actor=actor, note=note)
            return item_id

    @staticmethod
    def _record_revision(conn, *, config_item_id: int, revision: int, payload_text: str,
                         digest: str, is_active: int, change_type: str, actor: str,
                         note: str) -> None:
        conn.execute(
            """
            INSERT INTO config_item_revisions
                (config_item_id, revision, item_json, content_hash, is_active, change_type,
                 changed_at, changed_by, note)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (int(config_item_id), int(revision), payload_text, digest, int(is_active),
             str(change_type), utc_now_text(), str(actor), str(note)),
        )

    def _source_row(self, source_file: str) -> Any | None:
        with self.connect() as conn:
            return conn.execute(
                "SELECT * FROM config_sources WHERE source_file = ?", (str(source_file),)
            ).fetchone()


def utc_now_text() -> str:
    """The store's timestamp format — the same ISO-8601 UTC text the SQL DEFAULTs emit."""
    from db_ops.db.store import utc_now_text as _utc_now_text

    return _utc_now_text()


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS config_sources
(
    config_source_id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_file TEXT NOT NULL UNIQUE,
    app_code TEXT NOT NULL,
    display_name TEXT NOT NULL DEFAULT '',
    description TEXT NOT NULL DEFAULT '',
    source_ord INTEGER NOT NULL DEFAULT 0,
    is_active INTEGER NOT NULL DEFAULT 1 CHECK (is_active IN (0, 1)),
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
);

CREATE INDEX IF NOT EXISTS ix_config_sources_app ON config_sources (app_code, is_active);

CREATE TABLE IF NOT EXISTS config_collections
(
    config_collection_id INTEGER PRIMARY KEY AUTOINCREMENT,
    config_source_id INTEGER NOT NULL,
    collection TEXT NOT NULL,
    key_fields_json TEXT NOT NULL DEFAULT '[]' CHECK (json_valid(key_fields_json)),
    label_field TEXT NOT NULL DEFAULT '',
    collection_ord INTEGER NOT NULL DEFAULT 0,
    is_active INTEGER NOT NULL DEFAULT 1 CHECK (is_active IN (0, 1)),
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
    CONSTRAINT uq_config_collections UNIQUE (config_source_id, collection),
    CONSTRAINT fk_config_collections_source FOREIGN KEY (config_source_id)
        REFERENCES config_sources (config_source_id)
);

CREATE TABLE IF NOT EXISTS config_items
(
    config_item_id INTEGER PRIMARY KEY AUTOINCREMENT,
    config_collection_id INTEGER NOT NULL,
    item_key TEXT NOT NULL,
    item_ord INTEGER NOT NULL DEFAULT 0,
    label TEXT NOT NULL DEFAULT '',
    item_json TEXT NOT NULL CHECK (json_valid(item_json)),
    content_hash TEXT NOT NULL,
    metadata_json TEXT NOT NULL DEFAULT '{}' CHECK (json_valid(metadata_json)),
    revision INTEGER NOT NULL DEFAULT 1,
    is_active INTEGER NOT NULL DEFAULT 1 CHECK (is_active IN (0, 1)),
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
    deactivated_at TEXT NULL,
    updated_by TEXT NOT NULL DEFAULT '',
    note TEXT NOT NULL DEFAULT '',
    CONSTRAINT fk_config_items_collection FOREIGN KEY (config_collection_id)
        REFERENCES config_collections (config_collection_id)
);

-- Partial, and that is the whole design: uniqueness applies only while a row is active, so
-- retiring a record frees its key for a new one without ever deleting the old row.
CREATE UNIQUE INDEX IF NOT EXISTS ux_config_items_active
    ON config_items (config_collection_id, item_key) WHERE is_active = 1;

CREATE INDEX IF NOT EXISTS ix_config_items_collection
    ON config_items (config_collection_id, is_active, item_ord);
CREATE INDEX IF NOT EXISTS ix_config_items_key ON config_items (item_key);

CREATE TABLE IF NOT EXISTS config_item_revisions
(
    config_revision_id INTEGER PRIMARY KEY AUTOINCREMENT,
    config_item_id INTEGER NOT NULL,
    revision INTEGER NOT NULL,
    item_json TEXT NOT NULL CHECK (json_valid(item_json)),
    content_hash TEXT NOT NULL,
    is_active INTEGER NOT NULL DEFAULT 1 CHECK (is_active IN (0, 1)),
    change_type TEXT NOT NULL,
    changed_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
    changed_by TEXT NOT NULL DEFAULT '',
    note TEXT NOT NULL DEFAULT '',
    CONSTRAINT uq_config_item_revisions UNIQUE (config_item_id, revision),
    CONSTRAINT fk_config_item_revisions_item FOREIGN KEY (config_item_id)
        REFERENCES config_items (config_item_id)
);
"""
