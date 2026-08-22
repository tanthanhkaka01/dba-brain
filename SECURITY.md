# Security Policy

DBA Brain runs against production databases and holds the credentials to reach them. A bug here is
not a broken web page — it is a route into someone's estate. Reports are read seriously and
quickly.

## Reporting a vulnerability

**Do not open a public issue for a security problem.**

1. **Preferred:** GitHub private vulnerability reporting — on the repository, go to
   **Security → Report a vulnerability**. Only the maintainers see it, and it carries a private
   fork for the fix and an advisory when it is done.
2. **Fallback**, if you do not have a GitHub account or the form is unavailable: email
   **tanthanhkaka01@gmail.com** with `DBA BRAIN SECURITY` in the subject.

Please include: the version (`dbabrain --version` or the image tag), what an attacker gains, the
smallest reproduction you have, and — if the report concerns a live system — nothing that
identifies it. A redacted report is more useful than a delayed one.

### What to expect

| | |
| --- | --- |
| Acknowledgement | within 3 working days |
| First assessment | within 10 working days |
| Fix or mitigation | as fast as the severity warrants; you are told the plan and the timing |
| Credit | offered by default; say if you would rather stay anonymous |

This is a small project maintained by one person. The timings above are honest commitments, not a
funded SLA — if something slips, you will be told rather than left waiting.

Please give a reasonable window to ship a fix before disclosing publicly, and tell us when you
intend to publish. If a fix is taking too long, say so; a deadline is fair.

## Supported versions

| Version | Supported |
| --- | --- |
| Latest `0.x` minor | ✅ Fixes land here |
| Older `0.x` minors | ❌ Upgrade to the latest minor |

While the project is in `0.x`, only the most recent minor receives security fixes. This changes at
`1.0.0`, and this table changes with it.

## Scope

**In scope**

- Anything that discloses credentials, secrets, or the contents of the encrypted secret store.
- Anything that lets input — a config file, a Telegram message, a SQL result, a tool call —
  execute code, run unintended SQL, or reach a host it was not authorised to reach.
- Privilege escalation between the tool's own permission levels.
- Bypassing an approval, allow-list, or confirmation gate.
- Anything that writes secret material to logs, reports, or error messages.

**Out of scope**

- Vulnerabilities in a database engine, an OS, or a driver — report those to their vendor. If DBA
  Brain *uses* such a component insecurely, that part is in scope.
- Findings that require a level of access which already lets you do the same damage directly (an
  operator who already holds the secret passphrase, root on the host running the tool).
- Missing hardening that has no exploitable consequence, without a working scenario.
- Automated scanner output with no analysis attached.

## Security model — what you should know before running it

- **DBA Brain sends nothing anywhere by itself.** No telemetry, no analytics, no usage reporting,
  no phone-home. It talks only to the databases, hosts, and delivery channels you configure. If a
  future version were ever to change this, it would be opt-in, announced in the changelog, and
  documented here.
- **Secrets are encrypted at rest** and are decrypted with a passphrase supplied at runtime by
  environment variable or CLI flag. The passphrase itself is never written to a config file, a
  log, or the store.
- **Credentials are named, not embedded.** Configuration refers to a credential by name; the value
  is resolved from the environment or the encrypted store at use time.
- **The tool needs only the permissions you grant it.** Metric collection is read-only; anything
  that writes is a separate, explicitly configured capability. Grant least privilege on the
  monitored instances and the tool loses nothing it needs.
- **It is designed to run without outbound internet access.** Nothing in the core requires a
  connection beyond the systems you point it at.

## Hardening checklist for operators

- Give the tool its own least-privilege login on every monitored instance. Do not reuse a DBA
  account.
- Keep the passphrase out of shell history and out of any file in version control. Supply it
  through the environment or a secret manager.
- Restrict who can read the configuration directory: it names your hosts, your accounts, and your
  targets even when it holds no secret values.
- Pin the image by version, not `latest`, and pin by digest where it matters.
- Run the container as the non-root user it ships with, and mount the configuration read-only
  where your workflow allows it.
- Turn on the audit trail and keep it: it is what answers "what did this tool do to my database,
  and when".
