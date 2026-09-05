"""db_ops control app — master-side operations (deploy, versioning, inventory).

Runs on the master (PC). The worker host defaults to the first ``worker`` node in
config.json, so most commands only need ``--user`` (and a password prompt).

    python -m db_ops.control.cli deploy --user <user>
    python -m db_ops.control.cli bump-version --part minor
    python -m db_ops.control.cli inventory-health --user <user>
    python -m db_ops.control.cli inventory-summary
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from db_ops.common.identifier_scan import IdentifierScanError
from db_ops.lib.secret_text import encrypt_secret_text_file, resolve_cli_key, resolve_key
from db_ops.control import deploy as deploy_ops, config_gate
from db_ops.control import inventory as inventory_ops
from db_ops.control import version as version_ops
from db_ops.control import worker_exec as worker_exec_ops
from db_ops.control import worker_status as worker_status_ops
from db_ops.control._support import (
    DB_OPS_ROOT,
    DEFAULT_CONTAINER,
    DEFAULT_REMOTE_DIR,
    default_worker_host,
    default_worker_password_ref,
    default_worker_user,
    resolve_password,
)

# Encrypt-secret-text defaults: read the gitignored plaintext source that now lives inside
# the tool (db_ops/secrets/) and write the committed encrypted store under db_ops/data/.
DEFAULT_PLAINTEXT_SECRET = DB_OPS_ROOT / "secrets" / "secret_text.json"
DEFAULT_ENCRYPTED_SECRET = DB_OPS_ROOT / "data" / "encrypted_secret_text.json"


def _secret_key_from_env() -> str | None:
    """Passphrase fallback for commands that can run without an explicit ``--key``.

    This was a hard-coded base64 constant until 2026-08-06. Base64 is encoding, not
    protection: a tracked default put the live store's passphrase into every checkout,
    every clone and every image layer built from this source, so anyone with read access
    to the repository could decrypt ``data/encrypted_secret_text.json``. The environment
    variable is the same one the daemon already exports to every child process, so an
    operator who can run a deploy already has it set.
    """
    return resolve_key(None).strip() or None


def _add_target(parser, *, require_user: bool = True) -> None:
    default_user = default_worker_user()
    parser.add_argument("--host", "--ip", dest="host", default=None,
                        help="Worker host (default: first worker in config.json).")
    # --user defaults to the worker's user in config.json; only required when none is set there.
    parser.add_argument("--user", default=default_user, required=require_user and not default_user,
                        help="SSH username (default: worker 'user' in config.json).")
    parser.add_argument("--password", default=None,
                        help="SSH password. If omitted, resolved from the secret store via the worker "
                             "'password_ref' + --key/--key-base64, else prompted.")
    parser.add_argument("--port", type=int, default=22, help="SSH port (default 22).")
    parser.add_argument("--key-base64", dest="key_base64", default=None,
                        help="Base64 secret passphrase (decrypts the SSH password ref and, for the "
                             "daemon, the runtime secrets).")
    parser.add_argument("--key", default=None, help="Secret passphrase (plaintext alternative to --key-base64).")


def _resolve_host(args) -> str:
    host = args.host or default_worker_host()
    if not host:
        raise SystemExit("No --host given and no worker node in config.json.")
    return host


def _add_drift_argument(parser) -> None:
    """How this run answers config drift between the store and ``data/``.

    A flag rather than only a prompt because a deploy runs from CI as well as from a keyboard, and
    an unattended run must be able to *declare* which side it trusts. What it may not do is
    proceed without saying — see :mod:`db_ops.control.config_gate`.
    """
    parser.add_argument(
        "--on-config-drift", dest="on_config_drift", default="ask",
        choices=list(config_gate.DRIFT_CHOICES),
        help="What to do when the runtime store and this master's data/ disagree: "
             "ask (default, needs a terminal), adopt (rebuild data/ from the store), "
             "keep (ship data/ and re-sync the store from it), abort.")


def _gate_config_drift(args) -> None:
    """Stop a deploy that would silently revert what the web console changed.

    Skipped only when the store cannot be opened at all — a master with no access to the runtime
    store has no drift to detect, and refusing to deploy from it would be a new failure rather
    than a caught one. That case is reported, never swallowed.
    """
    try:
        from db_ops.config import load_config
        from db_ops.db.config_store import ConfigStore
        from db_ops.lib import secret_text

        secret_text.set_key_env(getattr(args, "key", None), getattr(args, "key_base64", None))
        store = ConfigStore.from_config(load_config())
    except Exception as exc:  # noqa: BLE001 - see docstring.
        print(f"NOTE: config drift not checked ({exc}); the deploy will ship data/ as it is.",
              file=sys.stderr)
        return
    config_gate.resolve(store, decision=getattr(args, "on_config_drift", "ask"),
                        actor="deploy-gate")


def _refresh_encrypted_secret_store(*, key: str | None, key_base64: str | None) -> None:
    """Re-encrypt the plaintext secret source into the committed store before a deploy.

    A deploy always ships ``data/encrypted_secret_text.json``, so refresh it from the
    plaintext source first — that way ``deploy`` alone picks up secret edits without a
    separate ``encrypt-secret-text`` run. Falls back to ``DB_OPS_SECRET_KEY`` when no
    ``--key``/``--key-base64`` is given. Skipped (with a notice) when the plaintext source
    is absent, e.g. a tree that only carries the already-encrypted store.
    """
    resolved = resolve_cli_key(key, key_base64) if (key or key_base64) else _secret_key_from_env()
    if not resolved:
        # Say so rather than skipping quietly: without a key the deploy still ships the
        # encrypted store, so a secret edited in the plaintext source would reach the
        # worker as its previous value and the failure would only surface at connect time.
        print("NOTE: no --key/--key-base64 and no DB_OPS_SECRET_KEY; "
              "shipping the existing encrypted store without refreshing it.")
        return
    if not DEFAULT_PLAINTEXT_SECRET.exists():
        print(f"NOTE: plaintext secret source not found ({DEFAULT_PLAINTEXT_SECRET}); "
              "shipping the existing encrypted store as-is.")
        return
    count = encrypt_secret_text_file(DEFAULT_PLAINTEXT_SECRET, DEFAULT_ENCRYPTED_SECRET, resolved)
    print(f"Refreshed encrypted secret store: {count} secrets -> {DEFAULT_ENCRYPTED_SECRET}")


def parse_args(argv):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)

    bump = sub.add_parser("bump-version", help="Bump db_ops/__init__.py __version__.")
    bump.add_argument("--part", choices=["major", "minor", "patch"], default="patch")
    bump.add_argument("--set", dest="set_to", default=None, help="Set the version explicitly.")
    bump.add_argument("--dry-run", action="store_true")

    build = sub.add_parser("build-image", help="Build the image + deploy bundle locally.")
    build.add_argument("--platform", default="linux/amd64")
    build.add_argument("--no-cache", action="store_true")
    build.add_argument("--skip-build", action="store_true")
    # The drift gate opens the runtime store, which on this tree is PostgreSQL behind a secret.
    # Without these, build-image could only check drift when the passphrase happened to be in the
    # environment — and would quietly skip the check when it was not.
    build.add_argument("--key-base64", dest="key_base64", default=None,
                       help="Base64 secret passphrase, for the config-drift check.")
    build.add_argument("--key", default=None,
                       help="Secret passphrase (plaintext alternative to --key-base64).")
    _add_drift_argument(build)

    copy = sub.add_parser("copy", help="Copy the deploy bundle to the worker over SFTP.")
    _add_target(copy)
    copy.add_argument("--remote-dir", default=DEFAULT_REMOTE_DIR)

    start = sub.add_parser("start-daemon", help="Load the image and (re)start the worker daemon.")
    _add_target(start)
    start.add_argument("--remote-dir", default=DEFAULT_REMOTE_DIR)
    start.add_argument("--container", default=DEFAULT_CONTAINER)
    start.add_argument("--node-role", default="worker", choices=["master", "worker"])

    dep = sub.add_parser("deploy", help="build-image -> copy -> start-daemon in one shot.")
    _add_target(dep)
    dep.add_argument("--remote-dir", default=DEFAULT_REMOTE_DIR)
    dep.add_argument("--container", default=DEFAULT_CONTAINER)
    dep.add_argument("--node-role", default="worker", choices=["master", "worker"])
    dep.add_argument("--platform", default="linux/amd64")
    dep.add_argument("--no-cache", action="store_true")
    dep.add_argument("--skip-build", action="store_true")
    _add_drift_argument(dep)
    # Default changed on 2026-08-11: the master is the source of truth, and a deploy ships it.
    #
    # It was the other way round from 2.24.00, after a deploy destroyed config registered through
    # the bot. That merge also made the
    # master un-editable for the fields the worker owns: `metrics.metric_overrides` is
    # worker-owned, so editing a severity map on the master and deploying silently restored the
    # worker's copy — over the master's file, before the build, so the edit was lost on both
    # sides. Making a deliberate master-side change required knowing that and working around it.
    #
    # The old failure is now the operator's to avoid by passing --merge; the new one is not
    # avoidable at all. deploy() prints what will be overwritten either way.
    dep.add_argument("--merge", dest="merge_worker", action="store_true",
                     help="Merge worker-added config (SQL tasks/targets, Telegram groups/users, "
                          "docker db connections) and .sql scripts back into the master BEFORE "
                          "building. Use it when the bot has registered things on the worker since "
                          "the last deploy. Without it the deploy is master -> worker and DELETES "
                          "anything registered through the bot since the last deploy.")
    dep.add_argument("--no-merge-worker", dest="merge_worker", action="store_false",
                     help="Deprecated no-op kept so existing runbook lines keep working; this is "
                          "now the default.")
    dep.set_defaults(merge_worker=False)

    exp = sub.add_parser(
        "export-public",
        help="Write the filtered public tree: what dba-brain receives, and nothing else.")
    exp.add_argument("target", help="Directory to write. Must be empty.")
    exp.add_argument("--plan-only", action="store_true",
                     help="Report what would be written and change nothing.")
    exp.add_argument("--force", action="store_true",
                     help="Empty the target first. Without it a non-empty target is refused.")
    exp.add_argument("--allow-binary", action="append", default=[], metavar="PATH",
                     help="Ship one non-text file, by repo-relative path. Repeatable.")
    exp.add_argument("--allow-inside-git", action="store_true",
                     help="Write into a repository that has a remote. Refused by default: a public "
                          "tree in a checkout that can push is one command from an early release. "
                          "Use it deliberately when updating a repository that is already public.")
    exp.add_argument("--skip-scan", action="store_true",
                     help="Do not run check-identifiers over the result. For debugging only.")

    inv = sub.add_parser("inventory-health", help="Build health overlay on the worker, fetch, merge canonical JSON.")
    _add_target(inv)
    inv.add_argument("--container", default=DEFAULT_CONTAINER)
    inv.add_argument("--days", type=int, default=2)
    inv.add_argument("--date", default=None)
    inv.add_argument("--inventory", default=str(inventory_ops.DEFAULT_INVENTORY))
    inv.add_argument("--snapshot-dir", default=str(inventory_ops.DEFAULT_SNAPSHOT_DIR))
    inv.add_argument("--container-runtime", default=inventory_ops.DEFAULT_CONTAINER_RUNTIME)
    inv.add_argument("--host-runtime", default=inventory_ops.DEFAULT_HOST_RUNTIME)
    inv.add_argument("--dry-run", action="store_true")

    enc = sub.add_parser("encrypt-secret-text",
                         help="Encrypt the plaintext secret source (db_ops/secrets/secret_text.json) into "
                              "the committed db_ops/data/encrypted_secret_text.json store.")
    enc.add_argument("--source", default=str(DEFAULT_PLAINTEXT_SECRET),
                     help=f"Plaintext secrets JSON (default {DEFAULT_PLAINTEXT_SECRET}).")
    enc.add_argument("--dest", default=str(DEFAULT_ENCRYPTED_SECRET),
                     help=f"Encrypted output JSON (default {DEFAULT_ENCRYPTED_SECRET}).")
    enc.add_argument("--key", default=None, help="Encryption passphrase (plaintext alternative to --key-base64).")
    enc.add_argument("--key-base64", "--key_base64", dest="key_base64", default=None,
                     help="Base64-encoded UTF-8 encryption passphrase. Defaults to the built-in project key.")

    summ = sub.add_parser("inventory-summary", help="Render *-summary.md from the canonical inventory JSON.")
    summ.add_argument("--inventory", default=str(inventory_ops.DEFAULT_INVENTORY))
    summ.add_argument("--output-dir", default=str(inventory_ops.DEFAULT_SNAPSHOT_DIR))
    summ.add_argument("--date", default=None)

    flow = sub.add_parser("inventory-workflow",
                          help="Run inventory-health then inventory-summary in one shot (--user defaults to worker user in config.json).")
    _add_target(flow)
    flow.add_argument("--container", default=DEFAULT_CONTAINER)
    flow.add_argument("--days", type=int, default=2)
    flow.add_argument("--date", default=None)
    flow.add_argument("--inventory", default=str(inventory_ops.DEFAULT_INVENTORY))
    flow.add_argument("--snapshot-dir", default=str(inventory_ops.DEFAULT_SNAPSHOT_DIR))
    flow.add_argument("--output-dir", default=str(inventory_ops.DEFAULT_SNAPSHOT_DIR))
    flow.add_argument("--container-runtime", default=inventory_ops.DEFAULT_CONTAINER_RUNTIME)
    flow.add_argument("--host-runtime", default=inventory_ops.DEFAULT_HOST_RUNTIME)
    flow.add_argument("--dry-run", action="store_true")

    ws = sub.add_parser("worker-status",
                        help="Check the worker daemon: container up?, version, and per-app last-run "
                             "status/due + metric freshness (read-only).")
    _add_target(ws)
    ws.add_argument("--container", default=DEFAULT_CONTAINER)
    ws.add_argument("--remote-dir", default=DEFAULT_REMOTE_DIR,
                    help="Worker bundle dir (host). Reports are read from <remote-dir>/runtime/reports.")
    ws.add_argument("--json", dest="as_json", action="store_true", help="Emit the in-container status as JSON.")
    ws.add_argument("--no-metrics", dest="no_metrics", action="store_true", help="Skip the metric-freshness section.")

    wr = sub.add_parser("worker-run",
                        help="Run an arbitrary command inside the worker container, e.g. "
                             "worker-run --key-base64 K -- python -m db_ops.reports.cli inventory-workflow --days 7 --beauty 1")
    _add_target(wr)
    wr.add_argument("--container", default=DEFAULT_CONTAINER)
    wr.add_argument("cmd", nargs=argparse.REMAINDER,
                    help="The command to run in the worker container (put it after `--`).")

    from db_ops.control import worker_data as _wd
    pp = sub.add_parser("worker-pull-data-config",
                        help="Copy updated data/ config files from the worker back to the master.")
    _add_target(pp)
    pp.add_argument("--from-worker-path", dest="from_worker_path", default=_wd.DEFAULT_WORKER_DATA_PATH,
                    help=f"Worker data dir on the host (default {_wd.DEFAULT_WORKER_DATA_PATH}).")
    pp.add_argument("--to-master-path", dest="to_master_path", default=_wd.DEFAULT_MASTER_DATA_PATH,
                    help="Master data dir to copy into (default the repo's data/).")
    pp.add_argument("--files", nargs="+", default=None,
                    help="Specific file names to pull (default: the docker-db connection registry).")
    pp.add_argument("--all-json", dest="all_json", action="store_true",
                    help="Pull every *.json in the worker data dir (excludes the secret store).")
    pp.add_argument("--include-secrets", dest="include_secrets", action="store_true",
                    help="Also pull the encrypted secret store (off by default). On its own this "
                         "OVERWRITES the master's store with the worker's — see --merge-secrets.")
    pp.add_argument("--merge-secrets", dest="merge_secrets", action="store_true",
                    help="Merge the worker's secret store into the master's instead of overwriting "
                         "it, then synchronize the merged refs into the master's plaintext source: "
                         "master-only refs are kept, and a ref whose value differs between stores "
                         "is reported as a conflict and nothing is written. "
                         "Needs --key/--key-base64 to decrypt both stores.")
    pp.add_argument("--plaintext-secret-path", dest="plaintext_secret_path",
                    default=_wd.DEFAULT_PLAINTEXT_SECRET_PATH,
                    help="Plaintext secret source synchronized by --merge-secrets "
                         f"(default {_wd.DEFAULT_PLAINTEXT_SECRET_PATH}).")
    pp.add_argument("--overwrite", action="store_true", help="Overwrite files that already exist on the master.")
    pp.add_argument("--writeback-config", dest="writeback_config", action="store_true",
                    help="Preset: pull the worker-owned config files (sql_commands.json, sql_targets.json, "
                         "app_commands.json) with overwrite so the master picks up runtime edits.")
    pp.add_argument("--include-sql", dest="include_sql", action="store_true",
                    help="Also mirror the worker's sql/ tree (*.sql task scripts) back to the master.")
    pp.add_argument("--dry-run", action="store_true", help="Print what would be copied without copying.")

    wc = sub.add_parser("worker-create-db-docker",
                        help="Run sre.cli create-db-docker inside the worker, then optionally pull config back.")
    _add_target(wc)
    wc.add_argument("--container", default=DEFAULT_CONTAINER)
    wc.add_argument("--name", required=True)
    wc.add_argument("--engine", required=True)
    wc.add_argument("--version", required=True)
    wc.add_argument("--mode", default="single")
    wc.add_argument("--replicas", type=int, default=None)
    wc.add_argument("--host-port", dest="host_port", type=int, default=None,
                    help="Default: the engine's own port (postgres 5432, mysql 3306, mssql 1433).")
    wc.add_argument("--password-env", dest="password_env", default=None,
                    help="Secret ref for the password. Default: <NAME>_PASSWORD.")
    wc.add_argument("--password-text", dest="password_text", default=None,
                    help="Password value to store under --password-env in the encrypted secret "
                         "store before provisioning (visible in the process list while it runs).")
    wc.add_argument("--containers-dir", dest="containers_dir", default=None)
    wc.add_argument("--network-subnet", dest="network_subnet", default=None,
                    help="CIDR for the lab's compose network. Default: a /24 derived from --name "
                         "inside 172.30.0.0/16, so it can never land on a range the estate routes.")
    wc.add_argument("--worker-host", dest="worker_host", default=None)
    wc.add_argument("--force", action="store_true")
    wc.add_argument("--dry-run", action="store_true")
    wc.add_argument("--no-register", dest="register", action="store_false")
    wc.add_argument("--pull-config", dest="pull_config", action="store_true",
                    help="After creating, pull the updated data config back to the master.")

    return parser.parse_args(argv)


def _export_public_command(args) -> int:
    """``export-public`` — produce the tree that becomes `dba-brain`.

    The last gate before something becomes permanent, so it is loud and it refuses easily. Two
    things it will not do: write over a non-empty directory, because an export written over a
    previous run ships whatever survived from it; and finish quietly on a tree that still names a
    real host, because the identifier scan runs against the **copy** rather than against the source
    it came from.
    """
    from pathlib import Path

    from db_ops.control import export_public
    from db_ops.lib.paths import TOOL_ROOT

    root = Path(TOOL_ROOT)
    target = Path(args.target).expanduser().resolve()

    unplanned = export_public.unplanned_paths(root)
    if unplanned:
        print(
            "These top-level paths are in neither PUBLIC_PATHS nor PRIVATE_PATHS, so nobody has "
            "decided about them. They will NOT be exported:",
            file=sys.stderr,
        )
        for name in unplanned:
            print(f"  {name}", file=sys.stderr)
        print("Add each to db_ops/lib/distribution.py.", file=sys.stderr)
        print("", file=sys.stderr)

    try:
        if args.plan_only:
            plan = export_public.build_plan(root, allow_binaries=frozenset(args.allow_binary))
        else:
            plan = export_public.export(
                root, target, allow_binaries=frozenset(args.allow_binary), force=args.force,
                allow_inside_git=args.allow_inside_git,
            )
    except export_public.ExportError as exc:
        print("export refused:", file=sys.stderr)
        print(str(exc), file=sys.stderr)
        return 1

    verb = "would write" if args.plan_only else "wrote"
    where = "" if args.plan_only else f" to {target}"
    print(f"{verb} {plan.file_count} file(s){where}")
    print(f"  packages left behind: {', '.join(sorted(plan.skipped_packages)) or 'none'}")
    print(f"  component docs left behind: {len(plan.skipped_docs)}")
    print(f"  tests for those packages left behind: {len(plan.skipped_tests)}")

    # Uncommitted files are the export's sharpest edge. It copies the working tree, so anything
    # another session happens to be part-way through goes out with whatever change is exported
    # next — which is how five half-written modules reached the public repository on 2026-08-24,
    # caught only by a duplicate-definition guard in CI. A file nobody has committed is a file
    # nobody has said is finished. Loud, and not fatal: shipping a work in progress on purpose is
    # the operator's call to make, not this command's.
    if plan.uncommitted:
        print("")
        print(f"  !! {len(plan.uncommitted)} file(s) in this copy are NOT COMMITTED in the source:")
        for relative in plan.uncommitted[:15]:
            print(f"       {relative}")
        if len(plan.uncommitted) > 15:
            print(f"       ... and {len(plan.uncommitted) - 15} more")
        print("     Commit them, or stash them, before publishing this tree. If another session is")
        print("     mid-edit, what ships is whatever they had saved at this instant.")

    if args.plan_only or args.skip_scan:
        return 0

    # No inventory means no search terms, and the scanner refuses rather than reporting a tree
    # clean on the strength of having looked for nothing. That refusal is right, and in the public
    # tree it is also the *normal* case — there are no real instances there to derive terms from.
    # Since `control` started shipping in v0.3.2, a reader running this in their own checkout met
    # it as a traceback after a successful copy, which reads as a failed export. It is neither a
    # pass nor a failure: the check did not run, and saying so is the honest report.
    try:
        outcome = export_public.scan_exported_tree(target)
    except IdentifierScanError as exc:
        print("")
        print(f"identifier scan SKIPPED: {exc}")
        print(
            "The copy is written. Nothing has been verified about it - run the scan from a "
            "checkout whose data/db_instances.json names the machines you need kept out."
        )
        return 0
    print("")
    print(
        f"identifier scan of the exported tree: {outcome['hits']} hit(s) in "
        f"{outcome['files_with_findings']} file(s)"
    )
    if outcome["hits"]:
        for item in outcome["files"][:20]:
            total = item["certain"] + item["likely"]
            print(f"  {total:4}  {item['file']}   {', '.join(item['terms'][:4])}")
        export_public.discard(target)
        print("", file=sys.stderr)
        print(
            "This tree names real machines and must not be published. G-02 is not met.",
            file=sys.stderr,
        )
        print(f"{target} was removed: a refused tree must not be left on disk.", file=sys.stderr)
        return 1
    print("clean - no configured identifier appears in the exported tree.")
    _print_review_tier(outcome)
    return 0


def _print_review_tier(outcome: dict) -> None:
    """Say what the scan matched but did not refuse over.

    `review` is the tier for a configured name that is also an ordinary word - this estate has
    databases whose names are English nouns - and it is excluded from `hits` on purpose, because a
    gate that refuses over the word "inventory" in `inventory_report.py` is a gate people route
    around. It was excluded from the *printout* too, which is different and was wrong: on
    2026-09-05 a customer database name shipped in the published package while this line said
    "clean", and the scan had matched it all along.

    So it prints, and it does not refuse: a number and the files, for a person to read once.
    """
    unknown = outcome.get("unrecognised_addresses") or {}
    if unknown:
        print(f"  plus {len(unknown)} address(es) no configuration names - the category the "
              "inventory-derived half cannot find, so nothing else will report them:")
        for literal, files in list(unknown.items())[:10]:
            print(f"    {literal}  ({len(files)} file(s), e.g. {files[0]})")
        if len(unknown) > 10:
            print(f"    ... and {len(unknown) - 10} more")
    review = int(outcome.get("review") or 0)
    if not review:
        return
    files = outcome.get("review_only_files") or []
    print(f"  plus {review} review-tier match(es) in {len(files)} file(s) - configured names that "
          "are also ordinary words. Not a refusal; read them once:")
    for name in files[:10]:
        print(f"    {name}")
    if len(files) > 10:
        print(f"    ... and {len(files) - 10} more")


def main(argv=None) -> int:
    try:
        return _run(parse_args(argv if argv is not None else sys.argv[1:]))
    except config_gate.ConfigDriftAbort as exc:
        # A refusal, not a crash: the operator is being told what to decide, and a traceback
        # would bury the three words that matter under a stack.
        print(f"\nERROR: {exc}", file=sys.stderr)
        return 3


def _run(args) -> int:

    if args.command == "export-public":
        return _export_public_command(args)

    if args.command == "bump-version":
        version_ops.bump_version(part=args.part, set_to=args.set_to, dry_run=args.dry_run)
        return 0
    if args.command == "build-image":
        # Before the bundle is staged, not after: the bundle is a copy of data/, so a check that
        # ran later would be checking something already committed to.
        _gate_config_drift(args)
        deploy_ops.build_image(platform=args.platform, no_cache=args.no_cache, skip_build=args.skip_build)
        return 0
    if args.command == "inventory-summary":
        # The inventory JSON is *produced* by `inventory-health`, so on a fresh install it is
        # legitimately absent — that is a sequence a reader has not run yet, not a broken toolkit.
        # It used to surface as a `FileNotFoundError` traceback out of `pathlib.open`, which names
        # the file and nothing about what makes one.
        inventory_path = Path(args.inventory)
        if not inventory_path.is_file():
            print(f"ERROR: no inventory at {inventory_path}.", file=sys.stderr)
            print("This file is generated, not written by hand. Build it first:", file=sys.stderr)
            print("  db-ops control inventory-health      # collects, then merges", file=sys.stderr)
            print("  db-ops control inventory-workflow    # health + summary in one step",
                  file=sys.stderr)
            return 2
        inventory_ops.build_inventory_summary(inventory=args.inventory, output_dir=args.output_dir, date=args.date)
        return 0
    if args.command == "encrypt-secret-text":
        try:
            key = resolve_cli_key(args.key, args.key_base64)
        except ValueError as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            return 2
        key = key or _secret_key_from_env()
        if not key:
            # Refuse rather than guess. Encrypting with the wrong passphrase produces a
            # store that decrypts nowhere, and the round-trip check inside
            # encrypt_secret_text_file cannot catch it — it verifies against the same key.
            print("ERROR: no passphrase. Pass --key/--key-base64 or set DB_OPS_SECRET_KEY.",
                  file=sys.stderr)
            return 2
        try:
            count = encrypt_secret_text_file(args.source, args.dest, key)
        except Exception as exc:  # noqa: BLE001 - CLI failure path.
            print(f"ERROR: {exc}", file=sys.stderr)
            return 1
        print(f"Encrypted {count} secrets -> {args.dest}")
        return 0

    # remaining commands need the worker host + password. The password is taken from
    # --password, else decrypted from the secret store via the worker password_ref +
    # --key/--key-base64, else prompted.
    host = _resolve_host(args)
    password = resolve_password(args.password, host=host, user=args.user,
                                key_base64=args.key_base64, key=args.key,
                                password_ref=default_worker_password_ref())

    if args.command == "copy":
        deploy_ops.copy_bundle(host=host, user=args.user, password=password, port=args.port,
                               remote_dir=args.remote_dir)
    elif args.command == "start-daemon":
        deploy_ops.start_daemon(host=host, user=args.user, password=password, port=args.port,
                                remote_dir=args.remote_dir, container=args.container,
                                key_base64=args.key_base64, key=args.key, node_role=args.node_role)
    elif args.command == "deploy":
        # Refresh the encrypted secret store from the plaintext source, then deploy. So
        # `deploy --key-base64 ...` ships the latest secrets without a separate encrypt step.
        _gate_config_drift(args)
        _refresh_encrypted_secret_store(key=args.key, key_base64=args.key_base64)
        deploy_ops.deploy(host=host, user=args.user, password=password, port=args.port,
                          remote_dir=args.remote_dir, container=args.container,
                          key_base64=args.key_base64, key=args.key, node_role=args.node_role,
                          platform=args.platform, no_cache=args.no_cache, skip_build=args.skip_build,
                          merge_worker=args.merge_worker)
    elif args.command == "inventory-health":
        inventory_ops.run_inventory_health(host=host, user=args.user, password=password, port=args.port,
                                           container=args.container, days=args.days, date=args.date,
                                           container_runtime=args.container_runtime, host_runtime=args.host_runtime,
                                           inventory=args.inventory, snapshot_dir=args.snapshot_dir,
                                           dry_run=args.dry_run)
    elif args.command == "inventory-workflow":
        inventory_ops.run_inventory_workflow(host=host, user=args.user, password=password, port=args.port,
                                             container=args.container, days=args.days, date=args.date,
                                             container_runtime=args.container_runtime, host_runtime=args.host_runtime,
                                             inventory=args.inventory, snapshot_dir=args.snapshot_dir,
                                             output_dir=args.output_dir, dry_run=args.dry_run)
    elif args.command == "worker-status":
        return worker_status_ops.run_worker_status(host=host, user=args.user, password=password, port=args.port,
                                                   container=args.container, remote_dir=args.remote_dir,
                                                   as_json=args.as_json, no_metrics=args.no_metrics,
                                                   key_base64=args.key_base64, key=args.key)
    elif args.command == "worker-run":
        return worker_exec_ops.run_worker_command(host=host, user=args.user, password=password, port=args.port,
                                                  container=args.container, command=args.cmd)
    elif args.command == "worker-pull-data-config":
        from db_ops.control import worker_data
        files = args.files
        all_json = args.all_json
        overwrite = args.overwrite
        if args.writeback_config:
            # Preset: pull exactly the worker-owned config files, overwriting the master copy.
            files = list(files or []) + [f for f in worker_data.WORKER_OWNED_CONFIG_FILES if f not in (files or [])]
            all_json = False
            overwrite = True
        # Merging a store means reading it: without the passphrase the master cannot decrypt its
        # own file, so say that up front instead of failing halfway through the pull.
        include_secrets = args.include_secrets or args.merge_secrets
        secret_key = resolve_cli_key(args.key, args.key_base64) if args.merge_secrets else None
        if args.merge_secrets and not secret_key:
            print("ERROR: --merge-secrets needs --key or --key-base64 to decrypt both stores.",
                  file=sys.stderr)
            return 2
        try:
            rc = worker_data.pull_data_config(
                host=host, user=args.user, password=password, port=args.port,
                from_worker_path=args.from_worker_path, to_master_path=args.to_master_path,
                files=files, all_json=all_json, include_secrets=include_secrets,
                merge_secrets=args.merge_secrets, secret_key=secret_key,
                plaintext_secret_path=args.plaintext_secret_path,
                overwrite=overwrite, dry_run=args.dry_run)
        except worker_data.SecretMergeConflict as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            return 3
        if rc == 0 and args.include_sql:
            print("\n=== worker-pull-sql-tree ===", flush=True)
            rc = worker_data.pull_sql_tree(
                host=host, user=args.user, password=password, port=args.port,
                overwrite=overwrite or args.writeback_config, dry_run=args.dry_run)
        return rc
    elif args.command == "worker-create-db-docker":
        from db_ops.control import worker_data
        # Omitted port / secret ref are left to the in-container CLI, which derives them from
        # the engine (5432/3306/1433) and the instance name (<NAME>_PASSWORD).
        sre_args = ["--name", args.name, "--engine", args.engine, "--version", args.version,
                    "--mode", args.mode]
        if args.host_port is not None:
            sre_args += ["--host-port", str(args.host_port)]
        if args.password_env:
            sre_args += ["--password-env", args.password_env]
        if args.password_text:
            sre_args += ["--password-text", args.password_text]
        if args.replicas is not None:
            sre_args += ["--replicas", str(args.replicas)]
        if args.containers_dir:
            sre_args += ["--containers-dir", args.containers_dir]
        if args.network_subnet:
            sre_args += ["--network-subnet", args.network_subnet]
        if args.worker_host:
            sre_args += ["--worker-host", args.worker_host]
        if args.force:
            sre_args.append("--force")
        if args.dry_run:
            sre_args.append("--dry-run")
        if not args.register:
            sre_args.append("--no-register")
        # Forward the key so the in-container command can resolve the password from the secret store.
        if args.key_base64:
            sre_args += ["--key-base64", args.key_base64]
        elif args.key:
            sre_args += ["--key", args.key]
        pull_kwargs = {"overwrite": True, "dry_run": args.dry_run}
        return worker_data.create_db_docker_on_worker(
            host=host, user=args.user, password=password, port=args.port,
            container=args.container, sre_args=sre_args,
            pull_config=args.pull_config, pull_kwargs=pull_kwargs)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
