"""Deleting ONE backup file whose name, path, bytes and checksum are all stated - and all true.

0.37.0. The delete at the source after a verified repository copy (1.246) was ``delete-files``: a
list of paths, one ``rm`` each, with the checksum proven by the caller *before* the call and nothing
on the host itself held to it. The operator, 2026-11-10 (translated): *deleting this file is very
dangerous - the code must be very careful and very well tested before anything is deleted*:

1. never one command that deletes two files or more;
2. never one command that deletes a folder that still holds a file - even one file must fail;
3. each command deletes one file only, or one empty folder only;
4. a file delete carries the file's full facts - name, path, bytes, checksum - and deletes only
   when every one of them is right;
5. the tests catch every case, the forbidden ones included;
6. it works on Windows and Ubuntu, over SSH and over WinRM.

So there are two commands, and each has exactly one thing it may remove:

* ``delete-verified-file`` - one regular file. The request names the file (``file_name``), where it
  is (``path``), its ``size_bytes`` and its ``sha256``, and the folder it must be inside
  (``must_be_under``). On the host, in ONE script, the file is looked at (not a link, not a folder,
  not a device), its size held to ``size_bytes``, its bytes hashed and held to ``sha256``, its size
  and modification time read again to prove nothing wrote to it while it was hashed - and only then
  is it removed, by a call that removes exactly one file (``rm --`` without ``-r``, .NET
  ``File.Delete``), and looked for again afterwards. Any fact that differs refuses the delete and the
  answer says what was found.
* ``delete-empty-folder`` - one folder with nothing in it. Its entries are counted on the host
  (hidden ones included) and one is enough to refuse; the removal itself is the platform's
  non-recursive one (``rmdir``, .NET ``Directory.Delete(path, false)``), which refuses a folder with
  anything in it on its own, so a file that lands between the count and the removal still stops it.

**Every refusal that does not need the host is made before the host is reached**: one path only (a
request carrying ``paths`` or any field this command does not know is refused - a typo in a field
name must not turn a check off), absolute, no wildcard, no ``.`` or ``..``, no control character, the
name equal to the path's last part, the checksum 64 hex characters, the size a whole number, the path
strictly inside ``must_be_under`` (which is required, and is never itself deleted).

**One script, not stat-then-delete in two round trips.** Two calls can disagree - the file a second
writer replaced between them is not the file that was checked. The host decides and reports in one
breath; this module only reads the word it printed.

**Not finding the file is ``not_found``** - nothing was deleted, which is the end state the caller
wanted; it is reported apart from ``deleted`` so a run says which of the two it did.

The script travels the way every host command does (:func:`dbabrain.common.hostcmd.run`): PowerShell
for a ``windows`` runtime - wrapped in ``-EncodedCommand`` locally and over SSH, sent as it is over
WinRM - and POSIX ``sh`` for ``linux`` / ``docker`` / ``k8s`` (no bash-only syntax: a container's
``sh`` may be dash or busybox). Windows hashes through .NET, as ``file-checksum`` does, so a 2008 R2
host with PowerShell 2.0 runs it too.

**Oracle: this removes the file, not RMAN's record of it** - the same warning as ``delete-file``; the
offload never releases an Oracle source (``lib.offload`` refuses the flag there).

**The forced delete of one file nobody recorded** (0.38.0, 1.329 - :func:`delete_forced_file`,
``delete-file`` with ``"force": true``). The old ``delete-file`` / ``delete-files`` are gone: a list
of paths, one ``rm`` each, nothing held to anything. What stays is a way to clear a stray file - a
partial of a killed copy, a file the catalog never knew - which has no stored facts to be held to.
The operator, 2026-11-11 (translated): *the old delete commands refuse to run; but with force a file
can still be deleted, to clear junk too - one file at a time: two files or more fail outright, a
folder that still holds a file fails, and to delete many, make an array and call the delete once per
file.* So it is the same one script on the host, with the two comparisons left out and nothing else:
not a link, not a folder, not a device; the bytes hashed and the size and time read again around the
hash; one non-recursive delete of that one file; and the answer says what was deleted - its bytes
and its sha256 - so the removal of a file nobody recorded is itself on record. A request carrying a
list is refused before the host is reached.
"""

from __future__ import annotations

import os
import re
import shlex
from typing import Any

from dbabrain.common.filechecksum import HASH_TIMEOUT_SECONDS
from dbabrain.common.hostcmd import WINDOWS, Host, HostCommandError, open_session, parse_host, run
from dbabrain.lib import errors
from dbabrain.lib.integrity import SHA256_HEX as _SHA256
from dbabrain.lib.powershell import quote_powershell as _ps_quote
from dbabrain.lib.verified_cleanup import last_part as _last_part

#: Outcomes. Only ``deleted``, ``not_found`` and (a dry run's) ``skipped`` are success.
DELETED = "deleted"
NOT_FOUND = "not_found"
SKIPPED = "skipped"
#: The file (or folder) is there and is not what the request says - nothing was removed.
REFUSED = "refused"
#: Everything matched and the platform's delete did not remove it (held open, no permission).
FAILED = "failed"
SUCCESS_STATUSES = (DELETED, NOT_FOUND, SKIPPED)

#: Refused inside a path: a pattern names files nobody listed.
WILDCARDS = ("*", "?", "[", "]")

FILE_FIELDS = frozenset({"host", "path", "file_name", "size_bytes", "sha256", "must_be_under", "dry_run",
                         "timeout_seconds"})
#: The forced delete (``delete-file``): no stored facts to state - and ``force`` must be true.
FORCED_FIELDS = frozenset({"host", "path", "must_be_under", "force", "dry_run", "timeout_seconds"})
FOLDER_FIELDS = frozenset({"host", "path", "must_be_under", "dry_run", "timeout_seconds"})
#: The list forms (0.37.0): one host, a list of items, each item the single command's own facts.
FILES_FIELDS = frozenset({"host", "files", "dry_run", "timeout_seconds"})
FILE_ITEM_FIELDS = frozenset({"path", "file_name", "size_bytes", "sha256", "must_be_under"})
FOLDERS_FIELDS = frozenset({"host", "folders", "dry_run", "timeout_seconds"})
FOLDER_ITEM_FIELDS = frozenset({"path", "must_be_under"})
#: An item after the first one that was not deleted: never sent to the host.
NOT_ATTEMPTED = "not_attempted"
#: A list longer than this is refused: a restored cluster is a few thousand files, and a list of a
#: hundred thousand is a folder named by mistake, not a cleanup.
MAX_ITEMS = 20000


class DeleteVerifiedError(errors.RequestError):
    """The request is refused before anything is run on the host."""


# --------------------------------------------------------------------------- #
# The two commands
# --------------------------------------------------------------------------- #
def delete_verified_file(request: dict[str, Any]) -> dict[str, Any]:
    """Delete one file when its name, path, size and sha256 are all what the request states."""
    _known_fields(request, FILE_FIELDS, "delete-verified-file")
    host = _host(request)
    facts = _file_facts(request, host=host)
    dry_run = _flag(request.get("dry_run"), "dry_run")
    timeout = _timeout(request.get("timeout_seconds"))
    row = _delete_one_file(host, facts, dry_run=dry_run, timeout=timeout, session=None)
    return {"file": row, "deleted": row["status"] == DELETED, "dry_run": dry_run}


def delete_forced_file(request: dict[str, Any]) -> dict[str, Any]:
    """Delete ONE file nobody recorded - measured on the host in the script that deletes it.

    ``force`` must be ``true``: without it this refuses and names the commands that hold a file to
    its stored facts. A list (``paths``, ``files``) is refused - one file per command, always; a
    folder is refused by the script (``DIR``) whether or not it is empty. The answer carries what
    was deleted: ``size_bytes_found`` and ``sha256_found``.
    """
    if isinstance(request, dict) and request.get("force") is not True:
        raise DeleteVerifiedError(
            'delete-file is refused: a backup file is deleted by delete-verified-file, which holds it to '
            'its name, path, bytes and sha256. To clear ONE stray file nobody recorded, state '
            '"force": true - the host then measures the file, deletes it and reports its bytes and sha256.')
    _known_fields(request, FORCED_FIELDS, "delete-file")
    host = _host(request)
    path, root = _validated_path(request, host=host)
    dry_run = _flag(request.get("dry_run"), "dry_run")
    timeout = _timeout(request.get("timeout_seconds"))
    script = (windows_file_script(path, size=None, sha256=None, dry_run=dry_run) if host.is_windows
              else posix_file_script(path, size=None, sha256=None, dry_run=dry_run))
    row = _forced_row(_run_and_read(host, script, timeout=timeout), path=path, root=root)
    return {"file": row, "deleted": row["status"] == DELETED, "dry_run": dry_run, "forced": True}


def delete_verified_files(request: dict[str, Any], *, open_session: Any = None) -> dict[str, Any]:
    """A list of files, each deleted exactly as :func:`delete_verified_file` deletes one.

    The operator, 2026-11-10 (translated): *delete it, but by an array of files* - a restored test
    cluster is thousands of files, and one process and one SSH login per file is hours. What the list
    changes is the transport and nothing else: every item states its own name, path, bytes, sha256
    and fence; every item is checked and removed by its own script on the host - one ``rm`` of one
    file - over one connection. The whole list is validated before the host is reached, so one
    malformed item refuses all of them; and the first item that is not deleted (refused, failed)
    stops the list - the items after it are answered ``not_attempted`` and never sent.
    """
    _known_fields(request, FILES_FIELDS, "delete-verified-files", single=False)
    host = _host(request)
    items = _items(request.get("files"), "files", FILE_ITEM_FIELDS)
    facts = [_file_facts(item, host=host, where=f"files[{index}]") for index, item in enumerate(items)]
    _no_duplicates([f["path"] for f in facts], host=host, field="files")
    dry_run = _flag(request.get("dry_run"), "dry_run")
    timeout = _timeout(request.get("timeout_seconds"))

    def one(session: Any, item: dict[str, Any]) -> dict[str, Any]:
        return _delete_one_file(host, item, dry_run=dry_run, timeout=timeout, session=session)

    def untouched(item: dict[str, Any], reason: str) -> dict[str, Any]:
        return {"path": item["path"], "file_name": item["name"], "must_be_under": item["root"],
                "size_bytes": item["size"], "sha256": item["sha256"], "size_bytes_found": None,
                "sha256_found": None, "status": NOT_ATTEMPTED, "reason": reason}

    rows = _each(host, facts, one, untouched, opener=open_session)
    return {"files": rows, **_tally(rows), "dry_run": dry_run}


def delete_empty_folders(request: dict[str, Any], *, open_session: Any = None) -> dict[str, Any]:
    """A list of folders, each deleted exactly as :func:`delete_empty_folder` deletes one - in the
    order given (deepest first is the caller's to state: a parent named before its child is refused
    as not empty, and stops the list)."""
    _known_fields(request, FOLDERS_FIELDS, "delete-empty-folders", single=False)
    host = _host(request)
    items = _items(request.get("folders"), "folders", FOLDER_ITEM_FIELDS)
    facts = []
    for index, item in enumerate(items):
        path, root = _validated_path(item, host=host, where=f"folders[{index}]")
        facts.append({"path": path, "root": root})
    _no_duplicates([f["path"] for f in facts], host=host, field="folders")
    dry_run = _flag(request.get("dry_run"), "dry_run")
    timeout = _timeout(request.get("timeout_seconds"))

    def one(session: Any, item: dict[str, Any]) -> dict[str, Any]:
        script = (windows_folder_script(item["path"], dry_run=dry_run) if host.is_windows
                  else posix_folder_script(item["path"], dry_run=dry_run))
        return _folder_row(_run_and_read(host, script, timeout=timeout, session=session),
                           path=item["path"], root=item["root"])

    def untouched(item: dict[str, Any], reason: str) -> dict[str, Any]:
        return {"path": item["path"], "must_be_under": item["root"], "entries": None,
                "status": NOT_ATTEMPTED, "reason": reason}

    rows = _each(host, facts, one, untouched, opener=open_session)
    return {"folders": rows, **_tally(rows), "dry_run": dry_run}


def _delete_one_file(host: Host, facts: dict[str, Any], *, dry_run: bool, timeout: int,
                     session: Any) -> dict[str, Any]:
    path, size, sha256 = facts["path"], facts["size"], facts["sha256"]
    script = (windows_file_script(path, size=size, sha256=sha256, dry_run=dry_run) if host.is_windows
              else posix_file_script(path, size=size, sha256=sha256, dry_run=dry_run))
    found = _run_and_read(host, script, timeout=timeout, session=session)
    return _file_row(found, path=path, name=facts["name"], size=size, sha256=sha256, root=facts["root"])


def _each(host: Host, items: list[dict[str, Any]], one: Any, untouched: Any, *,
          opener: Any) -> list[dict[str, Any]]:
    """``one(session, item)`` for each item in order, over one connection; the first row that is not
    a success stops it, and every later item is answered by ``untouched`` - never sent."""
    session = None
    rows: list[dict[str, Any]] = []
    if not host.is_local:
        try:
            session = (opener or open_session)(host)
        except HostCommandError as exc:
            return [untouched(item, f"not attempted: {exc}") for item in items]
    try:
        for index, item in enumerate(items):
            row = one(session, item)
            rows.append(row)
            if row["status"] not in SUCCESS_STATUSES:
                reason = f"not attempted: the list stopped at {row['path']} ({row['status']})"
                rows.extend(untouched(rest, reason) for rest in items[index + 1:])
                break
    finally:
        if session is not None:
            session.close()
    return rows


def _tally(rows: list[dict[str, Any]]) -> dict[str, Any]:
    stopped = next((row for row in rows if row["status"] not in SUCCESS_STATUSES), None)
    return {"deleted": sum(1 for row in rows if row["status"] == DELETED),
            "not_found": sum(1 for row in rows if row["status"] == NOT_FOUND),
            "skipped": sum(1 for row in rows if row["status"] == SKIPPED),
            "not_attempted": sum(1 for row in rows if row["status"] == NOT_ATTEMPTED),
            "complete": stopped is None,
            "stopped_at": None if stopped is None else {"path": stopped["path"], "status": stopped["status"],
                                                        "reason": stopped["reason"]}}


def delete_empty_folder(request: dict[str, Any]) -> dict[str, Any]:
    """Delete one folder that holds nothing at all; one entry in it refuses."""
    _known_fields(request, FOLDER_FIELDS, "delete-empty-folder")
    host = _host(request)
    path, root = _validated_path(request, host=host)
    dry_run = _flag(request.get("dry_run"), "dry_run")
    timeout = _timeout(request.get("timeout_seconds"))

    script = (windows_folder_script(path, dry_run=dry_run) if host.is_windows
              else posix_folder_script(path, dry_run=dry_run))
    found = _run_and_read(host, script, timeout=timeout)
    row = _folder_row(found, path=path, root=root)
    return {"folder": row, "deleted": row["status"] == DELETED, "dry_run": dry_run}


# --------------------------------------------------------------------------- #
# The scripts - one each, run on the host, ending in one tab-separated line
# --------------------------------------------------------------------------- #
# The first word of the line is the verdict; the rest are what the host read. Tokens:
#   DELETED / WOULD  size sha256          - every fact matched (WOULD: a dry run, nothing removed)
#   MISSING                               - nothing at that path
#   DIR / FILE / LINK / NOT_REGULAR       - the path is not the kind of thing this command removes
#   SIZE size / SHA size sha256           - the file is there and differs
#   CHANGED size                          - it changed while it was being hashed
#   NOT_EMPTY count                       - a folder with that many entries
#   UNREADABLE / FAILED  ...  reason      - the host could not read it, or could not remove it


def posix_file_script(path: str, *, size: int | None, sha256: str | None, dry_run: bool) -> str:
    """POSIX ``sh``: check one file, and remove it only when every fact matches.

    ``size`` / ``sha256`` ``None`` (the forced delete, both together): that comparison is left out -
    the file is still looked at, hashed, read again around the hash, and its bytes and sha256 printed.
    """
    p = shlex.quote(path)
    size_check = "if" if size is None else (
        f"if [ \"$size\" != \"{size}\" ]; then printf 'SIZE\\t%s\\n' \"$size\"; elif")
    sha_check = "" if sha256 is None else (
        f"elif [ \"$h\" != \"{sha256}\" ]; then printf 'SHA\\t%s\\t%s\\n' \"$size\" \"$h\"; ")
    act = ("printf 'WOULD\\t%s\\t%s\\n' \"$size\" \"$h\"" if dry_run else
           # `rm --` with ONE operand and no -r / -f: it cannot take a folder or a second file, and
           # a failure is not silenced. Looked for again afterwards rather than trusting the exit.
           "rm -- \"$p\" 2>/dev/null; "
           "if [ -e \"$p\" ] || [ -L \"$p\" ]; then printf 'FAILED\\t%s\\t%s\\tstill there after rm\\n' \"$size\" \"$h\"; "
           "else printf 'DELETED\\t%s\\t%s\\n' \"$size\" \"$h\"; fi")
    return (
        f"p={p}; "
        # A link is refused before anything else: -d and -f follow it, so a link to a folder or to a
        # file elsewhere would otherwise be judged by its target and the link itself removed.
        "if [ -L \"$p\" ]; then printf 'LINK\\n'; "
        "elif [ -d \"$p\" ]; then printf 'DIR\\n'; "
        "elif [ ! -e \"$p\" ]; then printf 'MISSING\\n'; "
        "elif [ ! -f \"$p\" ]; then printf 'NOT_REGULAR\\n'; "
        "elif ! s1=$(stat -c '%s %Y %i' -- \"$p\" 2>&1); then printf 'UNREADABLE\\t\\t\\t%s\\n' \"$s1\"; "
        "else size=${s1%% *}; "
        # Hashed from stdin, so the name never reaches sha256sum's output (it escapes some names).
        f"{size_check} ! h=$(sha256sum < \"$p\" 2>&1); then printf 'UNREADABLE\\t%s\\t\\t%s\\n' \"$size\" \"$h\"; "
        "else h=${h%% *}; s2=$(stat -c '%s %Y %i' -- \"$p\" 2>/dev/null); "
        "if [ \"$s1\" != \"$s2\" ]; then printf 'CHANGED\\t%s\\t%s\\n' \"${s2%% *}\" \"$h\"; "
        f"{sha_check}"
        f"else {act}; fi; fi; fi"
    )


def windows_file_script(path: str, *, size: int | None, sha256: str | None, dry_run: bool) -> str:
    """PowerShell (2.0 and up): check one file, and remove it only when every fact matches
    (``size`` / ``sha256`` ``None``: that comparison left out - see :func:`posix_file_script`)."""
    size_check = "" if size is None else f'if($len -ne {size}){{"SIZE`t$len";return}};'
    sha_check = "" if sha256 is None else f"if($h -ne '{sha256}'){{\"SHA`t$len`t$h\";return}};"
    act = ('"WOULD`t$len`t$h"' if dry_run else
           # .NET File.Delete removes exactly one file and throws on a folder; Remove-Item would take a
           # folder with -Recurse and prompts without it. Looked for again afterwards: a file held open
           # can survive a delete that reported nothing.
           'try{[System.IO.File]::Delete($i.FullName)}'
           'catch{"FAILED`t$len`t$h`t$($_.Exception.Message)";return};'
           'if(Test-Path -LiteralPath $p){"FAILED`t$len`t$h`tstill there after the delete"}'
           'else{"DELETED`t$len`t$h"}')
    return (
        "&{$ErrorActionPreference='Stop';"
        f"$p={_ps_quote(path)};"
        'if(-not(Test-Path -LiteralPath $p)){"MISSING";return};'
        "try{$i=Get-Item -LiteralPath $p -Force}catch{\"UNREADABLE`t`t`t$($_.Exception.Message)\";return};"
        # A reparse point (symbolic link, junction) is refused before it is judged by its target.
        'if(($i.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0){"LINK";return};'
        'if($i.PSIsContainer){"DIR";return};'
        "$len=[int64]$i.Length;$mt=$i.LastWriteTimeUtc.Ticks;"
        f"{size_check}"
        "try{$sha=[System.Security.Cryptography.SHA256]::Create();$s=[System.IO.File]::OpenRead($i.FullName);"
        "try{$h=[BitConverter]::ToString($sha.ComputeHash($s)).Replace('-','').ToLower()}finally{$s.Close()}}"
        "catch{\"UNREADABLE`t$len`t`t$($_.Exception.Message)\";return};"
        "$i.Refresh();"
        'if(([int64]$i.Length -ne $len) -or ($i.LastWriteTimeUtc.Ticks -ne $mt)){"CHANGED`t$($i.Length)`t$h";return};'
        f"{sha_check}"
        f"{act}}}"
    )


def posix_folder_script(path: str, *, dry_run: bool) -> str:
    """POSIX ``sh``: remove one folder only when it holds nothing - hidden entries count."""
    p = shlex.quote(path)
    act = ("printf 'WOULD\\n'" if dry_run else
           # rmdir refuses a folder with anything in it by itself - the second lock on the same door.
           "if ! e=$(rmdir -- \"$p\" 2>&1); then printf 'FAILED\\t\\t\\t%s\\n' \"$e\"; "
           "elif [ -e \"$p\" ]; then printf 'FAILED\\t\\t\\tstill there after rmdir\\n'; "
           "else printf 'DELETED\\n'; fi")
    return (
        f"p={p}; "
        "if [ -L \"$p\" ]; then printf 'LINK\\n'; "
        "elif [ ! -e \"$p\" ]; then printf 'MISSING\\n'; "
        "elif [ ! -d \"$p\" ]; then printf 'FILE\\n'; "
        # `ls -A`: every entry but . and .. - dot files count. A folder that cannot be listed is
        # UNREADABLE, never "empty".
        "elif ! e=$(ls -A -- \"$p\" 2>&1); then printf 'UNREADABLE\\t\\t\\t%s\\n' \"$e\"; "
        "elif [ -n \"$e\" ]; then printf 'NOT_EMPTY\\t%s\\n' \"$(printf '%s\\n' \"$e\" | wc -l | tr -d ' ')\"; "
        f"else {act}; fi"
    )


def windows_folder_script(path: str, *, dry_run: bool) -> str:
    """PowerShell (2.0 and up): remove one folder only when it holds nothing - hidden entries count."""
    act = ('"WOULD"' if dry_run else
           # Directory.Delete(path, $false): never recursive, throws on a folder that is not empty.
           'try{[System.IO.Directory]::Delete($i.FullName,$false)}'
           'catch{"FAILED`t`t`t$($_.Exception.Message)";return};'
           'if(Test-Path -LiteralPath $p){"FAILED`t`t`tstill there after the delete"}else{"DELETED"}')
    return (
        "&{$ErrorActionPreference='Stop';"
        f"$p={_ps_quote(path)};"
        'if(-not(Test-Path -LiteralPath $p)){"MISSING";return};'
        "try{$i=Get-Item -LiteralPath $p -Force}catch{\"UNREADABLE`t`t`t$($_.Exception.Message)\";return};"
        'if(($i.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0){"LINK";return};'
        'if(-not $i.PSIsContainer){"FILE";return};'
        # GetFileSystemEntries lists hidden and system entries too (Get-ChildItem would not, without -Force).
        "try{$n=[System.IO.Directory]::GetFileSystemEntries($i.FullName).Length}"
        "catch{\"UNREADABLE`t`t`t$($_.Exception.Message)\";return};"
        'if($n -gt 0){"NOT_EMPTY`t$n";return};'
        f"{act}}}"
    )


# --------------------------------------------------------------------------- #
# Reading the host's answer
# --------------------------------------------------------------------------- #
def _run_and_read(host: Host, script: str, *, timeout: int, session: Any = None) -> dict[str, Any]:
    try:
        result = run(host, script, timeout=timeout, session=session)
    except HostCommandError as exc:
        return {"token": "FAILED", "reason": str(exc)}
    lines = [line for line in (result.get("stdout") or "").splitlines() if line.strip()]
    if len(lines) != 1:
        # Exactly one line is the contract; anything else is an answer this module cannot vouch for.
        stderr = (result.get("stderr") or "").strip()
        return {"token": "FAILED", "reason": (stderr or f"unexpected output {(result.get('stdout') or '')!r}")[:1000]}
    parts = lines[0].rstrip("\r").split("\t")
    parts += [""] * (4 - len(parts))
    return {"token": parts[0].strip(), "size": parts[1].strip(), "sha256": parts[2].strip().lower(),
            "reason": "\t".join(parts[3:]).strip()[:1000]}


def _file_row(found: dict[str, Any], *, path: str, name: str, size: int, sha256: str, root: str) -> dict[str, Any]:
    token = found["token"]
    size_found = int(found["size"]) if str(found.get("size") or "").isdigit() else None
    sha_found = found.get("sha256") or None
    status, reason = {
        "DELETED": (DELETED, ""),
        "WOULD": (SKIPPED, "dry_run: every fact matched, nothing deleted"),
        "MISSING": (NOT_FOUND, "no file at that path - nothing deleted"),
        "DIR": (REFUSED, "the path is a folder - this command deletes one file only"),
        "LINK": (REFUSED, "the path is a link - never followed, never deleted"),
        "NOT_REGULAR": (REFUSED, "the path is not a regular file"),
        "SIZE": (REFUSED, f"{size_found} bytes on the host, not the {size} stated - nothing deleted"),
        "SHA": (REFUSED, "the checksum on the host is not the one stated - nothing deleted"),
        "CHANGED": (REFUSED, "the file changed while it was being hashed - nothing deleted"),
        "UNREADABLE": (REFUSED, f"could not be read: {found.get('reason') or ''}".strip()),
        "FAILED": (FAILED, found.get("reason") or "the delete did not remove it"),
    }.get(token, (FAILED, f"unexpected answer from the host: {token!r}"))
    if status == DELETED and (size_found != size or sha_found != sha256):
        # The script only deletes on a match; an answer saying otherwise is a broken script, and is
        # said as loudly as one.
        status, reason = FAILED, (f"the host reported a delete of {size_found} bytes / {sha_found}, "
                                  f"not the {size} / {sha256} stated")
    return {"path": path, "file_name": name, "must_be_under": root, "size_bytes": size, "sha256": sha256,
            "size_bytes_found": size_found, "sha256_found": sha_found, "status": status, "reason": reason}


def _forced_row(found: dict[str, Any], *, path: str, root: str) -> dict[str, Any]:
    """The forced delete's answer: no facts were stated, so what the host measured is the record."""
    token = found["token"]
    size_found = int(found["size"]) if str(found.get("size") or "").isdigit() else None
    sha_found = found.get("sha256") or None
    status, reason = {
        "DELETED": (DELETED, ""),
        "WOULD": (SKIPPED, "dry_run: one regular file, measured, nothing deleted"),
        "MISSING": (NOT_FOUND, "no file at that path - nothing deleted"),
        "DIR": (REFUSED, "the path is a folder - this command deletes one file only, never a folder"),
        "LINK": (REFUSED, "the path is a link - never followed, never deleted"),
        "NOT_REGULAR": (REFUSED, "the path is not a regular file"),
        "CHANGED": (REFUSED, "the file changed while it was being hashed - nothing deleted"),
        "UNREADABLE": (REFUSED, f"could not be read: {found.get('reason') or ''}".strip()),
        "FAILED": (FAILED, found.get("reason") or "the delete did not remove it"),
    }.get(token, (FAILED, f"unexpected answer from the host: {token!r}"))
    if status == DELETED and (size_found is None or not _SHA256.match(sha_found or "")):
        # The script prints what it removed; a delete it cannot describe is a broken script.
        status, reason = FAILED, "the host reported a delete without the file's bytes and sha256"
    return {"path": path, "file_name": _last_part(path), "must_be_under": root,
            "size_bytes_found": size_found, "sha256_found": sha_found, "status": status, "reason": reason}


def _folder_row(found: dict[str, Any], *, path: str, root: str) -> dict[str, Any]:
    token = found["token"]
    entries = int(found["size"]) if token == "NOT_EMPTY" and str(found.get("size") or "").isdigit() else None
    status, reason = {
        "DELETED": (DELETED, ""),
        "WOULD": (SKIPPED, "dry_run: the folder is empty, nothing deleted"),
        "MISSING": (NOT_FOUND, "no folder at that path - nothing deleted"),
        "FILE": (REFUSED, "the path is a file - this command deletes one empty folder only"),
        "LINK": (REFUSED, "the path is a link - never followed, never deleted"),
        "NOT_EMPTY": (REFUSED, f"the folder holds {entries} entr{'y' if entries == 1 else 'ies'} - "
                               "only an empty folder is deleted"),
        "UNREADABLE": (REFUSED, f"could not be listed: {found.get('reason') or ''}".strip()),
        "FAILED": (FAILED, found.get("reason") or "the delete did not remove it"),
    }.get(token, (FAILED, f"unexpected answer from the host: {token!r}"))
    return {"path": path, "must_be_under": root, "entries": entries, "status": status, "reason": reason}


# --------------------------------------------------------------------------- #
# The refusals made here, before the host is reached
# --------------------------------------------------------------------------- #
def _known_fields(request: Any, allowed: frozenset[str], command: str, *, single: bool = True) -> None:
    if not isinstance(request, dict):
        raise DeleteVerifiedError(f"{command} takes one JSON object.")
    unknown = sorted(set(request) - allowed)
    if single and ("paths" in unknown or "files" in unknown or "folders" in unknown):
        raise DeleteVerifiedError(f"{command} deletes ONE {'file' if 'file' in command else 'folder'} per "
                                  "command - give `path`, never a list. Call it once for each.")
    if unknown:
        # A misspelt field (`sha_256`, `mustBeUnder`) would otherwise be a check silently not made.
        raise DeleteVerifiedError(f"{command}: unknown field(s) {', '.join(unknown)}; "
                                  f"it takes {', '.join(sorted(allowed))}.")


def _items(value: Any, field: str, allowed: frozenset[str]) -> list[dict[str, Any]]:
    """The list itself: not empty, not too long, every item an object with only the known facts."""
    if not isinstance(value, list) or not value:
        raise DeleteVerifiedError(f"{field} is required and must be a non-empty list.")
    if len(value) > MAX_ITEMS:
        raise DeleteVerifiedError(f"{field} holds {len(value)} items; at most {MAX_ITEMS} per command.")
    for index, item in enumerate(value):
        if not isinstance(item, dict):
            raise DeleteVerifiedError(f"{field}[{index}] must be an object with its own facts.")
        unknown = sorted(set(item) - allowed)
        if unknown:
            # `host` or `dry_run` on an item would be a second answer to a question the request answers once.
            raise DeleteVerifiedError(f"{field}[{index}]: unknown field(s) {', '.join(unknown)}; "
                                      f"an item takes {', '.join(sorted(allowed))}.")
    return value


def _file_facts(request: dict[str, Any], *, host: Host, where: str = "") -> dict[str, Any]:
    """Every fact one file delete holds the host to, refused here when any is malformed."""
    try:
        path, root = _validated_path(request, host=host)
        name = _text(request.get("file_name"), "file_name")
        if name != _last_part(path):
            raise DeleteVerifiedError(f"file_name {name!r} is not the last part of path {path!r} - "
                                      "the name and the path must name the same file.")
        size = _size(request.get("size_bytes"))
        sha256 = _sha256(request.get("sha256"))
    except DeleteVerifiedError as exc:
        if not where:
            raise
        raise DeleteVerifiedError(f"{where}: {exc}") from exc
    return {"path": path, "root": root, "name": name, "size": size, "sha256": sha256}


def _no_duplicates(paths: list[str], *, host: Host, field: str) -> None:
    seen: set[str] = set()
    for path in paths:
        key = path.replace("/", "\\").lower() if host.runtime == WINDOWS else path
        if key in seen:
            raise DeleteVerifiedError(f"{field} names {path!r} twice - each item is one place, once.")
        seen.add(key)


def _host(request: dict[str, Any]) -> Host:
    host = parse_host(request.get("host"))
    if host.is_local and not host.is_windows and os.name == "nt":
        # `run` uses this machine's shell: a POSIX script handed to cmd.exe does not do what it says.
        raise DeleteVerifiedError('host.runtime must be "windows" for a path on this Windows machine.')
    return host


def _text(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise DeleteVerifiedError(f"{field} is required and must be text.")
    if value != value.strip():
        raise DeleteVerifiedError(f"{field} must not start or end with a space: {value!r}.")
    if any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise DeleteVerifiedError(f"{field} must not hold a control character (a newline, a tab): {value!r}.")
    return value


def _size(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise DeleteVerifiedError(f"size_bytes is required and must be a whole number of bytes, 0 or more; got {value!r}.")
    return value


def _sha256(value: Any) -> str:
    text = value.strip().lower() if isinstance(value, str) else ""
    if not _SHA256.match(text):
        raise DeleteVerifiedError(f"sha256 is required and must be 64 hexadecimal characters; got {value!r}.")
    return text


def _flag(value: Any, field: str) -> bool:
    if value is None:
        return False
    if not isinstance(value, bool):
        raise DeleteVerifiedError(f"{field} must be true or false; got {value!r}.")
    return value


def _timeout(value: Any) -> int:
    if value is None:
        # A 60 GB FULL is hashed before it is deleted, so the delete's limit is the hash's.
        return HASH_TIMEOUT_SECONDS
    if isinstance(value, bool) or not isinstance(value, int) or value < 10:
        raise DeleteVerifiedError(f"timeout_seconds must be a whole number, 10 or more; got {value!r}.")
    return value


def _validated_path(request: dict[str, Any], *, host: Host, where: str = "") -> tuple[str, str]:
    """``(path, must_be_under)``, or refused - absolute, one name, no pattern, strictly inside the fence."""
    if where:
        try:
            return _validated_path(request, host=host)
        except DeleteVerifiedError as exc:
            raise DeleteVerifiedError(f"{where}: {exc}") from exc
    path = _text(request.get("path"), "path")
    root = _text(request.get("must_be_under"), "must_be_under")
    for field, value in (("path", path), ("must_be_under", root)):
        if any(char in value for char in WILDCARDS):
            raise DeleteVerifiedError(f"{field} must name one place, not a pattern: {value!r}.")
        if not _is_absolute(value, host=host):
            raise DeleteVerifiedError(f"{field} must be absolute: {value!r}.")
        if any(part in (".", "..") for part in re.split(r"[\\/]+", value)):
            raise DeleteVerifiedError(f"{field} must not contain '.' or '..' segments: {value!r}.")
        if host.runtime == WINDOWS and ":" in value[2:]:
            # An alternate data stream (`a.bak:x`) is not the file the name says.
            raise DeleteVerifiedError(f"{field} must not hold ':' past its drive: {value!r}.")
    if path.endswith(("/", "\\")):
        raise DeleteVerifiedError(f"path must name a file or folder, not end in a separator: {path!r}.")
    if not _strictly_under(path, root, host=host):
        raise DeleteVerifiedError(f"path {path!r} is not inside must_be_under {root!r} - "
                                  "and the fence itself is never deleted.")
    return path, root


def _is_absolute(path: str, *, host: Host) -> bool:
    if host.runtime == WINDOWS:
        return path.startswith("\\\\") or (len(path) > 2 and path[1] == ":" and path[2] in "\\/")
    return path.startswith("/")


def _strictly_under(path: str, root: str, *, host: Host) -> bool:
    """``path`` is inside ``root`` and is not ``root`` itself. Text, case-insensitive on Windows -
    neither end is on this machine, so there is nothing here to resolve a link against."""
    if host.runtime == WINDOWS:
        normal, base, separator = path.replace("/", "\\").lower(), root.replace("/", "\\").lower(), "\\"
    else:
        normal, base, separator = path, root, "/"
    base = base.rstrip(separator)
    return normal.startswith(base + separator) and len(normal.rstrip(separator)) > len(base) + 1
