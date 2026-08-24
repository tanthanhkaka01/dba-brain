"""Reproduce one SQL Server schema from instance A on instance B — plan first, then apply.

``table_load`` covers "a spreadsheet into one table". This covers the other half of the same
question and the one that kept being answered outside the tool: *reproduce schema ``X`` from
instance A on instance B*. It exists because that job was done four times by hand — one deployment
script per ticket — and each time it was the same seven phases with a different list of tables,
and each time the thing that went wrong was something ``sys.tables`` scripting does not carry.

**Input is a JSON object**, like every other ``common`` operation::

    {
      "source": {"target": "ACME-192-0-2-111", "database": "APPDB_TEST", "schema": "sched"},
      "dest":   {"target": "ACME-192-0-2-250", "database": "APPDB",      "schema": "sched"},
      "assert_dest_instance": "APP-DB\\\\PROD",     // SERVERPROPERTY('ServerName') must match
      "exclude_tables": ["dataLock", "*Staging"], // shell globs, case-insensitive
      "with_data":      ["config", "config_version", "CalendarDay"],
      "exclude_modules": ["usp_*_Golden*", "usp_build_trace"],
      "phases": ["tables", "indexes", "modules"], // default: all of them, in order
      "plan": true                                // DEFAULT. false = write.
    }

**``plan`` defaults to true**, so a request that forgets to say which it wants gets the harmless
one. A plan reads only the source, prints every statement and count, and writes nothing.

Seven things this does that a ``sys.tables`` loop does not, each because its absence cost a real
deployment:

1. **Partitioning.** Function, scheme and the per-index ``ON scheme(column)`` clause. A copy
   without it creates every index on PRIMARY and reports success — 0 of 32 partitioned indexes
   on the 2026-08-14 hop, discovered weeks later.
2. **Change tracking**, database-level and per table, *before* the modules phase. A procedure
   whose body calls ``CHANGETABLE()`` does not fail at run time when tracking is off; it fails to
   **create**, with Msg 22105, halfway through the deploy.
3. **An application lock** over the whole operation. Two applier processes reached the same
   destination on 2026-08-22. The "already has rows" guard is read-then-write and does not
   prevent that; nothing was corrupted because every catalogue table had a primary key, which is
   luck, not a design.
4. **A destination assertion beyond ``DB_NAME()``**, because the same database name commonly
   exists on several instances of one estate. ``assert_dest_instance`` demands a specific
   ``SERVERPROPERTY('ServerName')`` and aborts otherwise.
5. **Idempotent phases.** Every DDL statement is guarded, modules go out as ``CREATE OR ALTER``,
   and the data phase skips a table that already has rows — so an interrupted run resumes by
   being re-run, with no repair step.
6. **Data pushed from the client.** ``INSERT … SELECT FROM [OtherDb]…`` only works when both
   databases are on one instance. Across instances the rows are read and ``executemany``-d in
   batches, with ``IDENTITY_INSERT`` handled per table.
7. **A report of what it will not carry** (:func:`db_ops.common.schema_catalog.unsupported_features`),
   which is worth as much as the copying: the alternative to a list is finding out one feature at
   a time, each as a failed deployment.

**Phase order is a dependency order, not a preference**: partition objects → tables → change
tracking → indexes and checks → data → modules → foreign keys. Foreign keys last so the load
order cannot violate one; modules after data so a view over a loaded table is verifiable.
"""

from __future__ import annotations

import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Iterator, Mapping, Sequence

from db_ops.common import db_connect, schema_catalog, sql_run, table_load
from db_ops.lib import mssql_ddl, name_filter


class SchemaCopyError(RuntimeError):
    """A user-facing failure: unknown target, wrong destination, a phase that could not run."""


#: The phases, in the order they must run. A request may name a subset; it may not reorder them,
#: because the order *is* the dependency graph and a caller that wants indexes before tables is
#: describing a run that cannot work.
PHASES: tuple[str, ...] = (
    "partitions",
    "change_tracking_database",
    "tables",
    "change_tracking_tables",
    "indexes",
    "checks",
    "data",
    "modules",
    "foreign_keys",
)

#: Rows per ``executemany``. Large enough that 100k rows is not 100k round trips, small enough
#: that a failed batch names a bounded range.
DEFAULT_BATCH_SIZE = 2000

#: A schema copy is minutes of DDL, not seconds of query. The default statement budget reflects
#: that; a single ``CREATE INDEX`` on a large table can outlast the 30s a query gets.
DEFAULT_TIMEOUT_SECONDS = 900

#: How long to wait for the application lock before giving up. Long enough that a second operator
#: starting the same copy queues behind the first rather than being told to try again; short
#: enough that a stuck session is reported inside a coffee break.
DEFAULT_LOCK_TIMEOUT_SECONDS = 300

#: How many times the modules phase re-tries what failed. Views over views, and functions used by
#: other functions, resolve at create time, so a single ordered pass is not enough on every
#: schema. Each pass must reduce the failure count or the loop stops — a genuinely broken module
#: is reported, not retried forever.
DEFAULT_MODULE_PASSES = 4


# --------------------------------------------------------------------------------- the request


@dataclass(frozen=True)
class Endpoint:
    """One side of the copy: which instance, which database, which schema."""

    target: str
    database: str
    schema: str = "dbo"
    credential_name: str = ""

    @classmethod
    def from_json(cls, payload: Any, *, side: str) -> "Endpoint":
        if not isinstance(payload, Mapping):
            raise SchemaCopyError(f'"{side}" must be an object with target/database/schema.')
        target = str(payload.get("target") or "").strip()
        database = str(payload.get("database") or payload.get("database_name") or "").strip()
        if not target:
            raise SchemaCopyError(f'"{side}.target" is required (a server_id, or "<db_type> <ip>").')
        if not database:
            raise SchemaCopyError(f'"{side}.database" is required.')
        return cls(
            target=target,
            database=database,
            schema=str(payload.get("schema") or "dbo").strip() or "dbo",
            credential_name=str(payload.get("credential_name")
                                or payload.get("user_ref") or "").strip(),
        )

    def label(self) -> str:
        return f"{self.target}/{self.database}.{self.schema}"


@dataclass(frozen=True)
class SchemaCopyRequest:
    """The parsed request. Build it with :meth:`from_json`, never field by field."""

    source: Endpoint
    dest: Endpoint
    plan_only: bool = True
    assert_dest_instance: str = ""
    include_tables: tuple[str, ...] = ()
    exclude_tables: tuple[str, ...] = ()
    with_data: tuple[str, ...] = ()
    include_modules: tuple[str, ...] = ()
    exclude_modules: tuple[str, ...] = ()
    phases: tuple[str, ...] = PHASES
    report_unsupported: bool = True
    #: ``"all"`` (default) copies every boundary value; ``"none"`` creates the function with no
    #: boundaries; a list states them literally. The list form exists for the schema whose
    #: boundaries are owned by a procedure at run time — pre-creating them there makes that
    #: procedure throw.
    partition_boundaries: Any = "all"
    #: ``{"FG_ARCHIVE": "PRIMARY"}`` — redirect a filegroup the destination does not have.
    map_filegroups: Mapping[str, str] = field(default_factory=dict)
    create_schema: bool = True
    skip_nonempty_tables: bool = True
    verify: bool = True
    batch_size: int = DEFAULT_BATCH_SIZE
    timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS
    lock_name: str = ""
    lock_timeout_seconds: int = DEFAULT_LOCK_TIMEOUT_SECONDS
    module_passes: int = DEFAULT_MODULE_PASSES
    data_dir: str = ""

    @classmethod
    def from_json(cls, payload: Any) -> "SchemaCopyRequest":
        if isinstance(payload, SchemaCopyRequest):
            return payload
        if not isinstance(payload, Mapping):
            raise SchemaCopyError("Request must be a JSON object.")

        source = Endpoint.from_json(payload.get("source"), side="source")
        dest = Endpoint.from_json(payload.get("dest") or payload.get("destination"), side="dest")
        if (source.target.lower() == dest.target.lower()
                and source.database.lower() == dest.database.lower()
                and source.schema.lower() == dest.schema.lower()):
            raise SchemaCopyError(
                f"source and dest are the same schema ({source.label()}). Name a different "
                "instance, database or schema.")

        phases = _phases(payload.get("phases"))
        return cls(
            source=source,
            dest=dest,
            # Default true, and `mode` accepted as the other spelling because a human types
            # "apply" and a config file carries a flag. Both say the same thing; neither
            # defaults to writing.
            plan_only=_plan_only(payload),
            assert_dest_instance=str(payload.get("assert_dest_instance") or "").strip(),
            include_tables=_patterns(payload.get("include_tables") or payload.get("tables")),
            exclude_tables=_patterns(payload.get("exclude_tables")),
            with_data=_patterns(payload.get("with_data")),
            include_modules=_patterns(payload.get("include_modules") or payload.get("modules")),
            exclude_modules=_patterns(payload.get("exclude_modules")),
            phases=phases,
            report_unsupported=bool(payload.get("report_unsupported", True)),
            partition_boundaries=payload.get("partition_boundaries", "all"),
            map_filegroups={str(key): str(value) for key, value
                            in (payload.get("map_filegroups") or {}).items()},
            create_schema=bool(payload.get("create_schema", True)),
            skip_nonempty_tables=bool(payload.get("skip_nonempty_tables", True)),
            verify=bool(payload.get("verify", True)),
            batch_size=_positive(payload.get("batch_size"), DEFAULT_BATCH_SIZE, "batch_size"),
            timeout_seconds=_positive(payload.get("timeout_seconds"), DEFAULT_TIMEOUT_SECONDS,
                                      "timeout_seconds"),
            lock_name=str(payload.get("lock_name") or "").strip(),
            lock_timeout_seconds=_positive(payload.get("lock_timeout_seconds"),
                                           DEFAULT_LOCK_TIMEOUT_SECONDS, "lock_timeout_seconds"),
            module_passes=_positive(payload.get("module_passes"), DEFAULT_MODULE_PASSES,
                                    "module_passes"),
            data_dir=str(payload.get("data_dir") or "").strip(),
        )

    def resource_name(self) -> str:
        """The application lock's resource string: this destination schema, and nothing else."""
        return self.lock_name or f"db_ops:schema_copy:{self.dest.database}.{self.dest.schema}"


def _plan_only(payload: Mapping[str, Any]) -> bool:
    mode = str(payload.get("mode") or "").strip().lower()
    if mode:
        if mode not in {"plan", "apply", "report"}:
            raise SchemaCopyError(f'mode must be plan, apply or report; got {mode!r}.')
        return mode != "apply"
    return bool(payload.get("plan", True))


def _phases(raw: Any) -> tuple[str, ...]:
    """The phases to run, always in :data:`PHASES` order whatever order they were named in."""
    if raw in (None, "", "all"):
        return PHASES
    if isinstance(raw, str):
        raw = [raw]
    if not isinstance(raw, (list, tuple)):
        raise SchemaCopyError('"phases" must be a list of phase names.')
    wanted = {str(item).strip().lower() for item in raw}
    unknown = sorted(wanted - set(PHASES))
    if unknown:
        raise SchemaCopyError(
            f"unknown phase(s): {', '.join(unknown)}. Known phases, in order: "
            f"{', '.join(PHASES)}.")
    return tuple(phase for phase in PHASES if phase in wanted)


def _patterns(raw: Any) -> tuple[str, ...]:
    if raw in (None, ""):
        return ()
    if isinstance(raw, str):
        return (raw,)
    if not isinstance(raw, (list, tuple)):
        raise SchemaCopyError("table/module lists must be lists of names or glob patterns.")
    return tuple(str(item).strip() for item in raw if str(item).strip())


def _positive(value: Any, default: int, name: str) -> int:
    if value in (None, ""):
        return default
    try:
        number = int(value)
    except (TypeError, ValueError):
        raise SchemaCopyError(f"{name} must be a whole number; got {value!r}.") from None
    if number <= 0:
        raise SchemaCopyError(f"{name} must be greater than zero; got {number}.")
    return number


# ------------------------------------------------------------------------------------ the plan


@dataclass(frozen=True)
class Step:
    """One unit of work: a guarded DDL statement, or a table's worth of rows to push."""

    phase: str
    name: str
    sql: str = ""
    guard: str = ""
    kind: str = "ddl"
    detail: Mapping[str, Any] = field(default_factory=dict)

    def script(self) -> str:
        """The printable form — what a reader approves before an apply."""
        return mssql_ddl.guarded(self.guard, self.sql) if self.sql else ""

    def to_json(self) -> dict[str, Any]:
        return {"phase": self.phase, "name": self.name, "kind": self.kind,
                "sql": self.sql, "guard": self.guard, "detail": dict(self.detail)}


@dataclass
class PlanContext:
    """Everything the phase planners share: the source cursor, the request, the selected names."""

    cursor: Any
    request: SchemaCopyRequest
    tables: list[str] = field(default_factory=list)
    skipped_tables: list[str] = field(default_factory=list)
    modules: list[dict[str, Any]] = field(default_factory=list)
    skipped_modules: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def source_schema(self) -> str:
        return self.request.source.schema

    @property
    def dest_schema(self) -> str:
        return self.request.dest.schema

    def storage(self, storage: Mapping[str, Any] | None) -> dict[str, Any]:
        """A storage clause with any filegroup remapping applied."""
        if not storage or not storage.get("data_space"):
            return {}
        mapped = dict(storage)
        name = str(storage["data_space"])
        if str(storage.get("data_space_type_desc") or "").upper() != "PARTITION_SCHEME":
            for source_name, dest_name in self.request.map_filegroups.items():
                if name.lower() == source_name.lower():
                    mapped["data_space"] = dest_name
                    break
        return mapped


def _plan_partitions(ctx: PlanContext) -> list[Step]:
    """Partition functions and schemes — first, because a table's ``ON`` clause needs them."""
    steps: list[Step] = []
    seen_functions: set[str] = set()
    for pair in schema_catalog.partition_schemes_used(ctx.cursor, ctx.source_schema):
        function_name = pair["function"]
        if function_name not in seen_functions:
            seen_functions.add(function_name)
            function = schema_catalog.partition_function(ctx.cursor, function_name)
            values = _boundary_values(ctx.request.partition_boundaries, function["values"])
            steps.append(Step(
                phase="partitions", name=function_name,
                sql=mssql_ddl.render_partition_function(
                    function_name, parameter_type=function["parameter_type"],
                    boundary_right=function["boundary_right"], values=values),
                guard=mssql_ddl.guard_partition_function_absent(function_name),
                detail={"kind": "partition_function",
                        "boundaries": len(values),
                        "boundaries_on_source": len(function["values"])}))
        scheme = schema_catalog.partition_scheme(ctx.cursor, pair["scheme"])
        filegroups = [ctx.request.map_filegroups.get(group, group)
                      for group in scheme["filegroups"]]
        steps.append(Step(
            phase="partitions", name=scheme["name"],
            sql=mssql_ddl.render_partition_scheme(
                scheme["name"], function_name=scheme["function"], filegroups=filegroups),
            guard=mssql_ddl.guard_partition_scheme_absent(scheme["name"]),
            detail={"kind": "partition_scheme", "filegroups": filegroups}))
    return steps


def _boundary_values(setting: Any, source_values: Sequence[Any]) -> list[Any]:
    """Which boundaries to create the function with.

    ``"all"`` mirrors the source, which is right for a copy. ``"none"`` and an explicit list exist
    for the schema whose boundaries are *owned* by a procedure at run time: pre-creating a month
    there makes that procedure throw on the month it was supposed to create. A partition function
    must have at least one boundary, so ``"none"`` still emits the empty-value form only if the
    source had none.
    """
    if isinstance(setting, (list, tuple)):
        return list(setting)
    choice = str(setting or "all").strip().lower()
    if choice == "none":
        return []
    if choice != "all":
        raise SchemaCopyError(
            'partition_boundaries must be "all", "none", or a list of boundary values.')
    return list(source_values)


def _plan_change_tracking_database(ctx: PlanContext) -> list[Step]:
    settings = schema_catalog.change_tracking_database(ctx.cursor)
    if not settings:
        ctx.notes.append("source database has no change tracking; nothing to enable.")
        return []
    return [Step(
        phase="change_tracking_database", name=ctx.request.dest.database,
        sql=mssql_ddl.render_change_tracking_database(
            ctx.request.dest.database, retention=settings["retention"],
            retention_units=settings["retention_units"], auto_cleanup=settings["auto_cleanup"]),
        # Not guarded by a rendered predicate: ALTER DATABASE cannot run inside EXEC() under the
        # guard form, so re-entry is checked at apply time instead.
        detail={"kind": "alter_database", **settings})]


def _plan_tables(ctx: PlanContext) -> list[Step]:
    steps = []
    for table in ctx.tables:
        by_index = schema_catalog.index_columns(ctx.cursor, ctx.source_schema, table)
        columns = schema_catalog.table_columns(ctx.cursor, ctx.source_schema, table)
        keys = schema_catalog.table_key_constraints(ctx.cursor, ctx.source_schema, table, by_index)
        storage = ctx.storage(schema_catalog.table_storage(
            ctx.cursor, ctx.source_schema, table, by_index))
        steps.append(Step(
            phase="tables", name=table,
            sql=mssql_ddl.render_create_table(ctx.dest_schema, table, columns,
                                              keys=keys, storage=storage),
            guard=mssql_ddl.guard_object_absent(ctx.dest_schema, table, object_type="U"),
            detail={"columns": len(columns), "keys": len(keys),
                    "storage": storage.get("data_space") or ""}))
    return steps


def _plan_change_tracking_tables(ctx: PlanContext) -> list[Step]:
    selected = set(ctx.tables)
    steps = []
    for entry in schema_catalog.change_tracking_tables(ctx.cursor, ctx.source_schema):
        if entry["table"] not in selected:
            continue
        steps.append(Step(
            phase="change_tracking_tables", name=entry["table"],
            sql=mssql_ddl.render_change_tracking_table(
                ctx.dest_schema, entry["table"],
                track_columns_updated=entry["track_columns_updated"]),
            detail={"kind": "alter_table", **entry}))
    return steps


def _plan_indexes(ctx: PlanContext) -> list[Step]:
    steps = []
    for table in ctx.tables:
        by_index = schema_catalog.index_columns(ctx.cursor, ctx.source_schema, table)
        for index in schema_catalog.table_indexes(ctx.cursor, ctx.source_schema, table, by_index):
            index = dict(index)
            index["storage"] = ctx.storage(index.get("storage"))
            steps.append(Step(
                phase="indexes", name=f"{table}.{index['name']}",
                sql=mssql_ddl.render_create_index(ctx.dest_schema, table, index),
                guard=mssql_ddl.guard_index_absent(ctx.dest_schema, table, index["name"]),
                detail={"table": table, "index": index["name"],
                        "partitioned": str(index["storage"].get("data_space_type_desc") or ""
                                           ).upper() == "PARTITION_SCHEME"}))
    return steps


def _plan_checks(ctx: PlanContext) -> list[Step]:
    steps = []
    for table in ctx.tables:
        for check in schema_catalog.table_check_constraints(ctx.cursor, ctx.source_schema, table):
            steps.append(Step(
                phase="checks", name=check["name"],
                sql=mssql_ddl.render_check_constraint(ctx.dest_schema, table, check),
                guard=mssql_ddl.guard_object_absent(ctx.dest_schema, check["name"]),
                detail={"table": table}))
    return steps


def _plan_data(ctx: PlanContext) -> list[Step]:
    """One step per table named in ``with_data``. The rows are not read here — a plan reads counts.

    Row *counts* come from the source so a plan can say how much is about to move; the rows
    themselves are streamed at apply time. Materialising 100k rows to describe them would make the
    harmless mode the expensive one.
    """
    if not ctx.request.with_data:
        return []
    wanted, _ = name_filter.split(ctx.tables, include=ctx.request.with_data)
    missed = name_filter.unused_patterns(ctx.tables, ctx.request.with_data)
    if missed:
        ctx.notes.append(
            f"with_data patterns matched no selected table: {', '.join(missed)} — check for a "
            "typo, or for a table excluded by exclude_tables.")
    steps = []
    for table in wanted:
        columns = schema_catalog.insertable_columns(ctx.cursor, ctx.source_schema, table)
        steps.append(Step(
            phase="data", name=table, kind="data",
            guard=mssql_ddl.guard_table_empty(ctx.dest_schema, table),
            detail={"table": table, "columns": columns,
                    "has_identity": schema_catalog.has_identity(
                        ctx.cursor, ctx.source_schema, table),
                    "source_rows": schema_catalog.row_count(
                        ctx.cursor, ctx.source_schema, table)}))
    return steps


def _plan_modules(ctx: PlanContext) -> list[Step]:
    return [Step(phase="modules", name=module["name"],
                 sql=mssql_ddl.as_create_or_alter(module["definition"]),
                 detail={"type": module["type_desc"]})
            for module in ctx.modules]


def _plan_foreign_keys(ctx: PlanContext) -> list[Step]:
    """Foreign keys last, so the data phase cannot be blocked by a load order it does not control."""
    selected = set(ctx.tables)
    steps = []
    for foreign_key in schema_catalog.foreign_keys(ctx.cursor, ctx.source_schema):
        if foreign_key["table"] not in selected:
            continue
        # A key pointing at a table this copy is not shipping would create a constraint against
        # something that is not there. Report it rather than emitting a statement that fails.
        if (foreign_key["referenced_schema"] == ctx.source_schema
                and foreign_key["referenced_table"] not in selected):
            ctx.notes.append(
                f"foreign key {foreign_key['name']} skipped: it references "
                f"{foreign_key['referenced_table']}, which is not in this copy.")
            continue
        mapped = dict(foreign_key)
        if foreign_key["referenced_schema"] == ctx.source_schema:
            mapped["referenced_schema"] = ctx.dest_schema
        steps.append(Step(
            phase="foreign_keys", name=foreign_key["name"],
            sql=mssql_ddl.render_foreign_key(ctx.dest_schema, mapped),
            guard=mssql_ddl.guard_object_absent(ctx.dest_schema, foreign_key["name"]),
            detail={"table": foreign_key["table"],
                    "references": f"{mapped['referenced_schema']}.{mapped['referenced_table']}"}))
    return steps


#: Phase name -> planner. A new phase is one entry here plus its position in :data:`PHASES`, which
#: is the whole reason this is a table and not an if-chain: the order and the work are stated
#: separately, so neither can be changed by accident while editing the other.
PHASE_PLANNERS: dict[str, Callable[[PlanContext], list[Step]]] = {
    "partitions": _plan_partitions,
    "change_tracking_database": _plan_change_tracking_database,
    "tables": _plan_tables,
    "change_tracking_tables": _plan_change_tracking_tables,
    "indexes": _plan_indexes,
    "checks": _plan_checks,
    "data": _plan_data,
    "modules": _plan_modules,
    "foreign_keys": _plan_foreign_keys,
}


def select_tables(cursor: Any, request: SchemaCopyRequest) -> tuple[list[str], list[str], list[str]]:
    """``(kept, skipped, unused_patterns)`` for the tables — reusable on its own for a dry check."""
    available = schema_catalog.list_tables(cursor, request.source.schema)
    kept, skipped = name_filter.split(available, include=request.include_tables,
                                      exclude=request.exclude_tables)
    unused = name_filter.unused_patterns(
        available, tuple(request.include_tables) + tuple(request.exclude_tables))
    return kept, skipped, unused


def select_modules(cursor: Any, request: SchemaCopyRequest
                   ) -> tuple[list[dict[str, Any]], list[str], list[str]]:
    """``(kept, skipped_names, unused_patterns)`` for the programmable objects."""
    available = schema_catalog.modules(cursor, request.source.schema)
    names = [module["name"] for module in available]
    kept_names, skipped = name_filter.split(names, include=request.include_modules,
                                            exclude=request.exclude_modules)
    keep = set(kept_names)
    unused = name_filter.unused_patterns(
        names, tuple(request.include_modules) + tuple(request.exclude_modules))
    return [module for module in available if module["name"] in keep], skipped, unused


def build_plan(cursor: Any, request: SchemaCopyRequest) -> dict[str, Any]:
    """Read the source and produce the whole plan. **Reads only** — safe to run against anything.

    ``cursor`` is an open cursor on the *source* database. The result is JSON-safe and is what a
    ``plan`` run returns verbatim, so what an operator approves is literally what an ``apply``
    then executes.
    """
    context = PlanContext(cursor=cursor, request=request)
    context.tables, context.skipped_tables, unused_tables = select_tables(cursor, request)
    context.modules, context.skipped_modules, unused_modules = select_modules(cursor, request)
    for pattern in unused_tables:
        context.notes.append(f"table pattern {pattern!r} matched nothing in "
                             f"{request.source.label()}.")
    for pattern in unused_modules:
        context.notes.append(f"module pattern {pattern!r} matched nothing in "
                             f"{request.source.label()}.")

    steps: list[Step] = []
    for phase in request.phases:
        steps.extend(PHASE_PLANNERS[phase](context))

    unsupported = (schema_catalog.unsupported_features(cursor, request.source.schema)
                   if request.report_unsupported else [])
    counts = {phase: sum(1 for step in steps if step.phase == phase) for phase in request.phases}
    counts["rows_to_copy"] = sum(int(step.detail.get("source_rows") or 0)
                                 for step in steps if step.kind == "data")
    counts["partitioned_indexes"] = sum(1 for step in steps
                                        if step.detail.get("partitioned") is True)
    return {
        "source": request.source.label(),
        "dest": request.dest.label(),
        "phases": list(request.phases),
        "tables": context.tables,
        "skipped_tables": context.skipped_tables,
        "modules": [module["name"] for module in context.modules],
        "skipped_modules": context.skipped_modules,
        "steps": [step.to_json() for step in steps],
        "counts": counts,
        "unsupported": unsupported,
        "notes": context.notes,
    }


def plan_steps(plan: Mapping[str, Any]) -> list[Step]:
    """Rebuild the :class:`Step` objects from a plan dict — the apply side's entry point."""
    return [Step(phase=item["phase"], name=item["name"], sql=item.get("sql") or "",
                 guard=item.get("guard") or "", kind=item.get("kind") or "ddl",
                 detail=item.get("detail") or {})
            for item in plan.get("steps") or ()]


# --------------------------------------------------------------------------------- destination


def assert_destination(cursor: Any, *, database: str, instance: str = "") -> dict[str, Any]:
    """Refuse to write unless this really is the destination the request named.

    **``DB_NAME()`` alone is not an identity.** One estate had ``APPDB_Prod`` on both the UAT and
    the production instance; a guard on the database name passes on either. ``instance`` is
    matched against ``SERVERPROPERTY('ServerName')`` as a case-insensitive substring, so a request
    may name the instance (``PRODHOST\\APPINST``) without knowing the host's full computed name.
    """
    identity = schema_catalog.server_identity(cursor)
    actual_database = str(identity.get("database_name") or "")
    actual_server = str(identity.get("server_name") or "")
    if actual_database.lower() != str(database).lower():
        raise SchemaCopyError(
            f"ABORT: connected to database {actual_database!r}, expected {database!r}.")
    if instance and str(instance).lower() not in actual_server.lower():
        raise SchemaCopyError(
            f"ABORT: connected to instance {actual_server!r}, which does not match the required "
            f"{instance!r}. The same database name exists on more than one instance of this "
            "estate — refusing to write to the wrong one.")
    if not instance:
        identity["warning"] = (
            "no assert_dest_instance was given, so only the database name was checked. The same "
            "database name commonly exists on several instances.")
    return identity


@contextmanager
def application_lock(cursor: Any, resource: str, *,
                     timeout_seconds: int = DEFAULT_LOCK_TIMEOUT_SECONDS) -> Iterator[int]:
    """Hold ``sp_getapplock`` over the whole operation, and release it however this ends.

    Session-scoped rather than transaction-scoped: the copy runs statement by statement with
    autocommit on — DDL, ``ALTER DATABASE`` and bulk loads do not belong in one transaction — so a
    transaction-owned lock would be released by the first commit and protect nothing.

    This is the guard the 2026-08-22 run did not have: two appliers reached one destination at
    once, and the phase guards did not prevent it because every one of them is read-then-write.
    """
    timeout_ms = max(0, int(timeout_seconds)) * 1000
    sql = (
        "DECLARE @rc int; "
        f"EXEC @rc = sp_getapplock @Resource = {mssql_ddl.quote_string(resource)}, "
        f"@LockMode = 'Exclusive', @LockOwner = 'Session', @LockTimeout = {timeout_ms}; "
        "SELECT @rc AS result;"
    )
    result = schema_catalog.scalar(cursor, sql)
    code = int(result if result is not None else -999)
    if code < 0:
        reason = {-1: f"timed out after {timeout_seconds}s — another copy is running against "
                      "this destination",
                  -2: "the lock request was cancelled",
                  -3: "the request was chosen as a deadlock victim",
                  -999: "the lock call returned an unexpected result"}.get(
                      code, f"sp_getapplock returned {code}")
        raise SchemaCopyError(f"could not acquire the schema-copy lock on {resource!r}: {reason}.")
    try:
        yield code
    finally:
        try:
            cursor.execute(
                f"EXEC sp_releaseapplock @Resource = {mssql_ddl.quote_string(resource)}, "
                "@LockOwner = 'Session';")
        except Exception:  # noqa: BLE001 - a lock that outlives the session is released by it.
            pass


# --------------------------------------------------------------------------------------- apply


def _execute(cursor: Any, step: Step) -> None:
    """Run one DDL step in its guarded form."""
    cursor.execute(step.script())


def _apply_alter_database_change_tracking(cursor: Any, step: Step) -> str:
    """``ALTER DATABASE`` cannot run inside ``EXEC()`` under a guard, so it is checked first."""
    already = schema_catalog.scalar(
        cursor, "SELECT COUNT(*) FROM sys.change_tracking_databases WHERE database_id = DB_ID()")
    if int(already or 0):
        return "already on"
    cursor.execute(step.sql)
    return "enabled"


def _apply_change_tracking_table(cursor: Any, step: Step, schema: str) -> str:
    table = str(step.detail.get("table") or step.name)
    already = schema_catalog.scalar(
        cursor, "SELECT COUNT(*) FROM sys.change_tracking_tables WHERE object_id = OBJECT_ID("
                f"{mssql_ddl.quote_string(f'{schema}.{table}')})")
    if int(already or 0):
        return "already tracked"
    cursor.execute(step.sql)
    return "enabled"


def copy_table_data(source_cursor: Any, dest_cursor: Any, *, source_schema: str, dest_schema: str,
                    table: str, columns: Sequence[str], has_identity: bool,
                    placeholders: str, batch_size: int = DEFAULT_BATCH_SIZE,
                    skip_if_not_empty: bool = True) -> dict[str, Any]:
    """Push one table's rows across, in batches, from the client.

    The natural implementation — ``INSERT … SELECT FROM [OtherDb].[schema].[table]`` — works only
    while both databases are on one instance, and every cross-instance copy discovers that after
    writing it. Rows are read with ``fetchmany`` rather than ``fetchall`` so a large table is
    bounded by the batch and not by its own size.

    ``IDENTITY_INSERT`` is toggled per table because it is a *session* setting with a limit of one
    table at a time; leaving it on leaks into the next table's load as a plain error.
    """
    qualified_dest = mssql_ddl.qualify(dest_schema, table)
    if skip_if_not_empty:
        existing = int(schema_catalog.scalar(
            dest_cursor, f"SELECT COUNT_BIG(*) FROM {qualified_dest}") or 0)
        if existing:
            return {"table": table, "rows": 0, "skipped": True, "existing_rows": existing}

    column_list = ", ".join(mssql_ddl.quote(name) for name in columns)
    insert = f"INSERT INTO {qualified_dest} ({column_list}) VALUES ({placeholders})"
    source_cursor.execute(
        f"SELECT {column_list} FROM {mssql_ddl.qualify(source_schema, table)}")

    copied = 0
    if has_identity:
        dest_cursor.execute(f"SET IDENTITY_INSERT {qualified_dest} ON")
    try:
        # pyodbc only, and worth an order of magnitude on a wide table; the pymssql fallback has
        # no equivalent and simply does not get one.
        if hasattr(dest_cursor, "fast_executemany"):
            dest_cursor.fast_executemany = True
        while True:
            batch = source_cursor.fetchmany(batch_size)
            if not batch:
                break
            dest_cursor.executemany(insert, [list(row) for row in batch])
            copied += len(batch)
    finally:
        if has_identity:
            try:
                dest_cursor.execute(f"SET IDENTITY_INSERT {qualified_dest} OFF")
            except Exception:  # noqa: BLE001 - the load's error is the one worth reporting.
                pass
    return {"table": table, "rows": copied, "skipped": False, "existing_rows": 0}


def apply_plan(plan: Mapping[str, Any], *, source_cursor: Any, dest_cursor: Any,
               request: SchemaCopyRequest, dest_connection: Any = None,
               progress: Callable[[str], None] | None = None) -> dict[str, Any]:
    """Execute a plan against the destination, phase by phase, and report what each did.

    The modules phase is the one that is not a straight loop: a view over a view, or a function
    used by another, resolves at **create** time, so a single ordered pass fails on whichever came
    first. Failures are re-tried while each pass reduces their number; what is still failing when
    progress stops is a genuinely broken module and is reported as one.
    """
    say = progress or (lambda _message: None)
    steps = plan_steps(plan)
    # **The connection, not the cursor.** `is_pymssql` is set on db_ops' own connection wrapper by
    # `sql_execution.SqlServerConnection` when the ODBC attempt failed; `cursor.connection` is the
    # raw driver object underneath it and does not carry the flag. Asking the wrong one builds `?`
    # placeholders on a pymssql connection, which is right on the Windows master and wrong in the
    # Linux worker — the exact failure `table_load` hit on 2026-08-13.
    style = db_connect.parameter_style("sqlserver", dest_connection)

    results: dict[str, Any] = {"phases": {}, "errors": [], "data": [], "rows_copied": 0}
    if request.create_schema:
        dest_cursor.execute(
            f"{mssql_ddl.guard_schema_absent(request.dest.schema)} "
            f"EXEC({mssql_ddl.quote_string(f'CREATE SCHEMA {mssql_ddl.quote(request.dest.schema)}')});")

    for phase in plan.get("phases") or ():
        phase_steps = [step for step in steps if step.phase == phase]
        if not phase_steps:
            results["phases"][phase] = {"planned": 0, "done": 0}
            continue
        say(f"-- {phase}: {len(phase_steps)} step(s)")
        if phase == "modules":
            outcome = _apply_modules(dest_cursor, phase_steps, request, say)
        elif phase == "data":
            outcome = _apply_data(source_cursor, dest_cursor, phase_steps, request,
                                  style, say)
            results["data"] = outcome.pop("tables", [])
            results["rows_copied"] = outcome.get("rows", 0)
        else:
            outcome = _apply_ddl(dest_cursor, phase, phase_steps, request, say)
        results["errors"].extend(outcome.pop("errors", []))
        results["phases"][phase] = outcome
    return results


def _apply_ddl(cursor: Any, phase: str, steps: Sequence[Step], request: SchemaCopyRequest,
               say: Callable[[str], None]) -> dict[str, Any]:
    done = 0
    notes: list[str] = []
    errors: list[dict[str, str]] = []
    for step in steps:
        try:
            if phase == "change_tracking_database":
                notes.append(f"{step.name}: {_apply_alter_database_change_tracking(cursor, step)}")
            elif phase == "change_tracking_tables":
                notes.append(f"{step.name}: "
                             f"{_apply_change_tracking_table(cursor, step, request.dest.schema)}")
            else:
                _execute(cursor, step)
            done += 1
        except Exception as exc:  # noqa: BLE001 - a failed statement is an operator message.
            errors.append({"phase": phase, "name": step.name, "error": str(exc)})
            say(f"   FAILED {step.name}: {exc}")
    return {"planned": len(steps), "done": done, "notes": notes, "errors": errors}


def _apply_modules(cursor: Any, steps: Sequence[Step], request: SchemaCopyRequest,
                   say: Callable[[str], None]) -> dict[str, Any]:
    pending = list(steps)
    passes = 0
    errors: list[dict[str, str]] = []
    while pending and passes < request.module_passes:
        passes += 1
        failed: list[Step] = []
        errors = []
        for step in pending:
            try:
                cursor.execute(step.sql)
            except Exception as exc:  # noqa: BLE001 - retried below, reported if it persists.
                failed.append(step)
                errors.append({"phase": "modules", "name": step.name, "error": str(exc)})
        if len(failed) == len(pending):
            # No progress this pass: what is left is broken, not merely out of order.
            break
        if failed:
            say(f"   pass {passes}: {len(failed)} module(s) deferred to the next pass")
        pending = failed
    for entry in errors:
        say(f"   FAILED {entry['name']}: {entry['error']}")
    return {"planned": len(steps), "done": len(steps) - len(pending), "passes": passes,
            "errors": errors}


def _apply_data(source_cursor: Any, dest_cursor: Any, steps: Sequence[Step],
                request: SchemaCopyRequest, style: str,
                say: Callable[[str], None]) -> dict[str, Any]:
    tables: list[dict[str, Any]] = []
    errors: list[dict[str, str]] = []
    total = 0
    for step in steps:
        columns = list(step.detail.get("columns") or ())
        # One builder for every bulk load db_ops does; `create-table-from-xlsx` writes the same
        # placeholders from the same function, so a driver added there is added here too.
        placeholders = table_load.build_placeholders(style, len(columns))
        try:
            outcome = copy_table_data(
                source_cursor, dest_cursor,
                source_schema=request.source.schema, dest_schema=request.dest.schema,
                table=str(step.detail.get("table") or step.name), columns=columns,
                has_identity=bool(step.detail.get("has_identity")),
                placeholders=placeholders, batch_size=request.batch_size,
                skip_if_not_empty=request.skip_nonempty_tables)
            tables.append(outcome)
            total += int(outcome["rows"])
            say(f"   {outcome['table']}: "
                + (f"SKIPPED, already has {outcome['existing_rows']:,} rows"
                   if outcome["skipped"] else f"{outcome['rows']:,} rows"))
        except Exception as exc:  # noqa: BLE001 - one table's failure is not the whole phase's.
            errors.append({"phase": "data", "name": step.name, "error": str(exc)})
            say(f"   FAILED {step.name}: {exc}")
    return {"planned": len(steps), "done": len(tables), "rows": total, "tables": tables,
            "errors": errors}


# -------------------------------------------------------------------------------------- verify


def verify_copy(source_cursor: Any, dest_cursor: Any, request: SchemaCopyRequest,
                plan: Mapping[str, Any]) -> dict[str, Any]:
    """Compare the two schemas on the things that matter, and say which comparisons should match.

    ``tables`` is *expected* to differ when the request excluded any, and saying so is the point:
    a report where every difference looks like a fault is a report nobody reads twice.
    """
    source = schema_catalog.schema_fingerprint(source_cursor, request.source.schema)
    destination = schema_catalog.schema_fingerprint(dest_cursor, request.dest.schema)
    expect_equal: set[str] = set()
    if not plan.get("skipped_tables"):
        expect_equal |= {"tables", "columns", "indexes", "check_constraints", "foreign_keys",
                         "partitioned_indexes", "change_tracked_tables"}
    if not plan.get("skipped_modules"):
        expect_equal |= {"procedures", "views", "functions"}

    row_counts = []
    for step in plan_steps(plan):
        if step.kind != "data":
            continue
        table = str(step.detail.get("table") or step.name)
        left = int(step.detail.get("source_rows") or 0)
        right = schema_catalog.row_count(dest_cursor, request.dest.schema, table)
        row_counts.append({"table": table, "source": left, "destination": right,
                           "match": left == right})

    comparison = schema_catalog.compare_fingerprints(source, destination,
                                                     expect_equal=expect_equal)
    return {
        "counts": comparison,
        "row_counts": row_counts,
        "untrusted_foreign_keys": destination.get("untrusted_foreign_keys", 0),
        "mismatches": ([item["count"] for item in comparison if item["status"] == "MISMATCH"]
                       + [item["table"] for item in row_counts if not item["match"]]),
    }


# ---------------------------------------------------------------------------------- entry point


def _connect(endpoint: Endpoint, request: SchemaCopyRequest) -> Any:
    """One connection, opened the way every db_ops caller opens one.

    Target resolution, credential decryption and driver choice all belong to ``sql_run``; nothing
    here knows a host, a login or a password. Autocommit is on because the operation is DDL,
    ``ALTER DATABASE`` and bulk loads — none of which belong in one transaction, and the phase
    guards are what make a partial run resumable instead.
    """
    parsed = sql_run.SqlRunRequest.from_json({
        "target": endpoint.target,
        "database": endpoint.database,
        "credential_name": endpoint.credential_name,
        "data_dir": request.data_dir,
        "sql": "SELECT 1",
    })
    try:
        resolved = sql_run.resolve_request_target(parsed)
        if str(resolved.get("db_type") or "") != "sqlserver":
            raise SchemaCopyError(
                f"{endpoint.target} is {resolved.get('db_type')}; schema copy is SQL Server only.")
        return sql_run.connect_target(resolved, timeout_seconds=request.timeout_seconds,
                                      autocommit=True)
    except sql_run.SqlRunError as exc:
        raise SchemaCopyError(f"{endpoint.target}: {exc}") from exc


def copy_schema(request: Any, *, progress: Callable[[str], None] | None = None) -> dict[str, Any]:
    """Plan — and, when asked, apply — one cross-instance schema copy.

    Returns the plan, what each phase did, and the verification, all JSON-safe. A ``plan`` run
    never opens a write connection at all, which is what makes it safe to hand to anyone: it
    cannot mistarget something it never connected to.
    """
    parsed = SchemaCopyRequest.from_json(request)
    say = progress or (lambda _message: None)
    started = time.monotonic()

    source_connection = _connect(parsed.source, parsed)
    result: dict[str, Any] = {"mode": "plan" if parsed.plan_only else "apply"}
    dest_connection = None
    try:
        source_cursor = source_connection.cursor()
        if not schema_catalog.schema_exists(source_cursor, parsed.source.schema):
            raise SchemaCopyError(
                f"schema {parsed.source.schema!r} does not exist in {parsed.source.label()}.")
        say(f"[src ] {parsed.source.label()}")
        plan = build_plan(source_cursor, parsed)
        result["plan"] = plan

        if parsed.plan_only:
            result["applied"] = None
            result["message"] = "plan only — nothing was written."
            return result

        dest_connection = _connect(parsed.dest, parsed)
        dest_cursor = dest_connection.cursor()
        result["destination"] = assert_destination(
            dest_cursor, database=parsed.dest.database, instance=parsed.assert_dest_instance)
        say(f"[dest] {result['destination'].get('server_name')} / "
            f"{result['destination'].get('database_name')}")

        with application_lock(dest_cursor, parsed.resource_name(),
                              timeout_seconds=parsed.lock_timeout_seconds):
            result["applied"] = apply_plan(plan, source_cursor=source_cursor,
                                           dest_cursor=dest_cursor, request=parsed,
                                           dest_connection=dest_connection, progress=say)
            if parsed.verify:
                result["verification"] = verify_copy(source_cursor, dest_cursor, parsed, plan)
        errors = result["applied"]["errors"]
        result["message"] = (
            f"{len(plan['steps'])} step(s) applied to {parsed.dest.label()}"
            + (f"; {len(errors)} failed." if errors else "."))
        return result
    finally:
        result["duration_ms"] = int((time.monotonic() - started) * 1000)
        db_connect.close_quietly(source_connection)
        if dest_connection is not None:
            db_connect.close_quietly(dest_connection)


def format_plan(plan: Mapping[str, Any], *, show_sql: bool = False) -> str:
    """The plan as lines a person reads before approving it.

    Counts first and statements only on request: the question at approval time is "how many of
    what, and what is it not carrying", and 400 statements in front of that answer buries it.
    """
    lines = [f"source : {plan.get('source')}", f"dest   : {plan.get('dest')}", ""]
    lines.append(f"tables : {name_filter.describe(plan.get('tables') or [], plan.get('skipped_tables') or [], noun='table')}")
    lines.append(f"modules: {name_filter.describe(plan.get('modules') or [], plan.get('skipped_modules') or [], noun='module')}")
    lines.append("")
    for phase in plan.get("phases") or ():
        count = (plan.get("counts") or {}).get(phase, 0)
        lines.append(f"  {phase:<26} {count}")
    counts = plan.get("counts") or {}
    if counts.get("partitioned_indexes"):
        lines.append(f"  {'(on a partition scheme)':<26} {counts['partitioned_indexes']}")
    if counts.get("rows_to_copy"):
        lines.append(f"  {'rows to copy':<26} {counts['rows_to_copy']:,}")

    unsupported = [item for item in (plan.get("unsupported") or ())]
    if unsupported:
        lines += ["", "NOT carried by this copy:"]
        for item in unsupported:
            status = "?" if item.get("status") == "unknown" else str(item.get("count"))
            lines.append(f"  {item['feature']:<26} {status:>4}  {item['note']}")
    for note in plan.get("notes") or ():
        lines.append(f"  note: {note}")
    if show_sql:
        lines += ["", "-- statements --"]
        for step in plan.get("steps") or ():
            if step.get("sql"):
                lines.append(mssql_ddl.guarded(step.get("guard") or "", step["sql"]))
    return "\n".join(lines)
