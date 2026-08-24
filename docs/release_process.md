# Commit and release process

**This file is the authority. If a commit or a release did not follow it, it was wrong — including
the ones already in history.**

It exists because the first eight releases of this project did not follow one. Versions were cut
because the work felt finished, notes were written from memory at tag time, and defects that a
clean install would have caught went out to PyPI. Twice CI went red *after* a push that had not
been checked first. That is the practice this replaces.

---

## 0. Where the code comes from, and the three stages

This repository is **generated**, not edited directly. Work happens in a private tree; a manifest
decides what crosses; the copy is what becomes the GitHub repository and the PyPI package.

```
   ┌─────────────────┐   export-public   ┌──────────────┐   git push   ┌────────┐   tag+release   ┌──────┐
   │ private db_ops  │ ────────────────▶ │  dba-brain   │ ───────────▶ │ GitHub │ ──────────────▶ │ PyPI │
   │ (work happens)  │                   │ (the copy)   │              │  + CI  │                 │ GHCR │
   └─────────────────┘                   └──────────────┘              └────────┘                 └──────┘
        stage 1                              stage 2                                stage 3
```

**Three stages, three different gates. Passing one does not earn the next.**

| Stage | What happens | Gate |
| :-: | --- | --- |
| **1** | Change the private tree, commit it there | **§2 — the commit gate**: lint, full suite, export gate, secret scan |
| **2** | `export-public` into the public tree, commit and push it | §2 again *in the public tree*, plus **CI green on that commit** |
| **3** | Tag, GitHub Release → PyPI + GHCR | **§3 — the release gate**, which includes everything above |

Two consequences worth stating, because both have been got wrong:

- **A commit in the private tree publishes nothing.** The public repository only changes when
  somebody exports and pushes. So work can be committed freely at stage 1 and still not be
  released — that is the normal state, not a backlog.
- **The export copies the working tree, not the last commit.** Uncommitted work — including
  another session's — crosses at stage 2 unless stage 1 finished properly. §2.2.

### 0.1 The two rules everything else serves

1. **Nothing is committed until the full gate passes locally.** Not "the tests I touched" — the
   whole gate, in the order below.
2. **Nothing is released until it has been installed somewhere that never had the source, and
   run.** A green suite proves the code is consistent with itself. It does not prove the artifact
   works, and every serious defect in this project's history was found by installing it.

---

## 1. Authorship — every commit is `tanthanhkaka01`

| Field | Value |
| --- | --- |
| `user.name` | `Trieu Tan Thanh` |
| `user.email` | `tanthanhkaka01@gmail.com` |

Check once per clone:

```bash
git config user.name && git config user.email
```

**Never add a `Co-Authored-By:` trailer**, for Claude or anything else. GitHub parses that trailer
and credits the named identity as a **contributor on the repository**. Eight commits carried
`Co-Authored-By: Claude` and put a `claude` account in the contributor list of a repository that is
entirely this operator's work. Removing them meant rewriting all of history, re-pointing eight
release tags and a force-push — an hour to undo something that took one line to cause.

A plain-text footer (`🤖 Generated with Claude Code`) is fine: it is text, and creates no
contributor.

**Refuse to commit if:**

```bash
git log -1 --format='%an <%ae>%n%b' | grep -i 'co-authored-by'   # must print nothing
```

---

## 2. The commit gate

Run all four. **In this order**, because each is cheaper than the next and failing early is free.

| # | Gate | Command | Pass condition |
| :-: | --- | --- | --- |
| 1 | Lint | `.venv/Scripts/python.exe -m ruff check db_ops tests` | `All checks passed!` |
| 2 | Full suite | `.venv/Scripts/python.exe -m pytest tests -q` | `0 failed`. Not `-k`, not one file |
| 3 | Export gate | `.venv/Scripts/python.exe -m db_ops.control.cli export-public <tmp> --plan-only` | `clean`, and **no `!! NOT COMMITTED` lines other than the files you are about to commit** |
| 4 | Secret scan | see §2.1 | no findings |

### 2.1 The secret scan, locally — and why CI's green is not enough

**CI's secret scan is incremental.** The action passes `--log-opts=<base>^..<head>`, so it reads
only the commits in that push range. It has never certified the whole tree, and a green
`secret-scan` badge does not mean the repository is clean — measured on 2026-08-24, when CI was
green and a whole-tree scan of the same commit reported **12 findings**.

So the local gate is the **whole-tree** scan, run over the tree that would actually ship:

```bash
# export first, so the scan sees exactly what would be published
python -m db_ops.control.cli export-public <tmp>
docker run --rm -v "<tmp>:/repo" -w /repo zricethezav/gitleaks:latest \
  detect --no-git --config /repo/.gitleaks.toml --redact
```

Pass condition: **`no leaks found`**. All 12 of those findings were test placeholders shaped like
credentials; each was decoded, compared against the real store passphrase, and allowlisted by its
own literal — so this scan is clean today, which is what makes a new finding meaningful.

A finding is resolved **by changing the code**, or by an entry in `.gitleaks.toml` **with its
reason written out**. Never by disabling a rule or excluding a path: a scanner that cries wolf is
one people learn to switch off, and a path allowlist over `tests/` would hide a genuine key checked
into a test — exactly what the scan exists to catch.

### 2.2 What the export gate is for

`export-public` copies the **working tree**, so anything uncommitted — including another session's
half-written files — ships with whatever is exported next. It has already published five unfinished
modules once. The `!!` block lists every file it would ship that nobody has committed. Read it.

It also runs `check-identifiers` over the result and **refuses**, deleting the tree, if the copy
names a real host, database or account. That gate caught three real database names in the
`copy-schema` examples after an earlier export of the same files had reported clean.

### 2.3 Scope of a commit

One commit is one change. **Never `git add -A`** in this repository: it is shared with other
sessions, and doing so has twice swept up work that was not mine — once another session's pending
deletion, once five in-flight modules. Stage the paths you touched, by name, then:

```bash
git diff --cached --name-only     # read it before committing
```

### 2.4 The message

Say what was wrong and how it was found, not what the diff shows. The house style is the *why*.
State the evidence: "verified on a fresh wheel, six commands `status=done`" is worth more than any
adjective.

---

## 3. The release gate

A release is a **separate, later decision** from a commit. Passing §2 earns a commit; it does not
earn a version.

### 3.1 Preconditions

| # | Precondition | How it is checked |
| :-: | --- | --- |
| 1 | The commit gate passed, and the tree is committed | §2 |
| 2 | Exported and pushed to `dba-brain`, and **CI is green on that exact commit** | §3.2 |
| 3 | The public suite passes **in the public tree**, not only the private one | `pytest tests -q` inside `dba-brain` |
| 4 | **A clean-room install of the built wheel runs** | §3.3 |
| 5 | **The release notes file exists and is complete** | §3.4 |
| 6 | The operator has said to release | Explicitly. Never inferred from "it works" |

### 3.2 CI must be green on the commit being released

All four jobs, on the exact SHA:

```
core (3.12)   success
core (3.13)   success
lint          success
secret-scan   success
```

Python **3.12 and 3.13** are the supported matrix (`requires-python = ">=3.12"`). Both must pass —
they are not the same run: a `csv.QUOTE_STRINGS` defect and a path-separator defect were each found
by one version and not the other.

**A red run older than the current commit is not a blocker** — GitHub keeps every run. Check the
run whose `head_sha` is the commit you are releasing, not the top of the list.

### 3.3 The clean-room run — the one that actually matters

Build the wheel, install it into an **empty virtualenv in a directory that never had the source**,
and run the product:

```bash
python -m build --wheel
python -m venv .venv && .venv/Scripts/python.exe -m pip install "dist/dbabrain-<version>-py3-none-any.whl[postgres]"
mkdir estate && cd estate
db-ops init                       # writes the tool root
# configure one real target, one credential, encrypt the secret
db-ops daemon --config config.json --once
```

**Pass condition: every scheduled command reaches `status=done`.** Not "no traceback" — `done`.

This step is not optional and not substitutable. It is the only thing that has ever caught: a
catalogue that shipped 3 metrics of 90, install hints naming a package that does not exist, a
daemon that ran the wrong Python, a console that showed nothing, and a default schedule that could
not complete one cycle. Every one of those passed the full suite.

### 3.4 The release notes file — required, written before the tag

**One file per version**, at `docs/releases/vX.Y.Z.md`. The tag is not created until it exists and
is complete. Notes written at tag time from memory are how a release stops being trustworthy
exactly when somebody is deciding whether to upgrade.

Required sections:

```markdown
# vX.Y.Z — <one line: what this release is for>

| | |
| --- | --- |
| Date | YYYY-MM-DD |
| Previous | vA.B.C |
| Python | 3.12, 3.13 |
| Artifacts | PyPI wheel + sdist, ghcr.io/<owner>/dbabrain:X.Y.Z |

## What this release is for
One paragraph. Why cut it now.

## Fixed since vA.B.C
One entry per defect: what was wrong, **how it was found**, and what proves it is fixed.
A defect with no evidence of the fix does not go in this list.

## Added since vA.B.C
Same standard.

## Changed / breaking
Anything a reader upgrading must do. "Nothing" is a valid answer and must be stated.

## Verified
The literal commands run and the literal results. Counts, not adjectives.

## Known not to work
What is still broken, deferred, or unverified. **This section is never empty by default** —
if it is, say why you believe it is.
```

`CHANGELOG.md` gets the short form; this file carries the evidence.

### 3.5 Version choice

| Change | Bump |
| --- | --- |
| A new capability, a new command, a new app | **minor** — `0.3.4` → `0.4.0` |
| Fixes and corrections only | **patch** — `0.3.3` → `0.3.4` |
| A change that breaks an existing tool root | **minor while `0.x`**, and it goes in *Changed / breaking* |

Set it in **one place** — `PUBLIC_VERSION` in `db_ops/lib/distribution.py` — and let the export
carry it. The release workflow refuses a tag that disagrees with `pyproject.toml`.

### 3.6 The release itself

Only after 3.1–3.5:

```bash
git tag -a vX.Y.Z -m "vX.Y.Z - <the one line>"
git push origin vX.Y.Z
# then create the GitHub Release, body taken from docs/releases/vX.Y.Z.md
```

Publishing a GitHub Release fires the `release` workflow: **build → publish (PyPI) → image
(GHCR)**. Watch all three. A tag is not a release until all three are green.

### 3.7 After the release

Verify the artifacts from outside, not from the build log:

```bash
curl -s https://pypi.org/pypi/dbabrain/X.Y.Z/json    # the files are there
docker pull ghcr.io/<owner>/dbabrain:X.Y.Z          # the image is there
docker run --rm ghcr.io/<owner>/dbabrain:X.Y.Z --help
```

---

## 4. What stops a commit or a release outright

Any one of these means stop, whatever else is green:

- The full suite is not run, or is run with `-k`.
- `git add -A` was used.
- A `Co-Authored-By` trailer is present.
- The export gate reports uncommitted files you did not intend to ship, or refuses on identifiers.
- CI is red on the commit being released.
- The clean-room run was skipped, or a scheduled command did not reach `status=done`.
- `docs/releases/vX.Y.Z.md` does not exist, or its **Verified** section contains claims that were
  not actually run.
- The operator has not said to release.

---

## 5. Why each rule is here

Every rule above is a scar. None is theoretical.

| Rule | What it prevents, and when it happened |
| --- | --- |
| No `Co-Authored-By` | A `claude` contributor on the operator's repository; removing it rewrote all history and 8 tags (2026-08-24) |
| Full suite, not `-k` | A time-of-day flake went red in CI in a file the change had not touched (2026-08-24) |
| Whole-tree secret scan | `secret-scan` red on a push; the fix took two more pushes to land. And CI's scan is **incremental** — it was green on a commit whose tree held 12 findings (2026-08-24) |
| Export gate before commit | Five unfinished modules from another session published to PyPI's repository (2026-08-24) |
| Identifier scan | Three real database names in shipped examples (2026-08-24) |
| Never `git add -A` | Another session's pending deletion committed inside an unrelated commit (2026-08-23) |
| Clean-room run | `init` shipped 3 metrics of 90; the daemon ran the wrong Python; the console showed nothing; the default schedule could not complete a cycle (v0.3.0–v0.3.4) |
| Release notes before the tag | Eight releases whose notes were written at tag time from memory |
| Check CI on the *released* SHA | Red runs from earlier commits read as current failures (2026-08-24) |
