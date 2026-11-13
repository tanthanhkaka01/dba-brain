"""``read-file`` reads ONE small file on its host and proves the bytes on arrival (0.37.0).

Why it matters: it is how a backup certificate pair reaches dbabrain's vault before the server's copy
is deleted - a pair kept on the word of a transfer that did not check would be the one copy left. So
the host states the size and the sha256 beside the bytes, and the bytes are held to both here; a
link, a missing file, a pattern, a relative path, a file over ``max_bytes`` or an unknown field is
refused. The scripts run for real: PowerShell for a Windows host, POSIX ``sh`` (Git Bash here) for
Linux.
"""

from __future__ import annotations

import base64
import hashlib
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from dbabrain.common import readfile as R

# The Windows script, run where its paths are Windows paths: a Linux runner has pwsh too, and its
# POSIX temp folder is no path the script takes (0.38.0, the first CI run on Linux).
POWERSHELL = (shutil.which("powershell") or shutil.which("pwsh")) if os.name == "nt" else None
SH = (r"C:\Program Files\Git\bin\bash.exe" if os.name == "nt" and os.path.exists(r"C:\Program Files\Git\bin\bash.exe")
      else (shutil.which("sh") if os.name != "nt" else None))


def _posix(path: Path) -> str:
    text = str(path)
    return "/" + text[0].lower() + text[2:].replace("\\", "/") if os.name == "nt" and text[1:2] == ":" else text


def _shells():
    out = []
    if POWERSHELL:
        out.append(pytest.param("windows", id="powershell"))
    if SH:
        out.append(pytest.param("linux", id="sh"))
    return out or [pytest.param("none", marks=pytest.mark.skip(reason="no shell"))]


@pytest.fixture(params=_shells())
def shell(request, monkeypatch):
    def run(host, command, *, timeout=300, session=None):
        if host.is_windows:
            encoded = base64.b64encode(command.encode("utf-16-le")).decode()
            done = subprocess.run([POWERSHELL, "-NoProfile", "-NonInteractive", "-EncodedCommand", encoded],
                                  capture_output=True, text=True, timeout=120)
        else:
            done = subprocess.run([SH, "-c", command], capture_output=True, text=True, timeout=120)
        return {"exit_code": done.returncode, "stdout": done.stdout, "stderr": done.stderr}
    monkeypatch.setattr(R, "run", run)
    kind = request.param
    return {"host": {"runtime": kind} if kind == "windows" else {"runtime": "linux", "host": "192.0.2.1"},
            "name": (lambda p: str(p)) if kind == "windows" else _posix}


def test_a_file_is_read_whole_and_its_facts_are_the_hosts(shell, tmp_path):
    data = bytes(range(256)) * 40 + b"\x00\r\n tail"
    path = tmp_path / "dbabrain_backup_cert.pvk"
    path.write_bytes(data)

    answer = R.read_file({"host": shell["host"], "path": shell["name"](path)})

    assert base64.b64decode(answer["base64"]) == data
    assert answer["size_bytes"] == len(data) and answer["sha256"] == hashlib.sha256(data).hexdigest()
    assert answer["file_name"] == "dbabrain_backup_cert.pvk"


def test_an_empty_file_is_read_as_empty(shell, tmp_path):
    path = tmp_path / "empty.cer"
    path.write_bytes(b"")
    answer = R.read_file({"host": shell["host"], "path": shell["name"](path)})
    assert answer["size_bytes"] == 0 and answer["base64"] == ""


def test_a_missing_file_and_a_file_over_the_cap_are_refused(shell, tmp_path):
    with pytest.raises(R.ReadFileError, match="no such file"):
        R.read_file({"host": shell["host"], "path": shell["name"](tmp_path / "absent.cer")})
    big = tmp_path / "big.bin"
    big.write_bytes(b"x" * 2048)
    with pytest.raises(R.ReadFileError, match="over max_bytes"):
        R.read_file({"host": shell["host"], "path": shell["name"](big), "max_bytes": 1024})


@pytest.mark.parametrize("stdout, words", [
    (f"OK 5 {hashlib.sha256(b'hello').hexdigest()}\n{base64.b64encode(b'hellx').decode()}\n", "not used"),
    (f"OK 6 {hashlib.sha256(b'hello').hexdigest()}\n{base64.b64encode(b'hello').decode()}\n", "not used"),
    ("OK 5\n", "unexpected answer"),
    ("Welcome\nOK 5 x\naGVsbG8=\n", "unexpected answer"),
    ("", "unexpected answer"),
    ("LINK\n", "never followed"),
])
def test_bytes_that_are_not_what_the_host_stated_are_never_used(monkeypatch, stdout, words):
    monkeypatch.setattr(R, "run", lambda *_a, **_k: {"exit_code": 0, "stdout": stdout, "stderr": ""})
    with pytest.raises(R.ReadFileError, match=words):
        R.read_file({"host": {"runtime": "linux", "host": "192.0.2.1"}, "path": "/b/_cert/c.cer"})


@pytest.mark.parametrize("request_, words", [
    ({"path": "/b/_cert/*.cer"}, "not a pattern"),
    ({"path": "b/_cert/c.cer"}, "must be absolute"),
    ({"path": "/b/../etc/shadow"}, "'..'"),
    ({"path": ""}, "path is required"),
    ({"path": "/b/c.cer", "paths": ["/b/d.cer"]}, "unknown field"),
    ({"path": "/b/c.cer", "max_bytes": 0}, "max_bytes"),
    ({"path": "/b/c.cer", "max_bytes": 1 << 30}, "max_bytes"),
])
def test_a_request_that_does_not_name_one_file_in_full_is_refused_before_the_host(monkeypatch, request_, words):
    def unreachable(*_a, **_k):
        raise AssertionError("the host was reached")
    monkeypatch.setattr(R, "run", unreachable)
    with pytest.raises(R.ReadFileError, match=words):
        R.read_file({"host": {"runtime": "linux", "host": "192.0.2.1"}, **request_})
