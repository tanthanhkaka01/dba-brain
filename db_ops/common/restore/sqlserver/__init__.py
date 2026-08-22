"""SQL Server restore, as data and statements - no instance touched, no config read.

* :mod:`.chain` - which backups to restore and in what order, decided from headers and LSNs
  rather than from file names, and refusing a moment the logs do not cover.
* :mod:`.sql` - the RESTORE statements for a chosen chain, including ``STOPAT`` on the one log
  that carries it.

The caller supplies the headers and executes the statements. Splitting it this way is what lets
the part that decides a recovery's correctness be tested exhaustively, against chains that are
awkward on purpose, with no SQL Server in sight.
"""

from db_ops.common.restore.sqlserver.chain import (
    DIFF,
    FULL,
    LOG,
    BackupHeader,
    RestoreChain,
    RestoreChainError,
    parse_headers,
    select_chain,
)
from db_ops.common.restore.sqlserver.sql import build_restore_statements

__all__ = [
    "FULL", "DIFF", "LOG", "BackupHeader", "RestoreChain", "RestoreChainError",
    "parse_headers", "select_chain", "build_restore_statements",
]
