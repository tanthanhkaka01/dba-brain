"""A directory the bundle stops shipping must stop existing on the worker.

The upload writes files over files. It has never had an opinion about a directory that used to be
shipped and is not any more, so such a directory simply stays — and the deploy prints success.

On 2026-08-22 that cost a deploy. The built-in SQL had just moved into the package, so the bundle
stopped carrying ``assets/metrics``; the worker's copy from the previous layout survived; and the
asset lookup prefers the operator's tree over the package's. The image was correct and the worker
ran the *old* queries out of a directory nobody had shipped for a release. The fix at the time was
a human renaming four directories over SSH.

The class recurs with different names, because it is created by any move — which is exactly the
kind of change that touches no behaviour and so has nothing watching it.

Two decisions are worth stating, because both could reasonably have gone the other way:

**Top level of an owned directory only.** A whole directory vanishing is a structural change made
on the master. What happens *inside* ``assets/tasks`` is the bot writing SQL on the worker, which
the deploy is meant to mirror back rather than remove — so going one level deeper would turn a
fix into data loss.

**Moved aside, not deleted.** This runs against a live worker, driven by a diff with a bundle that
was built locally moments earlier. A build that assembled a thin bundle would otherwise turn one
bad build into a wiped worker. Moving is enough to fix the defect — the lookup stops finding the
directory — and it stays recoverable.
"""

from __future__ import annotations

from pathlib import Path

from db_ops.control import deploy


def _bundle(tmp_path: Path, layout: dict[str, list[str]]) -> Path:
    bundle = tmp_path / "db_ops_deploy"
    for owned, children in layout.items():
        for child in children:
            (bundle / owned / child).mkdir(parents=True)
    bundle.mkdir(exist_ok=True)
    return bundle


def test_a_directory_the_bundle_stopped_carrying_is_superseded(tmp_path) -> None:
    bundle = _bundle(tmp_path, {"assets": ["tasks", "sql_telegram_commands"]})
    worker = {"assets": ["tasks", "sql_telegram_commands", "metrics", "backup", "restore", "host"]}

    assert deploy.superseded_dirs(bundle, worker) == {
        "assets": ["backup", "host", "metrics", "restore"]
    }


def test_a_directory_the_bundle_still_carries_is_left_alone(tmp_path) -> None:
    """The bot writes SQL into `assets/tasks` on the worker; a deploy must not take it away."""
    bundle = _bundle(tmp_path, {"assets": ["tasks", "sql_telegram_commands"]})
    worker = {"assets": ["tasks", "sql_telegram_commands"]}

    assert deploy.superseded_dirs(bundle, worker) == {}


def test_a_bundle_that_carries_no_such_directory_supersedes_nothing(tmp_path) -> None:
    """An empty side of the comparison is a failed build, not a retirement.

    Treating it as one would wipe the worker's `assets/` because a copy step silently produced
    nothing — turning a bad build into data loss, which is the opposite of the point.
    """
    bundle = _bundle(tmp_path, {"data": ["reports"]})
    worker = {"assets": ["tasks", "metrics"], "data": ["reports"]}

    assert deploy.superseded_dirs(bundle, worker) == {}


def test_an_owned_directory_holding_only_files_supersedes_nothing(tmp_path) -> None:
    """`data/` is mostly loose JSON. No shipped subdirectory means no shape to enforce."""
    bundle = _bundle(tmp_path, {"assets": ["tasks"]})
    (bundle / "data").mkdir()
    (bundle / "data" / "db_instances.json").write_text("{}", encoding="utf-8")
    worker = {"data": ["archive"], "assets": ["tasks"]}

    assert deploy.superseded_dirs(bundle, worker) == {}


def test_only_the_owned_directories_are_ever_compared(tmp_path) -> None:
    """`logs/`, `runtime/` and `containers/` belong to the worker and are never in the diff.

    `containers/` in particular holds the lab databases' bind mounts, owned by the database users
    inside them. It is already excluded from the deploy's `chown`, for a failure that took out WAL
    archiving for a day; it must not come back in through a prune.
    """
    bundle = _bundle(tmp_path, {"assets": ["tasks"], "data": ["reports"]})
    worker = {"assets": ["tasks"], "data": ["reports"], "containers": ["pg_ha_01"], "logs": ["old"]}

    superseded = deploy.superseded_dirs(bundle, worker)

    assert "containers" not in superseded and "logs" not in superseded
    assert set(deploy.BUNDLE_OWNED_DIRS) == {"data", "assets"}


def test_the_worker_having_nothing_yet_is_not_an_error(tmp_path) -> None:
    """A first deploy onto a fresh host has no directories to compare against."""
    bundle = _bundle(tmp_path, {"assets": ["tasks"], "data": ["reports"]})

    assert deploy.superseded_dirs(bundle, {}) == {}


def test_the_prune_runs_after_the_upload_and_not_before() -> None:
    """Order is the property: pruning first would delete a directory the upload then rewrites.

    It also means a transfer that dies half way leaves the worker's directories where they were,
    rather than having already moved them aside for an upload that never landed.
    """
    import inspect

    source = inspect.getsource(deploy.copy_bundle)
    assert source.index("sftp_put_tree(") < source.index("prune_superseded_dirs(")
