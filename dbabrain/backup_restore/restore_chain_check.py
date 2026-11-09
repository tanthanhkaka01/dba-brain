"""The share-driven restore's chain, held to its LSNs before anything is restored (0.36.0, 1.250).

``restore-by-id`` checks a SQL Server chain with the LSNs ``list-backup-files`` reads over its own
connection (0.35.0, gap G10). The share-driven workflow (``restore_database``) reaches the target
only through ``sqlcmd`` - over WinRM, often with Windows authentication - so that connection does
not exist for it, and a missing ``.trn`` surfaced at ``RESTORE LOG``, after the FULL and the DIFF
had been applied, as an LSN in an error, leaving the target database in RESTORING.

Here the target reads ``RESTORE HEADERONLY`` of every file of the chosen chain in one ``sqlcmd``
batch and prints one marker line per backup set; the chain is then judged by the same
``lib.integrity.sqlserver_chain_break`` as ``restore-by-id``. Reading the header needs nothing the
restore itself does not (the same channel, the same files, the certificate already ensured).

``RESTORE HEADERONLY`` grows columns with each release (2012, 2014, 2022...), and ``INSERT ... EXEC``
needs the exact count. Rather than a table per version, the batch starts below the 2008 R2 count and
adds a column on each failure until the first file fits - the CATCH sees only the last error, and
under ``INSERT ... EXEC`` a column count that does not match surfaces as 3013 ("RESTORE HEADERONLY is
terminating abnormally") after the 213, the same number an unreadable file gives, so a file that
fits no count is reported with that last error;
every column is ``nvarchar(4000)``, to which every value of the header converts. The columns read
are among the first 2008 R2 already had, whose positions have not moved since.

Anything that cannot be read - sqlcmd failing, a header error, no marker line - is **not judged**: a
check that guesses is worse than none, and the restore then finds what it finds as it always did.
"""

from __future__ import annotations

from typing import Any

#: Where the batch starts counting RESTORE HEADERONLY's columns - below 2008 R2's (about 52), so a
#: release with fewer than thought still fits: the count only grows.
BASE_COLUMNS = 45
#: Enough for every release so far (2022 has 58 or so); a file that fits none is not judged.
MAX_COLUMNS = 90

#: 1-based positions of the columns read, unchanged since 2008 R2.
POSITIONS = {"backup_type": 3, "position": 6, "database": 10, "first_lsn": 14, "last_lsn": 15,
             "checkpoint_lsn": 16, "differential_base_lsn": 48}

MARKER = "DBABRAIN_HEAD|"
ERROR_MARKER = "DBABRAIN_HEAD_ERR|"


def _sql_string(value: str) -> str:
    return str(value).replace("'", "''")


def build_header_sql(paths: list[str]) -> str:
    """One batch reading every file's header: ``DBABRAIN_HEAD|<index>|<type>|<position>|<database>|
    <first>|<last>|<checkpoint>|<diff base>`` per backup set, ``DBABRAIN_HEAD_ERR|<index>|<number>|
    <message>`` for a file that could not be read."""
    values = ",\n    ".join(f"({index}, N'{_sql_string(path)}')" for index, path in enumerate(paths))
    picked = " + N'|' + ".join(f"COALESCE(c{POSITIONS[key]}, N'')" for key in (
        "backup_type", "position", "database", "first_lsn", "last_lsn", "checkpoint_lsn",
        "differential_base_lsn"))
    # Two quotes inside the dynamic text: it is a string inside a string.
    return f"""
SET NOCOUNT ON;
DECLARE @files TABLE (i int NOT NULL, path nvarchar(4000) NOT NULL);
INSERT INTO @files (i, path) VALUES
    {values};
CREATE TABLE #h (c1 nvarchar(4000) NULL);
DECLARE @n int = 1, @fits bit = 0, @first nvarchar(4000), @sql nvarchar(max), @error nvarchar(400);
WHILE @n < {BASE_COLUMNS}
BEGIN
    SET @n = @n + 1;
    SET @sql = N'ALTER TABLE #h ADD c' + CONVERT(nvarchar(10), @n) + N' nvarchar(4000) NULL';
    EXEC (@sql);
END;
SELECT @first = path FROM @files WHERE i = 0;
WHILE @fits = 0 AND @n <= {MAX_COLUMNS}
BEGIN
    BEGIN TRY
        SET @sql = N'RESTORE HEADERONLY FROM DISK = N''' + REPLACE(@first, N'''', N'''''') + N'''';
        INSERT INTO #h EXEC (@sql);
        SET @fits = 1;
    END TRY
    BEGIN CATCH
        SELECT @error = CONVERT(nvarchar(10), ERROR_NUMBER()) + N'|'
            + REPLACE(REPLACE(LEFT(ERROR_MESSAGE(), 300), CHAR(13), N' '), CHAR(10), N' ');
        IF ERROR_NUMBER() NOT IN (213, 3013)
            SET @n = {MAX_COLUMNS} + 1;
        ELSE
        BEGIN
            SET @n = @n + 1;
            SET @sql = N'ALTER TABLE #h ADD c' + CONVERT(nvarchar(10), @n) + N' nvarchar(4000) NULL';
            EXEC (@sql);
        END
    END CATCH
END;
IF @fits = 0
    SELECT N'{ERROR_MARKER}0|' + COALESCE(@error, N'0|no header');
IF @fits = 1
BEGIN
    DECLARE @i int, @path nvarchar(4000);
    DECLARE files CURSOR LOCAL FAST_FORWARD FOR SELECT i, path FROM @files ORDER BY i;
    OPEN files;
    FETCH NEXT FROM files INTO @i, @path;
    WHILE @@FETCH_STATUS = 0
    BEGIN
        IF @i > 0
        BEGIN
            TRUNCATE TABLE #h;
            BEGIN TRY
                SET @sql = N'RESTORE HEADERONLY FROM DISK = N''' + REPLACE(@path, N'''', N'''''') + N'''';
                INSERT INTO #h EXEC (@sql);
            END TRY
            BEGIN CATCH
                SELECT N'{ERROR_MARKER}' + CONVERT(nvarchar(10), @i) + N'|' + CONVERT(nvarchar(10), ERROR_NUMBER())
                    + N'|' + REPLACE(REPLACE(LEFT(ERROR_MESSAGE(), 300), CHAR(13), N' '), CHAR(10), N' ');
            END CATCH
        END;
        SELECT N'{MARKER}' + CONVERT(nvarchar(10), @i) + N'|' + {picked} FROM #h;
        FETCH NEXT FROM files INTO @i, @path;
    END;
    CLOSE files;
    DEALLOCATE files;
END;
DROP TABLE #h;
""".strip()


def _lsn(text: str) -> int:
    try:
        return int(float(text.strip() or 0))
    except ValueError:
        return 0


def parse_headers(stdout: str, paths: list[str]) -> tuple[list[dict[str, Any] | None], list[str]]:
    """Each path's header (its first backup set - the one a ``RESTORE`` without ``FILE =`` reads),
    in the shape ``sqlserver_chain_break`` takes, or None where none was printed; and the errors
    printed, as ``"<file>: Msg <n> <text>"``."""
    heads: list[dict[str, Any] | None] = [None] * len(paths)
    errors: list[str] = []
    for line in (stdout or "").splitlines():
        if ERROR_MARKER in line:
            parts = line[line.index(ERROR_MARKER) + len(ERROR_MARKER):].strip().split("|", 2)
            if len(parts) == 3 and parts[0].isdigit() and int(parts[0]) < len(paths):
                errors.append(f"{paths[int(parts[0])]}: Msg {parts[1]} {parts[2].strip()}")
            continue
        if MARKER not in line:
            continue
        parts = line[line.index(MARKER) + len(MARKER):].strip().split("|")
        if len(parts) != 8 or not parts[0].isdigit():
            continue
        index = int(parts[0])
        position = parts[2].strip()
        # A file holding several backup sets is read by RESTORE at FILE = 1: that set is its header.
        if index >= len(paths) or (heads[index] is not None
                                   and (position != "1" or heads[index]["position"] == "1")):
            continue
        heads[index] = {
            "position": position,
            "path": paths[index], "backup_type": parts[1].strip(), "database": parts[3].strip(),
            "first_lsn": _lsn(parts[4]), "last_lsn": _lsn(parts[5]),
            "checkpoint_lsn": _lsn(parts[6]), "differential_base_lsn": _lsn(parts[7]),
        }
    return heads, errors


def chain_break(full: str, diff: str | None, logs: list[str], stdout: str) -> tuple[str, str]:
    """``(break, not_judged)``: the first break of the chain in words naming its file, or why it was
    not judged. Both empty when it holds."""
    from dbabrain.lib.integrity import sqlserver_chain_break

    paths = [full, *([diff] if diff else []), *logs]
    heads, errors = parse_headers(stdout, paths)
    if errors:
        return "", "a header could not be read - " + "; ".join(errors[:3])
    if any(head is None for head in heads):
        missing = [path for path, head in zip(paths, heads) if head is None]
        return "", f"no header was printed for {len(missing)} file(s), first {missing[0]}"
    # RESTORE HEADERONLY's BackupType: 1 database, 5 differential, 2 log. A file of another kind than
    # its place in the chain means the columns were not where they are read - not judged, never a
    # refusal made on misread numbers.
    expected = ["1", *(["5"] if diff else []), *["2"] * len(logs)]
    wrong = [head["path"] for head, kind in zip(heads, expected) if head["backup_type"] != kind]
    if wrong:
        return "", f"a header did not read as its kind ({wrong[0]}), so the LSNs were not trusted"
    full_head = heads[0]
    diff_head = heads[1] if diff else None
    log_heads = heads[2 if diff else 1:]
    return sqlserver_chain_break(full_head, diff_head, log_heads), ""
