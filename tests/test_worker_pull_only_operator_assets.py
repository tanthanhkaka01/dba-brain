"""A `--merge` deploy must not resurrect the directories the prune just removed.

`pull_sql_tree` mirrors the worker's `assets/**/*.sql` back to the master, so that task SQL an
operator registered through the bot survives the next deploy. It walked the whole tree.

That defeats `deploy.superseded_dirs` completely, and it did on 2026-08-22. The redeploy moved
`assets/backup`, `assets/host` and `assets/restore` aside and left `assets/metrics.superseded-...`
exactly where it was — because the merge, running minutes earlier in the same command, had pulled
that directory's 189 `.sql` files onto the *master*. By the time the bundle was assembled the
master genuinely carried the directory, so the prune was right to leave it alone. Two correct
mechanisms, and the pair makes a stale tree permanent.

The fix is to say what the pull is for. The worker's `assets/` should hold `tasks/` and
`sql_telegram_commands/` and nothing else; those are the kinds the package ships no built-in half
of (`OPERATOR_ASSET_KINDS`), which is exactly the same question. Anything else at that level is a
leftover, and a leftover is what the prune is there to remove.
"""

from __future__ import annotations

import stat as stat_mod

import pytest

from db_ops.control import worker_data
from db_ops.lib.paths import OPERATOR_ASSET_KINDS


class _Entry:
    def __init__(self, filename: str, *, directory: bool = False) -> None:
        self.filename = filename
        self.st_mode = (stat_mod.S_IFDIR | 0o755) if directory else (stat_mod.S_IFREG | 0o644)


class _Sftp:
    """The smallest thing that answers `listdir_attr` for a fixed tree."""

    def __init__(self, tree: dict[str, list[_Entry]]) -> None:
        self.tree = tree
        self.listed: list[str] = []

    def listdir_attr(self, path: str) -> list[_Entry]:
        self.listed.append(path)
        if path not in self.tree:
            raise IOError(path)
        return self.tree[path]


ROOT = "/opt/db_ops/assets"


def _worker_tree() -> _Sftp:
    return _Sftp({
        ROOT: [
            _Entry("tasks", directory=True),
            _Entry("sql_telegram_commands", directory=True),
            _Entry("metrics.superseded-20260822", directory=True),
            _Entry("README.md"),
        ],
        f"{ROOT}/tasks": [_Entry("sqlserver", directory=True)],
        f"{ROOT}/tasks/sqlserver": [_Entry("001_month_end.sql")],
        f"{ROOT}/sql_telegram_commands": [_Entry("list_targets.sql")],
        f"{ROOT}/metrics.superseded-20260822": [_Entry("001_instance_status.sql")],
    })


def test_a_leftover_directory_is_not_pulled_back_to_the_master() -> None:
    sftp = _worker_tree()

    pulled = worker_data._iter_remote_sql_files(sftp, ROOT)

    assert "metrics.superseded-20260822/001_instance_status.sql" not in pulled, (
        "pulling this recreates the directory on the master, so the next bundle ships it back "
        "and the deploy's prune correctly decides it is still carried"
    )


def test_the_task_sql_the_pull_exists_for_still_comes_back() -> None:
    """The whole point of `--merge`: SQL registered through the bot must survive a deploy."""
    sftp = _worker_tree()

    pulled = worker_data._iter_remote_sql_files(sftp, ROOT)

    assert pulled == [
        "sql_telegram_commands/list_targets.sql",
        "tasks/sqlserver/001_month_end.sql",
    ]


def test_a_leftover_directory_is_not_even_walked() -> None:
    """Descending into it would cost an SFTP round trip per subdirectory for nothing.

    It also keeps the reason visible: the directory is refused at the level where the decision is
    meaningful, not filtered out of the results afterwards.
    """
    sftp = _worker_tree()

    worker_data._iter_remote_sql_files(sftp, ROOT)

    assert f"{ROOT}/metrics.superseded-20260822" not in sftp.listed


@pytest.mark.parametrize("kind", sorted(OPERATOR_ASSET_KINDS))
def test_every_operator_asset_kind_is_walked(kind: str) -> None:
    """Read from `OPERATOR_ASSET_KINDS` rather than listed again here.

    A third kind added to the package's definition of "the operator owns this outright" has to
    start being pulled back on the same day, or the first operator to use it loses their SQL on
    the next deploy.
    """
    sftp = _Sftp({
        ROOT: [_Entry(kind, directory=True)],
        f"{ROOT}/{kind}": [_Entry("query.sql")],
    })

    assert worker_data._iter_remote_sql_files(sftp, ROOT) == [f"{kind}/query.sql"]
