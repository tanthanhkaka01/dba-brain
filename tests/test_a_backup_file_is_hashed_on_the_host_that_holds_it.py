"""A backup file is hashed on the host that holds it - ``common.cli file-checksum`` (0.34.0, 1.210).

Why it matters: every check the backup catalog makes - the hash a backup records, the source and
the copy a restore compares before it runs, the scrub that finds a backup encrypted after it was
written - is this one command. If it hashed by streaming the file here, a 60 GB FULL would cross the
network twice for one restore; if it read a missing file as an error, a restore gate could not tell
"gone" from "the host refused"; if Windows needed ``Get-FileHash``, the 2008 R2 hosts (PowerShell
2.0) could never be checked. And it is the only one (R43): pack, pull, push and pack-files build
their single-file hash from it.
"""

from __future__ import annotations

import base64
import hashlib
import json
import shutil
import subprocess

import pytest

from dbabrain.common import cli, filechecksum
from dbabrain.common.hostcmd import parse_host

LINUX = {"runtime": "linux", "host": "192.0.2.20", "username": "u", "password": "p"}
WINDOWS = {"runtime": "windows", "host": "192.0.2.30", "username": "u", "password": "p"}


class _Session:
    def close(self):
        return None


def _on_host(monkeypatch, answer):
    """Every command sent to the host goes to ``answer(command) -> (exit, stdout, stderr)``."""
    sent: list[str] = []

    def run(host, command, *, timeout=300, session=None):
        sent.append(command)
        code, out, err = answer(command)
        return {"exit_code": code, "stdout": out, "stderr": err}

    monkeypatch.setattr(filechecksum, "open_session", lambda host: _Session())
    monkeypatch.setattr(filechecksum, "run", run)
    return sent


# --------------------------------------------------------------------------- #
# Here, with no host
# --------------------------------------------------------------------------- #
def test_files_on_this_machine_are_hashed_in_the_order_asked_and_a_missing_one_is_listed(tmp_path):
    full = tmp_path / "APPDB_FULL.bak"
    log = tmp_path / "APPDB_LOG.trn"
    full.write_bytes(b"a full backup")
    log.write_bytes(b"a log backup")

    answer = filechecksum.checksum({"paths": [str(log), str(tmp_path / "gone.trn"), str(full), str(log)]})

    assert [item["path"] for item in answer["files"]] == [str(log), str(full)]
    assert answer["files"][1]["sha256"] == hashlib.sha256(b"a full backup").hexdigest()
    assert answer["files"][1]["size_bytes"] == len(b"a full backup")
    assert answer["files"][1]["mtime_utc"].endswith("Z")
    assert answer["missing"] == [str(tmp_path / "gone.trn")]
    assert answer["host"] == "local"
    assert answer["file_count"] == 2
    assert answer["bytes_total"] == len(b"a full backup") + len(b"a log backup")


def test_a_request_without_paths_or_with_an_unknown_algorithm_is_refused():
    with pytest.raises(filechecksum.FileChecksumError, match="paths is required"):
        filechecksum.checksum({"paths": []})
    with pytest.raises(filechecksum.FileChecksumError, match="algorithm must be one of"):
        filechecksum.checksum({"paths": ["/x"], "algorithm": "md5"})


# --------------------------------------------------------------------------- #
# A Linux host
# --------------------------------------------------------------------------- #
def test_a_linux_host_is_asked_with_sha256sum_and_stat_and_a_missing_file_is_an_answer(monkeypatch):
    digest = "a" * 64

    def answer(command):
        if command.startswith("sha256sum"):
            return 1, f"{digest}  /b/APPDB_FULL.bak\n", "sha256sum: /b/gone.trn: No such file or directory\n"
        return 1, "4096 1761700000 /b/APPDB_FULL.bak\n", "stat: cannot statx '/b/gone.trn'\n"

    sent = _on_host(monkeypatch, answer)
    result = filechecksum.checksum({"host": LINUX, "paths": ["/b/APPDB_FULL.bak", "/b/gone.trn"]})

    assert sent[0] == "sha256sum /b/APPDB_FULL.bak /b/gone.trn"
    assert sent[1] == "stat -c '%s %Y %n' /b/APPDB_FULL.bak /b/gone.trn"
    assert result["files"] == [{"path": "/b/APPDB_FULL.bak", "sha256": digest, "size_bytes": 4096,
                                "mtime_utc": "2025-10-29T01:06:40Z"}]
    assert result["missing"] == ["/b/gone.trn"]
    assert result["host"] == "192.0.2.20"


def test_a_host_without_sha256sum_fails_rather_than_reporting_every_file_missing(monkeypatch):
    _on_host(monkeypatch, lambda command: (127, "", "sh: 1: sha256sum: not found\n"))
    with pytest.raises(filechecksum.FileChecksumError, match="not found"):
        filechecksum.checksum({"host": LINUX, "paths": ["/b/APPDB_FULL.bak"]})


def test_a_name_with_a_backslash_or_a_leading_dash_comes_back_under_the_name_asked(monkeypatch):
    digest = "b" * 64

    def answer(command):
        if command.startswith("sha256sum"):
            return 0, f"\\{digest}  /b/odd\\\\name.bak\n{digest}  ./-dash.bak\n", ""
        return 0, "", ""

    sent = _on_host(monkeypatch, answer)
    result = filechecksum.checksum({"host": LINUX, "paths": ["/b/odd\\name.bak", "-dash.bak"]})

    assert "./-dash.bak" in sent[0], "a leading dash must not be read as an option"
    assert [item["path"] for item in result["files"]] == ["/b/odd\\name.bak", "-dash.bak"]
    assert result["missing"] == []


def test_many_paths_go_in_batches_each_under_the_command_budget(monkeypatch):
    paths = [f"/opt/backup/SALESDB/LOG/SALESDB_LOG_20261029_{n:04d}.trn" for n in range(3000)]
    sent = _on_host(monkeypatch, lambda command: (0, "", ""))

    filechecksum.checksum({"host": LINUX, "paths": paths})

    hashes = [command for command in sent if command.startswith("sha256sum")]
    assert len(hashes) > 1
    assert all(len(command) <= filechecksum.MAX_COMMAND_CHARS["posix"] + 200 for command in hashes)
    assert sum(command.count(".trn") for command in hashes) == len(paths)


# --------------------------------------------------------------------------- #
# A Windows host
# --------------------------------------------------------------------------- #
def test_windows_hashes_through_dotnet_so_powershell_2_works_too():
    script = filechecksum.windows_script([r"D:\SQLBK\APP_PROD_FULL.bak"])
    assert "Get-FileHash" not in script
    assert "System.Security.Cryptography.SHA256" in script
    assert "Get-FileHash" not in filechecksum.hash_command("powershell", r"D:\x.bak")


def test_an_apostrophe_in_a_windows_path_is_doubled_not_left_to_end_the_string():
    assert "'D:\\O''Brien\\a.bak'" in filechecksum.windows_script(["D:\\O'Brien\\a.bak"])


def test_a_windows_answer_is_read_line_by_line_ok_and_missing(monkeypatch):
    digest = "c" * 64
    out = (f"OK\t{digest}\t1024\t1761700000\tD:\\SQLBK\\APP_PROD_FULL.bak\r\n"
           "MISSING\t\t\t\tD:\\SQLBK\\gone.trn\r\n")
    _on_host(monkeypatch, lambda command: (0, out, ""))

    result = filechecksum.checksum({"host": WINDOWS,
                                    "paths": [r"D:\SQLBK\APP_PROD_FULL.bak", r"D:\SQLBK\gone.trn"]})

    assert result["files"] == [{"path": r"D:\SQLBK\APP_PROD_FULL.bak", "sha256": digest,
                                "size_bytes": 1024, "mtime_utc": "2025-10-29T01:06:40Z"}]
    assert result["missing"] == [r"D:\SQLBK\gone.trn"]


def test_a_windows_batch_stays_short_enough_for_an_encoded_command():
    paths = [rf"D:\SQLBK_DBOPS\SALESDB\LOG\SALESDB_LOG_20261029_{n:04d}.trn" for n in range(400)]
    for batch in filechecksum._batches(paths, windows=True):
        wrapped = parse_host(WINDOWS)
        from dbabrain.common.hostcmd import wrap

        assert len(wrap(wrapped, filechecksum.windows_script(batch))) < 8191


@pytest.mark.skipif(shutil.which("powershell") is None, reason="needs Windows PowerShell")
def test_the_windows_script_gives_the_same_hash_as_python_when_powershell_runs_it(tmp_path):
    target = tmp_path / "it's a backup.bak"
    target.write_bytes(b"bytes a restore depends on\n" * 1000)
    encoded = base64.b64encode(filechecksum.windows_script([str(target), str(tmp_path / "gone.bak")])
                               .encode("utf-16-le")).decode("ascii")
    done = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-EncodedCommand", encoded],
                          capture_output=True, text=True, timeout=120)
    parsed = filechecksum._parse_windows([str(target), str(tmp_path / "gone.bak")], done.stdout)

    assert parsed[str(target)]["sha256"] == hashlib.sha256(target.read_bytes()).hexdigest()
    assert parsed[str(target)]["size_bytes"] == target.stat().st_size
    assert str(tmp_path / "gone.bak") not in parsed

    single = subprocess.run(
        ["powershell", "-NoProfile", "-NonInteractive", "-EncodedCommand",
         base64.b64encode(filechecksum.hash_command("powershell", str(target)).encode("utf-16-le")).decode("ascii")],
        capture_output=True, text=True, timeout=120)
    assert single.stdout.split()[0] == hashlib.sha256(target.read_bytes()).hexdigest()


# --------------------------------------------------------------------------- #
# The command
# --------------------------------------------------------------------------- #
def test_the_command_answers_in_the_envelope_with_the_files_and_what_was_missing(tmp_path, capsys):
    full = tmp_path / "APPDB_FULL.bak"
    full.write_bytes(b"a full backup")
    request = tmp_path / "request.json"
    request.write_text(json.dumps({"paths": [str(full), str(tmp_path / "gone.trn")]}), encoding="utf-8")

    code = cli.main(["file-checksum", f"@{request}"])
    body = json.loads(capsys.readouterr().out)

    assert code == 0
    assert body["success"] is True
    assert body["data"]["files"][0]["sha256"] == hashlib.sha256(b"a full backup").hexdigest()
    assert body["data"]["missing"] == [str(tmp_path / "gone.trn")]
    assert "1 missing" in body["message"]
