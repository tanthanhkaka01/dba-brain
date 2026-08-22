# Changelog

All notable changes to DBA Brain are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project
follows [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

**Entries are written as the work lands, not reconstructed at release time.** A release that has to
remember what went into it gets it wrong, and the notes stop being trustworthy exactly when someone
is deciding whether to upgrade. Every pull request that changes behaviour adds its line to
`Unreleased`.

Write entries for the person deciding whether to upgrade: what changed for them, and what they must
do about it. Not the internal refactor that made it possible.

## [Unreleased]

> **Status: preview, and the suite is now green — 2,895 passed, 0 failed.**
>
> This was published early and deliberately, so the shape could be reviewed before it was
> finished. The monitoring path it claims *is* verified end to end against a real SQL Server:
> install, `db-ops init`, add one instance, collect metrics, alert to Telegram.
>
> The ten remaining failures were tests that read the maintainer's own configuration — a coupling,
> not a broken product. Eight of them ask "is my configuration still correct", which is a question
> only that estate can answer, and they now live in a private suite that is not part of this
> repository. Two were ordinary coupling and were fixed: they assert how a reply is formatted and
> how a metric is filtered, and each failed because something else in the call reached for a file
> this distribution does not ship.

### Added

- **`db-ops init`** — the first command anybody runs. Turns a directory into a working tool root:
  a SQLite store, an empty inventory, a starter metric catalogue, and an `AGENTS.md` next to the
  JSON explaining what to put in each file. Without it the toolkit could be installed and not
  started.
- **`db-ops encrypt-secret`** — turn `secrets/secret_text.json` into the store the toolkit reads.
  It was previously only in the deploy tooling, which this distribution does not ship.
- The store defaults to **SQLite** on a first run, so nothing has to be installed to hold the
  results of monitoring. Moving to PostgreSQL is an edit in `data/store_config.json`.

- Apache-2.0 `LICENSE` and `NOTICE`, `SECURITY.md`, `CONTRIBUTING.md`, `CODE_OF_CONDUCT.md`, this
  changelog, and `.env.example` — the project-governance set required before the first public
  release.
- `pyproject.toml`: the toolkit is installable. Database drivers moved behind extras
  (`[mssql]`, `[oracle]`, `[postgres]`, `[mysql]`, `[ssh]`, `[winrm]`, `[all]`), so an install
  no longer drags in an ODBC driver and an Oracle client for someone who runs only PostgreSQL.
- `DB_OPS_HOME` and `DB_OPS_DATA_DIR` for telling an installed copy where its configuration is.

### Fixed

- The test suite passes on a clean install. Ten tests failed because they read configuration files
  this distribution does not ship. Eight of them were asking *is my configuration still correct* —
  a question only the maintainer's estate can answer — and have moved to a suite that stays there;
  the other two were fixed, and still ship. CI runs the whole suite on 3.11, 3.12 and 3.13 with
  nothing skipped and nothing allowed to fail.
- The documented first run could not be completed. Two commands the guides told you to run are not
  in this distribution: `db_ops.control.cli encrypt-secret-text`, which moved to
  `db-ops encrypt-secret`, and `db_ops.reports.cli queue-metrics-reports`, whose app is not shipped
  yet — so the alert step failed whichever spelling you used. The guides now name what exists, and
  say plainly that the scheduled reporting path arrives in `v0.2.0`.
- **Alerts have a supported path again**: `db-ops metrics alert-summary` builds the text from the
  results already collected — it reads the store, not the instance, so it needs no passphrase and
  costs the monitored server nothing — and `db-ops telegram send-message` sends it.
- `db-ops init` printed a next step that fails. It suggested
  `db-ops metrics collect --key-base64 …`, but the key is parsed by the app rather than by its
  subcommand, so it errored with `unrecognized arguments` — a wrong position reported as a wrong
  flag. It now prints the form that works.
- An installed copy could not find its configuration. The tool derived its project root from the
  package's own file path, which is correct only when the package sits beside `data/` — true in a
  checkout and in the container, false for every `pip install`, where it resolved to
  `site-packages/data`. The root is now resolved as: `DB_OPS_HOME`, then the working directory if
  it holds `data/` or `config.json`, then the package location as a fallback. Checkout and
  container behaviour is unchanged.
- Documentation examples throughout the code now use the addresses RFC 5737 reserves for
  documentation, instead of addresses from a private range that a reader cannot tell apart from
  their own network.
- Report links no longer default to one particular server. `report_base_url` had a built-in
  fallback pointing at a real internal host, so an install that never configured it produced links
  that resolved and were wrong. Unset now means unset: HTML pages use relative hrefs and chat
  messages omit the link.
- The Oracle bridge reads its shared secret from the environment variable named by
  `sql_access.secret_ref`, the same convention the rest of the toolkit uses. It previously read one
  fixed variable name, which meant a second bridge on a second host could not be given its own
  secret.
- Inventory pages no longer hide servers nobody asked them to hide. A subnet prefix was a constant
  in the rendering library, so every inventory page silently dropped those servers with nothing on
  the page saying so. The default now hides nothing; set `inventory_exclude_ip_prefixes` in
  `reports_config.json` to exclude a range deliberately.
- Ten further modules resolved their own default config, log and runtime paths the same way, so
  each of them pointed into the install directory too: the restore config, the SLA policies, the
  metric definitions, the Telegram support commands, the runtime log, the schema export directory
  and the working directory of two subprocess calls. All of them now share the one resolution.

<!--
Section order, and what belongs in each:

### Added        new capability a user can invoke
### Changed      behaviour that differs from the previous release
### Deprecated   still works, will be removed; say in which version
### Removed      gone; say what replaces it
### Fixed        a bug, described by its symptom
### Security     anything with a security consequence, with the advisory link

At release: rename [Unreleased] to [X.Y.Z] - YYYY-MM-DD, add a fresh empty [Unreleased],
and make sure the version here, the version in pyproject.toml, and the git tag agree —
the release workflow fails if they do not.
-->
