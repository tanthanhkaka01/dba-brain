"""A PostgreSQL base backup is written in TAR format, compressed on the server - and still restores (0.37.0).

Why it matters: the plain format copied the data directory byte for byte, uncompressed - the
store's 65 GB cluster was 5,904 files and 65 GB a night, where its data compresses five to thirteen
times; every file then had to be copied, encrypted, proven and deleted on its own. The operator,
2026-11-10: *switch to tar, compressed*. A backup is still one directory (``base/<stamp>_FULL``)
with its ``backup_manifest`` beside the tars, so the catalog, the copy and the encryption read it as
before - but whatever read a file INSIDE the data directory must now read the manifest or unpack:

* the WAL a backup starts from - ``backup_label`` is inside ``base.tar`` - comes from the manifest's
  ``WAL-Ranges`` (the WAL sweep's cut line, a restore's WAL-hole check);
* a restore unpacks a tar member before laying it down or before ``pg_combinebackup``, which reads
  plain directories only; a plain member is read as it always was; a damaged archive, an unknown
  tablespace archive or a missing decompressor stops it by name.

The shell runs for real (Git Bash here) on real tar files.
"""

from __future__ import annotations

import io
import json
import os
import shutil
import subprocess
import tarfile
from pathlib import Path

import pytest

from dbabrain.common.backupfiles import postgresql as listing
from dbabrain.common.restorestep import postgresql as step
from dbabrain.lib.integrity import postgresql_start_wal_from_manifest

WINDOWS_HERE = os.name == "nt"
SH = (r"C:\Program Files\Git\bin\bash.exe" if WINDOWS_HERE and os.path.exists(r"C:\Program Files\Git\bin\bash.exe")
      else shutil.which("bash"))
needs_sh = pytest.mark.skipif(not SH, reason="a POSIX shell runs the restore's commands")
SCRIPTS = Path(step.__file__).resolve().parents[1] / "backup_scripts" / "postgresql"
RESTORE_SCRIPT = Path(step.__file__).resolve().parents[1] / "restore_scripts" / "postgresql" / "pg_restore_basebackup.sh"


def _posix(path: Path | str) -> str:
    text = str(path)
    if WINDOWS_HERE and len(text) > 1 and text[1] == ":":
        return "/" + text[0].lower() + text[2:].replace("\\", "/")
    return text


def _sh(command: str) -> subprocess.CompletedProcess:
    return subprocess.run([SH, "-c", command], capture_output=True, text=True, timeout=120)


# --------------------------------------------------------------------------- #
# The backup script writes tar, compressed on the server, from PostgreSQL 15
# --------------------------------------------------------------------------- #
def test_the_backup_script_asks_for_tar_compressed_on_the_server_from_postgresql_15():
    text = (SCRIPTS / "pg_basebackup_database.sh").read_text(encoding="utf-8")
    assert "show server_version_num" in text
    assert 'bb_format="-Ft --compress=server-zstd"' in text
    assert '[ "$server_version_num" -ge 150000 ]' in text
    # Both the full and the incremental carry it; the incremental still chains on the manifest.
    assert text.count("${bb_format} -X stream") == 2
    assert "--incremental='${latest}/backup_manifest'" in text


def test_a_tar_backup_is_verified_by_pg_verifybackup_18_without_parsing_its_wal_and_said_otherwise():
    text = (SCRIPTS / "pg_basebackup_database.sh").read_text(encoding="utf-8")
    assert "'${vb_bin}' -n '${target}'" in text and 'ls -1 /usr/lib/postgresql/${server_major}/bin/pg_verifybackup' in text
    assert "reads tar only from PostgreSQL 18 - not verified here" in text
    assert "the WAL in pg_wal.tar not parsed" in text


# --------------------------------------------------------------------------- #
# The start WAL from the manifest
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("ranges, segment", [
    ('"WAL-Ranges":[{"Timeline":1,"Start-LSN":"0/2000028","End-LSN":"0/2000100"}]', "000000010000000000000002"),
    ('"WAL-Ranges":[{"Timeline":3,"Start-LSN":"1A/FE000028","End-LSN":"1A/FF000060"}]', "000000030000001A000000FE"),
    # A timeline switch during the backup: the earliest range's start.
    ('"WAL-Ranges":[{"Timeline":2,"Start-LSN":"1A/FF000060","End-LSN":"1B/0"},'
     '{"Timeline":1,"Start-LSN":"1A/FE000028","End-LSN":"1A/FF000060"}]', "000000010000001A000000FE"),
    ("", ""), ("not json", ""), ('"WAL-Ranges":[{"Timeline":1}]', ""),
])
def test_the_segment_a_backup_starts_from_is_read_off_its_manifest(ranges, segment):
    assert postgresql_start_wal_from_manifest(ranges) == segment


def test_a_cluster_with_64_mb_segments_names_its_segment_by_that_size():
    ranges = '"WAL-Ranges":[{"Timeline":1,"Start-LSN":"0/8000028","End-LSN":"0/9000000"}]'
    assert postgresql_start_wal_from_manifest(ranges, segment_bytes=64 * 1024 * 1024) == "000000010000000000000002"


@needs_sh
@pytest.mark.parametrize("seg_text, expected", [
    ("16MB", "000000010000001A000000FE"), ("64MB", "000000010000001A0000003F"), ("", "000000010000001A000000FE")])
def test_the_wal_sweep_finds_a_tar_backups_start_from_its_manifest(tmp_path, seg_text, expected):
    base = tmp_path / "base" / "20261110T010000Z_FULL"
    base.mkdir(parents=True)
    (base / "backup_manifest").write_text(json.dumps({"WAL-Ranges": [
        {"Timeline": 1, "Start-LSN": "1A/FE000028", "End-LSN": "1A/FF000060"}]}, indent=2), encoding="utf-8")
    text = (SCRIPTS / "pg_archive_wal.sh").read_text(encoding="utf-8")
    block = text[text.index("# >>> start wal from manifest"):text.index("# <<< start wal from manifest")]
    harness = "\n".join([
        'run_db() { case "$1" in psql*) printf "%s\\n" "$SEG_TEXT" ;; *) sh -c "$1" ;; esac; }',
        f"oldest_base='{_posix(base)}'", 'start_wal=""', "if true; then", block, "fi",
        'printf "%s\\n" "$start_wal"'])
    done = subprocess.run([SH, "-c", harness], capture_output=True, text=True, env={**os.environ, "SEG_TEXT": seg_text})
    assert done.returncode == 0, done.stderr
    assert done.stdout.strip() == expected


def test_the_listing_reads_a_tar_backups_start_off_its_manifest(monkeypatch):
    lines = ("/b/base/20261110T010000Z_FULL|4096|2026-11-10 01:05:00.000000000 +0000|"
             'M:"WAL-Ranges":[{"Timeline":1,"Start-LSN":"0/2000028","End-LSN":"0/2000100"}]\n'
             "/b/base/20261109T010000Z_FULL|4096|2026-11-09 01:05:00.000000000 +0000|000000010000000000000001\n"
             f"{listing.HOST_NOW}|2026-11-10 02:00:00\n")
    monkeypatch.setattr(listing, "run", lambda *a, **k: {"exit_code": 0, "stdout": lines, "stderr": ""})
    rows = listing.list_files({"host": {"runtime": "linux", "host": "192.0.2.1"}, "path": "/b"})
    starts = {r["path"]: r.get("start_wal") for r in rows}
    assert starts["/b/base/20261110T010000Z_FULL"] == "000000010000000000000002"
    assert starts["/b/base/20261109T010000Z_FULL"] == "000000010000000000000001"


# --------------------------------------------------------------------------- #
# The restore unpacks a tar member, reads a plain one as before
# --------------------------------------------------------------------------- #
def _tar(path: Path, members: dict[str, bytes], *, gz: bool) -> None:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as archive:
        for name, data in members.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            archive.addfile(info, io.BytesIO(data))
    raw = buffer.getvalue()
    if gz:
        import gzip
        raw = gzip.compress(raw)
    path.write_bytes(raw)


def _tar_backup(folder: Path, *, tag: str, gz: bool = True, extra: dict[str, bytes] | None = None) -> Path:
    folder.mkdir(parents=True)
    _tar(folder / ("base.tar.gz" if gz else "base.tar"),
         {"PG_VERSION": b"18\n", "base/1/1259": tag.encode(), "global/pg_control": b"ctl"}, gz=gz)
    _tar(folder / "pg_wal.tar", {"000000010000000000000002": b"wal-" + tag.encode()}, gz=False)
    (folder / "backup_manifest").write_text(json.dumps({"tag": tag}), encoding="utf-8")
    for name, data in (extra or {}).items():
        (folder / name).write_bytes(data)
    return folder


@needs_sh
@pytest.mark.parametrize("gz", [True, False])
def test_a_tar_full_is_unpacked_into_a_data_directory(tmp_path, gz):
    backup = _tar_backup(tmp_path / "base" / "20261110T010000Z_FULL", tag="full", gz=gz)
    staging = tmp_path / "staging"

    done = _sh(step._full_command(_posix(backup), _posix(staging)))

    assert done.returncode == 0, done.stderr
    assert (staging / "PG_VERSION").read_text() == "18\n"
    assert (staging / "base" / "1" / "1259").read_text() == "full"
    assert (staging / "pg_wal" / "000000010000000000000002").read_bytes() == b"wal-full"
    assert not (staging / "backup_manifest").exists() and not list(staging.glob("*.tar*"))


@needs_sh
def test_a_plain_full_is_copied_as_it_always_was(tmp_path):
    backup = tmp_path / "base" / "20261101T010000Z_FULL"
    (backup / "base" / "1").mkdir(parents=True)
    (backup / "PG_VERSION").write_text("18\n")
    (backup / "base" / "1" / "1259").write_text("plain")
    staging = tmp_path / "staging"

    done = _sh(step._full_command(_posix(backup), _posix(staging)))

    assert done.returncode == 0, done.stderr
    assert (staging / "base" / "1" / "1259").read_text() == "plain"


@needs_sh
@pytest.mark.parametrize("damage, words", [
    ("corrupt", "damaged"),
    ("tablespace", "tablespace archive"),
    ("lz4", "lz4 not found"),
])
def test_a_tar_full_that_cannot_be_unpacked_whole_stops_by_name(tmp_path, damage, words):
    folder = tmp_path / "base" / "20261110T010000Z_FULL"
    if damage == "corrupt":
        _tar_backup(folder, tag="full")
        data = (folder / "base.tar.gz").read_bytes()
        (folder / "base.tar.gz").write_bytes(data[: len(data) // 2])
    elif damage == "tablespace":
        _tar_backup(folder, tag="full", extra={"16385.tar": b"x" * 512})
    else:
        folder.mkdir(parents=True)
        (folder / "base.tar.lz4").write_bytes(b"not really lz4")
        (folder / "backup_manifest").write_text("{}")
    if damage == "lz4" and shutil.which("lz4"):
        pytest.skip("this machine has lz4")

    done = _sh(step._full_command(_posix(folder), _posix(tmp_path / "staging")))

    assert done.returncode != 0
    assert words in done.stderr


@needs_sh
def test_a_chain_of_tar_and_plain_members_is_unpacked_with_its_manifests_then_combined(tmp_path):
    full = _tar_backup(tmp_path / "base" / "20261103T010000Z_FULL", tag="full")
    incr = _tar_backup(tmp_path / "base" / "20261104T010000Z_INCR", tag="incr")
    plain = tmp_path / "base" / "20261105T010000Z_INCR"
    plain.mkdir(parents=True)
    (plain / "PG_VERSION").write_text("18\n")
    (plain / "backup_manifest").write_text("{}")
    # A pg_combinebackup that records what it was given and makes the output.
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake = bin_dir / "pg_combinebackup"
    seen = tmp_path / "seen.txt"
    fake.write_text('#!/bin/sh\nout=""\nfor a in "$@"; do printf "%s\\n" "$a" >> "' + _posix(seen) + '"; done\n'
                    'while [ $# -gt 0 ]; do [ "$1" = -o ] && out="$2"; shift; done\n'
                    'mkdir -p "$out" && echo 18 > "$out/PG_VERSION"\n', encoding="utf-8", newline="\n")
    fake.chmod(0o755)
    staging = tmp_path / "staging"

    done = _sh(step._combine_command([_posix(full), _posix(incr), _posix(plain)], _posix(staging), _posix(bin_dir)))

    assert done.returncode == 0, done.stderr
    args = seen.read_text().split()
    unpacked = _posix(staging) + "_unpacked"
    assert args == ["-o", _posix(staging), f"{unpacked}/0", f"{unpacked}/1", _posix(plain)]
    assert (staging / "PG_VERSION").exists()
    assert not Path(str(staging) + "_unpacked").exists(), "the work folder goes once combined"


@needs_sh
def test_each_unpacked_member_carries_its_manifest_for_pg_combinebackup(tmp_path):
    full = _tar_backup(tmp_path / "base" / "20261103T010000Z_FULL", tag="full")
    dest = tmp_path / "unpacked" / "0"
    done = _sh(step.extract_command(_posix(full), _posix(dest), with_manifest=True))
    assert done.returncode == 0, done.stderr
    assert json.loads((dest / "backup_manifest").read_text()) == {"tag": "full"}
    assert (dest / "pg_wal" / "000000010000000000000002").exists()


def test_the_drill_script_unpacks_tar_members_and_reads_the_identifier_off_the_manifest():
    text = RESTORE_SCRIPT.read_text(encoding="utf-8")
    assert "unpacked=\"${staging}_unpacked\"" in text
    assert "pg_combinebackup ${inputs}" in text
    assert "System-Identifier" in text and "control_field \"$chain_full\"" in text
