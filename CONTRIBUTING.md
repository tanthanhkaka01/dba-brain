# Contributing to DBA Brain

Thanks for considering it. This project automates operations against production databases, so the
bar is "would a DBA trust this at 3am", not "does it work on my machine". The rules below exist
because of things that broke, not because of taste.

## Before you start

- **Open an issue first** for anything larger than a fix. A capability that does not fit the
  architecture is expensive to review and painful to reject after it is written.
- **Small, complete pull requests.** One change, its tests, and its documentation in the same PR.
- **Never include anything real.** No hostname, IP address, server name, account, connection
  string, chat ID, or credential from a system you operate — not in code, tests, fixtures,
  documentation, examples, or a commit message. This is checked by a secret-scanning job, but the
  job is the backstop, not the rule.

  **Use these placeholders, and only these**, so one example host stays one example host across
  the whole tree:

  | For | Use | Why |
  | --- | --- | --- |
  | addresses | `192.0.2.x`, `198.51.100.x`, `203.0.113.x` | RFC 5737 reserves them for documentation, so they can never be somebody's real machine |
  | hostnames | `db01.example.com` | RFC 2606 reserves `example.com` for the same reason |
  | organisations | `ACME`, `GLOBEX` | |
  | databases and services | `SALESDB`, `APPDB`, `FINDB` | |
  | accounts | `dba_user`, `monitor_user` | |

  Do **not** use `10.x`, `192.168.x` or `172.16-31.x`: those are real private ranges, and a reader
  cannot tell your example from your estate. Keep the example concrete — a filled-in request *is*
  the documentation, and `<host>` teaches nobody. Reuse the same placeholder for the same machine
  within a passage, so the example still tells its story.

  One trap worth naming: `11.2.0.2` and `8.1.7.0` are Oracle **version numbers**, not addresses.
  A regular expression that scrubs IPv4 will corrupt them. Classify before you replace.

## Development setup

```bash
git clone https://github.com/dba_userkaka01/dba-brain.git
cd dba-brain
python -m venv .venv
.venv/bin/pip install -e '.[dev]'        # Windows: .venv\Scripts\pip
```

Run the tests:

```bash
.venv/bin/python -m pytest tests -q          # full suite
.venv/bin/python -m pytest tests/test_<area>.py -q   # while developing
```

**The test suite is offline and stays offline.** No database, no network, no message delivery. It
is the reason CI can run on every push, and the reason you can refactor confidently. A test that
needs a live engine belongs in the (separate, scheduled) integration suite, never in `tests/`.

## The rules that matter

### Configuration is data

New thresholds, targets, routes, schedules, severities, and policies belong in the JSON
configuration, never as literals in Python. The entire design assumes an operator can change
behaviour by editing data they own — a hardcoded value takes that away and cannot be reviewed by
the person it affects.

### Components stay independent

The tree is a fixed set of components, each with one directory and its own CLI, sitting on two
shared layers:

- **Apps never import each other.** If an app needs another app's knowledge, it asks that app's
  CLI.
- **The `common` layer is never imported by an app** — it is invoked as a CLI. It performs
  *operations*: reaching a host, running SQL, handling secrets.
- **The `lib` layer is only ever imported, never run as a CLI.** It holds *values and rules* —
  pure functions of their arguments — and imports nothing from the rest of the project.

Each of these is enforced by a guard test. If your change needs an exception, the exception is
named at the guard with the reason, and it is part of the discussion in the issue — not something
to slip past.

### Every shared operation takes one JSON object

`common` commands take a single JSON object — inline, from a file, or on stdin — with the same
shape as the configuration files. Never ad-hoc flags for the payload. That is what lets a config
file, a chat command, and a shell caller pass the same request through untranslated.

### If you had to write a throwaway script, build it in instead

The moment a task needs a scratch script — auditing something, rotating something, probing a host
— it belongs in a `common` module with a CLI command, in the same change. A scratch script answers
once and takes its edge cases with it; the next person rewrites it and gets a different answer.
Several existing commands exist precisely because the scratch versions got the port, the protocol,
or the parameter binding wrong.

### Every component has a doc, and every doc has a component

One documentation file per component, both directions, enforced by a test. **A component is not
finished until its doc exists.** That guard was written after one shared layer reached 46 modules
and ~6,500 lines undocumented with a fully green suite — nothing connected the two directories, so
nothing noticed.

Change a component's behaviour, update its doc in the same pull request.

### Comments explain why, not what

This codebase is deliberately heavy on rationale: a comment explaining the failure that motivated
the code is the house style. Match the density of the code around you. Do not strip it, and do not
narrate trivial code.

### Tests read as prose

A module docstring explaining *why the behaviour matters*, then test names that are sentences:
`test_a_header_without_a_verdict_is_left_alone`. Match that.

### Everything written is in English

Code, comments, docstrings, tests, documentation, commit messages, and the `note` / `description`
fields inside configuration files. Discussion happens in whatever language suits the people
talking; the file on disk is English, so that it can be searched and read by everyone.

## Pull request checklist

- [ ] The full test suite passes.
- [ ] New behaviour has tests, and they are offline.
- [ ] The affected component's documentation is updated in the same PR.
- [ ] `CHANGELOG.md` has an entry under `Unreleased` describing the change for a *user*, not for a
      reviewer.
- [ ] No real host, account, or credential anywhere in the diff.
- [ ] Nothing production-facing changed without saying so explicitly in the PR description.

## Committing and releasing

A change has to pass one gate before it can be committed, and a version a second, stricter one
before it can be released.

**To commit:** the **full** test suite, lint, and a secret scan all pass locally — not the tests
you touched, the whole suite.

**To release, additionally:** green CI on that exact commit, a clean-room install of the built
wheel that is actually run end to end, and `docs/releases/vX.Y.Z.md` written **before** the tag,
carrying the commands that were run and their results rather than a summary from memory.

## Reporting bugs and security issues

- **Bugs:** open an issue with the version, the engine and its version, what you expected, what
  happened, and the smallest reproduction you have. Redact anything that identifies your systems.
- **Security:** do **not** open a public issue — follow [`SECURITY.md`](./SECURITY.md).

## Code of Conduct

Participation is governed by the [Code of Conduct](./CODE_OF_CONDUCT.md).

## License

By contributing, you agree that your contributions are licensed under the
[Apache License 2.0](./LICENSE), the same license as the project.
