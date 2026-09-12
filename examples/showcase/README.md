# Showcase — real published pages

These are the actual HTML reports a running estate publishes, copied whole and then rewritten so
that **every name belonging to that estate is a stable fake one**. Nothing here is a mock-up: the
fleet sizes, the storage figures, the fragmentation percentages, the SLA verdicts and the index
recommendations are what the tool measured. Only the *names* are not real.

## These pages are a snapshot of something that keeps moving

On the node that produced them, these are **live pages, rebuilt automatically about once an hour**
and republished in place under the same file name — so the copy you would fetch at noon is not the
copy you fetched at eleven. That is the point of them: a report is only worth reading if it is
current.

Two consequences, and they are why the file names here look the way they do:

* **What is in this folder is one capture, frozen.** Each file name carries the moment *that page*
  states it was built, in UTC: `20260912T0130Z_sla.html` is the SLA page as of 01:30 UTC on
  2026-09-12. Not the moment it was downloaded — a page may have been built hours earlier and not
  rebuilt since, and the name says which. (A page whose own header carries no stamp keeps the name
  its node gave it, rather than claiming a moment it never stated.)
* **A live node keeps its history, and this folder does not.** The web host writes one archived
  copy per calendar day and answers `?date=2026-09-01` with the newest snapshot at or before that
  date, for any of these pages. That is a running node with its archive behind it; here there is
  nothing to ask, so the `?date=` links have been pointed at the files that are actually present.

How often the pages rebuild is configuration, not a fixed property: it is the `repeat_interval` of
the reports commands in `data/app_commands.json`, and the estate these came from runs them hourly.

## What you are looking at

| Page | What it answers |
| --- | --- |
| `*_index.html` | The landing page. **Start here** and follow the links — every page below is one click away |
| `*_database-inventory.html` | The whole fleet on one page: every instance, its databases, storage, backup age and health triage, with the findings that need somebody |
| `*_server-metrics.html` | Per-server charts over time — CPU, memory, disk, the OS side of each machine |
| `*_index-usage_<server>.html` | One page per server: which indexes are used, which are not, which are fragmented, and what it would cost to fix them. The big ones are big because the server really has that many indexes |
| `*_sla.html` | Every SLA/SLO the estate declares, and which ones passed, are at risk, failed, or have no data to judge |

## How to look at them

**A repository cannot show you an HTML page.** GitHub renders an `.html` file as source and
`raw.githubusercontent.com` serves it as `text/plain`, so clicking one here shows you markup — that
is true of any HTML in any repository, however self-contained. A static site needs a static host.

* **Published site** — the same files, served. Everything works, including the fleet picker.
  ([`.github/workflows/pages.yml`](../../.github/workflows/pages.yml) publishes this folder; the
  repository owner turns it on under Settings → Pages.)
* **A local copy** — serve the folder rather than double-clicking:

  ```bash
  cd examples/showcase
  python -m http.server 8000      # then open http://localhost:8000/
  ```

Most of these pages are a single file and do open fine from disk. **The fleet page does not.**
`*_server-metrics.html` is one page for the whole estate and fetches one
`server-metrics_<server>.json` as you pick each server — 40 of them here. A browser refuses that
fetch from a `file://` page, so the charts sit on *Loading…* and then say so. That is the browser's
rule, not a defect in the copy: the `*.json` files are the data, they are all present, and they are
scrubbed along with the pages.

## What was replaced, and what that guarantees

The rewrite is not a search for things that *look* sensitive — that is how a scrub misses the one
that mattered. The terms come from two places the tool already knows exactly: the operator's own
inventory (addresses, `server_id`s, host names, credentials, people, in every spelling they are
written in), and the tool's own output shapes on the page (`db\schema.table.index` and the `k=v`
fields a collector writes), which give it the database, schema, table and index names that no
configuration file lists.

The replacements are **stable and shaped**, which is what makes these pages worth publishing:

* one machine is the same fake machine on every page — follow `ACME-192-0-2-15-MSSQL25-1433` from
  the inventory to its metrics to its index report and it is one server throughout;
* every fake address is in an RFC 5737 documentation range (`192.0.2.x`, `198.51.100.x`,
  `203.0.113.x`), reserved so that an example can never be a real machine;
* a `server_id` keeps its construction, an index keeps its `PK_`/`IX_` prefix, and engine-owned
  names (`dbo`, `master`, `msdb`, `postgres`, `public`) are left alone — renaming those would make
  the pages read as a different *product* rather than a different estate.

Nothing reversible was written. There is no mapping file and no key: a table that turned this
folder back into the estate would *be* the estate.

## How this folder is produced

By one command, never by hand:

```bash
python -m db_ops.common.cli build-showcase @request.json
```

It learns the vocabulary from every page first and then rewrites all of them, so an object that
first appears on day three is mapped on day one's page too. Then it hands the result to
`check-identifiers` — the same scanner that guards every public release — and **any** finding
deletes the output and fails the command. A partially scrubbed page is the one that gets published,
so there is no such thing as a partial success here.

Details, including the two things a first run always hits, are in
[`docs/13_common.md`](../../docs/13_common.md).
