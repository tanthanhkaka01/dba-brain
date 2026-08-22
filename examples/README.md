# Examples

Worked configurations you can copy whole. Each directory is a complete **tool root** — a
`config.json` and a `data/` folder — so you can stand in it and run the toolkit against it without
touching anything else.

| Example | What it shows |
| --- | --- |
| [`postgres-quickstart/`](./postgres-quickstart) | The smallest configuration that does real work: one throwaway PostgreSQL container, a least-privilege monitoring login, seven metrics collected into a local SQLite store. No message delivery, no scheduler, nothing to uninstall. **Start here** — it needs no system packages, because the PostgreSQL driver is pure Python. |
| [`sqlserver-quickstart/`](./sqlserver-quickstart) | The same shape against the engine most of the metric catalogue is written for, and it ends somewhere more interesting: the collection finds a **real problem** with the instance, you fix it, and the finding clears. Also shows the three things SQL Server does differently — `service_name` is a label and never a database, the engine version picks the query, and severity is graded by the target's environment. Needs Microsoft's ODBC driver. |

## How a tool root is found

The toolkit does **not** look for its configuration next to its own code. It asks, in order:

1. `DB_OPS_HOME`, if set;
2. the current working directory, if it holds `data/` or `config.json`;
3. the package location, as the fallback that keeps a source checkout and the container working.

`DB_OPS_DATA_DIR` moves the data folder on its own, for an installed copy whose configuration
lives where the operator keeps configuration rather than beside the code.

That is why an example directory works by being *stood in*:

```bash
cd examples/postgres-quickstart
python -m db_ops.metrics.cli --config config.json collect --dry-run
```

Full detail in [`docs/configuration.md`](../docs/configuration.md).

## Placeholders

Every address in `data/*.example.json` and in these examples comes from the ranges reserved for
documentation — `192.0.2.x`, `198.51.100.x`, `203.0.113.x` (RFC 5737) and `example.com`
(RFC 2606) — so an example host can never be somebody's real machine. `127.0.0.1` appears only
where the value is genuinely loopback, as it is for a container running on your own laptop.
