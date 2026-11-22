"""``python -m dbabrain.jobs.status`` - the node's status report, now ``db.cli node-status`` (1.0.0).

The report moved to the shared ``db`` layer (``dbabrain.db.node_status``) so the master's ``control
worker-status`` runs a shared command, not the jobs app's CLI (rules R42). This name keeps answering
for whoever typed it, with the same flags.

    python -m dbabrain.jobs.status [--config config.json] [--json] [--no-metrics]
"""

from __future__ import annotations

import argparse
import sys

from dbabrain.db import node_status
from dbabrain.lib import secret_text

build_status = node_status.build_status


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--config", default=None)
    ap.add_argument("--json", action="store_true", help="Emit JSON instead of a table.")
    ap.add_argument("--no-metrics", action="store_true", help="Skip the metric_results freshness section.")
    # A PostgreSQL store needs its password decrypted from the secret store, so this needs the
    # passphrase like any other command; a SQLite store does not.
    secret_text.add_key_argument(ap)
    args = ap.parse_args(argv if argv is not None else sys.argv[1:])
    secret_text.set_key_env(args.key, args.key_base64)
    report = node_status.build_status(config_path=args.config, with_metrics=not args.no_metrics,
                                      key=secret_text.resolve_cli_key(args.key, args.key_base64))
    print(node_status.render(report, as_json=args.json))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
