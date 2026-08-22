"""Moving a backup set between hosts: one stream, and nothing copied twice.

A PostgreSQL backup set is thousands of tiny files. Copying them one at a time over SFTP pays
several network round trips *each*, and the orchestrator sits an internet hop from both ends —
measured at 10 KB/s for a 362 MB set, i.e. eight hours, with the link never the limit. The
files that need copying are therefore streamed as a single tar.

The two properties that make it usable unattended: it never re-copies a file that is already
there, and it degrades to the old per-file path instead of failing when tar is unusable.
"""

import os
import shutil
import subprocess

import pytest

from db_ops.backup_restore import transfer


class _Chan:
    def __init__(self, rc=0):
        self._rc = rc
    def recv_exit_status(self):
        return self._rc
    def shutdown_write(self):
        pass


class _In:
    def __init__(self):
        self.written = b""
        self.channel = _Chan()
    def write(self, data):
        self.written += data if isinstance(data, bytes) else data.encode()
    def flush(self):
        pass
    def close(self):
        pass


class _Out:
    def __init__(self, payload=b"", rc=0):
        self._payload = payload
        self.channel = _Chan(rc)
    def read(self, n=-1):
        if n == -1 or n >= len(self._payload):
            data, self._payload = self._payload, b""
            return data
        data, self._payload = self._payload[:n], self._payload[n:]
        return data


class _Client:
    """Records exec_command calls; serves a fixed payload as the source."""
    def __init__(self, payload=b"", rc=0, sftp=None):
        self.payload, self.rc, self.commands = payload, rc, []
        self._sftp = sftp
        self.stdin = _In()
    def exec_command(self, command, timeout=None):
        self.commands.append(command)
        return self.stdin, _Out(self.payload, self.rc), _Out(b"", 0)
    def open_sftp(self):
        return self._sftp


class _Sftp:
    def __init__(self, tree, writable=True):
        self.tree = tree      # {dir: [(name, size, is_dir)]}
        self.made = []
        self.writable = writable      # a staging directory the SSH user cannot write to
        self.probed = []
    def listdir_attr(self, path):
        import stat as st
        class _E:
            def __init__(self, filename, size, isdir):
                self.filename, self.st_size = filename, size
                self.st_mode = (st.S_IFDIR if isdir else st.S_IFREG) | 0o644
        return [_E(n, s, d) for n, s, d in self.tree.get(path, [])]
    def stat(self, path):
        raise IOError("missing")
    def mkdir(self, path):
        self.made.append(path)
    def open(self, path, mode="r"):
        if not self.writable:
            raise IOError("Permission denied")
        self.probed.append(path)
        class _F:
            def write(self, _data): pass
            def __enter__(self_inner): return self_inner
            def __exit__(self_inner, *_a): return False
        return _F()
    def remove(self, path):
        pass
    def close(self):
        pass


def test_only_the_named_files_are_streamed():
    """tar -T - takes the caller's list, so the skip decision still governs what moves."""
    source = _Client(payload=b"TARDATA")
    target = _Client()

    ok = transfer._stream_files(
        source_client=source, source_dir="/src", target_client=target, target_dir="/dst",
        files=[("base/a", 10), ("wal/b", 20)],
    )

    assert ok is True
    assert "tar -cf - -C /src" in source.commands[0] and "-T -" in source.commands[0]
    assert source.stdin.written == b"base/a\nwal/b\n"      # exactly the two names
    assert "tar -xf - -C /dst" in target.commands[0]
    assert target.stdin.written == b"TARDATA"              # the stream, untouched


def test_a_failing_tar_reports_failure_so_the_caller_can_fall_back():
    source = _Client(payload=b"", rc=2)
    target = _Client()

    assert transfer._stream_files(
        source_client=source, source_dir="/src", target_client=target, target_dir="/dst",
        files=[("a", 1)],
    ) is False


def test_nothing_is_transferred_when_the_target_already_has_it(monkeypatch):
    """The unattended case: a drill that runs daily must not re-send what it sent yesterday."""
    same = {"/src": [("a.bak", 100, False)], "/dst": [("a.bak", 100, False)]}
    source = _Client(sftp=_Sftp(same))
    target = _Client(sftp=_Sftp(same))
    called = []
    monkeypatch.setattr(transfer, "_stream_files", lambda **kw: called.append(kw) or True)

    result = transfer.sync_backup_dir(source_client=source, source_dir="/src",
                                      target_client=target, target_dir="/dst")

    assert (result.copied, result.skipped) == (0, 1)
    assert called == [], "a file already present must not be streamed again"


def test_only_the_new_files_are_streamed_on_a_repeat_run(monkeypatch):
    tree_src = {"/src": [("old.bak", 100, False), ("new.bak", 200, False)]}
    tree_dst = {"/dst": [("old.bak", 100, False)]}
    source = _Client(sftp=_Sftp(tree_src))
    target = _Client(sftp=_Sftp(tree_dst))
    seen = {}
    monkeypatch.setattr(transfer, "_stream_files", lambda **kw: seen.update(kw) or True)

    result = transfer.sync_backup_dir(source_client=source, source_dir="/src",
                                      target_client=target, target_dir="/dst")

    assert [rel for rel, _ in seen["files"]] == ["new.bak"]
    assert (result.copied, result.skipped, result.bytes_copied) == (1, 1, 200)


def test_a_changed_size_is_re_sent():
    """Same name, different size = not the same file."""
    tree_src = {"/src": [("a.bak", 300, False)]}
    tree_dst = {"/dst": [("a.bak", 100, False)]}
    source = _Client(sftp=_Sftp(tree_src), payload=b"X")
    target = _Client(sftp=_Sftp(tree_dst))

    result = transfer.sync_backup_dir(source_client=source, source_dir="/src",
                                      target_client=target, target_dir="/dst")

    assert (result.copied, result.skipped) == (1, 0)


def test_a_staging_dir_the_ssh_user_cannot_write_fails_before_anything_is_sent(monkeypatch):
    """A read-only target is invisible to every other check: it lists fine and refuses every file.

    Left to the stream, `tar -x` complains once per file into a stderr nobody can read until the
    command ends - which it never does while the copy is still going - so the source pushed 8.25 GB
    into a stalled pipe, the target wrote nothing, and the run sat at RUNNING for its full two-hour
    timeout. Measured on CLOUD2 after a staging directory was recreated with sudo and came back
    root-owned. One probe file up front is the difference between that and a sentence."""
    tree_src = {"/src": [("a.bak", 100, False)]}
    source = _Client(sftp=_Sftp(tree_src))
    target = _Client(sftp=_Sftp({"/dst": []}, writable=False))
    streamed = []
    monkeypatch.setattr(transfer, "_stream_files", lambda **kw: streamed.append(kw) or True)

    with pytest.raises(PermissionError, match="not writable by the SSH user"):
        transfer.sync_backup_dir(source_client=source, source_dir="/src",
                                 target_client=target, target_dir="/dst")

    assert streamed == [], "nothing may be streamed into a directory that refuses it"


def test_the_probe_file_is_cleaned_up_on_a_writable_target(monkeypatch):
    tree = {"/src": [("a.bak", 100, False)], "/dst": [("a.bak", 100, False)]}
    target_sftp = _Sftp(tree)
    source = _Client(sftp=_Sftp(tree))
    target = _Client(sftp=target_sftp)
    monkeypatch.setattr(transfer, "_stream_files", lambda **kw: True)

    transfer.sync_backup_dir(source_client=source, source_dir="/src",
                             target_client=target, target_dir="/dst")

    assert target_sftp.probed == ["/dst/.db_ops_write_probe"]


def test_stderr_is_drained_while_the_stream_runs(monkeypatch):
    """The deadlock was structural: stderr was only read after recv_exit_status(), which cannot
    be reached while the copy is still in flight. So the drain must start with the command."""
    source = _Client(payload=b"TARDATA")
    target = _Client()
    drained = []
    real_drain = transfer._drain
    monkeypatch.setattr(transfer, "_drain",
                        lambda handle: drained.append(handle) or real_drain(handle))

    transfer._stream_files(source_client=source, source_dir="/src",
                           target_client=target, target_dir="/dst", files=[("a", 1)])

    assert len(drained) == 2, "both ends' stderr must be drained, not just the source's"


# --------------------------------------------------------------------------- #
# Pruning the restore-target staging dir must not delete the directories a
# PostgreSQL backup piece is *supposed* to have empty
# --------------------------------------------------------------------------- #
class _CapturingClient:
    """paramiko-like client that records the command instead of running it."""

    def __init__(self, stdout="0\n", exit_status=0):
        self.command = None
        self._stdout, self._exit = stdout, exit_status

    def exec_command(self, command, timeout=None):  # noqa: ARG002 - signature parity
        self.command = command
        channel = type("C", (), {"recv_exit_status": lambda _self: self._exit})()
        out = type("O", (), {"read": lambda _self: self._stdout.encode(), "channel": channel})()
        return None, out, out


def test_prune_never_deletes_a_directory_that_is_empty_by_design():
    """`find -type d -empty -delete` took pg_tblspc / pg_replslot / pg_stat_tmp with it.

    The transfer creates them, this ran seconds later and removed them, and the restore then
    failed with `pg_combinebackup: could not open directory ".../pg_tblspc"` — for a path
    nobody had deleted at the source. A directory may only go when its whole subtree holds no
    file at all.
    """
    from db_ops.backup_restore.transfer import prune_target_dir

    client = _CapturingClient()
    prune_target_dir(client, "/opt/db_ops/pg_restore_stage", 8 * 86400)

    assert "-type d -empty -delete" not in client.command
    # A husk is still collected, but only when nothing beneath it is a file.
    assert "-type f -print -quit" in client.command
    assert "-maxdepth 2" in client.command          # never steps inside a backup piece
    assert "-depth" in client.command               # post-order: no descend-into-deleted error


def test_prune_still_deletes_files_older_than_the_retention():
    from db_ops.backup_restore.transfer import prune_target_dir

    client = _CapturingClient(stdout="7\n")
    result = prune_target_dir(client, "/stage", 2 * 86400)

    assert result["pruned"] == 7
    assert "-type f -mmin +2880 -print -delete" in client.command


def test_prune_is_skipped_when_retention_is_disabled():
    from db_ops.backup_restore.transfer import prune_target_dir

    client = _CapturingClient()
    result = prune_target_dir(client, "/stage", 0)

    assert result["skipped"] == "retention disabled"
    assert client.command is None


@pytest.mark.skipif(os.name == "nt" or shutil.which("bash") is None,
                    reason="needs a real POSIX /tmp: under MSYS each bash resolves it separately")
def test_the_prune_shell_command_really_keeps_the_piece_and_drops_the_husk(tmp_path):
    """Runs the generated command for real — this is where the shell logic is proven.

    Skipped on Windows: the command runs on the Linux restore target, and a Git-Bash harness
    resolves /tmp per invocation, so a green run here would prove nothing anyway."""
    from db_ops.backup_restore.transfer import prune_target_dir

    root = "/tmp/dbops_prune_test"
    layout = (
        f"rm -rf {root} && mkdir -p "
        f"{root}/base/live/pg_tblspc {root}/base/live/pg_replslot "
        f"{root}/base/husk/base/1 {root}/empty_top {root}/wal && "
        f"echo x > {root}/base/live/backup_manifest && "
        f"echo old > {root}/wal/old.wal && touch -t 202001010000 {root}/wal/old.wal"
    )
    subprocess.run(["bash", "-lc", layout], check=True, capture_output=True)

    class ShellClient(_CapturingClient):
        def exec_command(self, command, timeout=None):
            self.command = command
            done = subprocess.run(["bash", "-lc", command], capture_output=True, text=True)
            channel = type("C", (), {"recv_exit_status": lambda _s: done.returncode})()
            out = type("O", (), {"read": lambda _s: done.stdout.encode(), "channel": channel})()
            return None, out, out

    client = ShellClient()
    result = prune_target_dir(client, root, 8 * 86400)
    listing = subprocess.run(["bash", "-lc", f"find {root} -mindepth 1 | sort"],
                             capture_output=True, text=True).stdout.split()
    rel = {path[len(root):] for path in listing}

    assert "error" not in result, result            # find must not exit non-zero
    assert "/base/live/pg_tblspc" in rel            # empty by design, and required
    assert "/base/live/pg_replslot" in rel
    assert "/base/live/backup_manifest" in rel
    assert "/base/husk" not in rel                  # no file anywhere beneath it
    assert "/empty_top" not in rel
    assert "/wal/old.wal" not in rel                # older than the retention
