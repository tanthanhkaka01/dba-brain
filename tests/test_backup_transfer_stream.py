"""Making a staging folder ready for a backup set: nothing travels twice, and nothing is removed.

A drill runs every day against a folder yesterday's run already filled. What makes it usable
unattended is decided before the first byte moves: a file the target already holds, at the same
size and not older, does not travel again; a folder the SSH user cannot write is refused in one
sentence instead of a copy that stalls; and only a folder that carries this tool's marker - or is
plainly an earlier copy of this source - is treated as a staging at all.

Until 0.38.0 (1.329) this file also held the copy itself: the one ``tar`` stream the files crossed
in (a PostgreSQL set is thousands of small files, and one SFTP transfer each was measured at
10 KB/s), its stderr drained while it ran, and its failure reported so the caller could go file by
file. Those tests went with the stream: the bytes travel by ``copy-files`` now, each file with its
size and sha256, and the staging's part is ``prepare_staging`` - which says what still has to
travel, and names what the source no longer has without removing it.
"""

import pytest

from dbabrain.common import backup_copy as transfer


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
    """Records open_stream calls; serves a fixed payload as the source."""
    def __init__(self, payload=b"", rc=0, sftp=None):
        self.payload, self.rc, self.commands = payload, rc, []
        self._sftp = sftp
        self.stdin = _In()
    def open_stream(self, command, timeout_seconds=None):
        self.commands.append(command)
        return self.stdin, _Out(self.payload, self.rc), _Out(b"", 0)
    def sftp(self):
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


def _prepare(source_tree, target_sftp, **kwargs):
    """``prepare_staging`` of ``/src`` into ``/dst`` over the two fakes."""
    return transfer.prepare_staging(source_session=_Client(sftp=_Sftp(source_tree)), source_dir="/src",
                                    target_session=_Client(sftp=target_sftp), target_dir="/dst", **kwargs)


def test_nothing_is_left_to_travel_when_the_target_already_has_it():
    """The unattended case: a drill that runs daily must not re-send what it sent yesterday."""
    same = {"/src": [("a.bak", 100, False)], "/dst": [("a.bak", 100, False), (".dbabrain-staging", 1, False)]}

    result = _prepare(same, _Sftp(same))

    assert result.pending == [], "a file already present must not travel again"
    assert result.skipped == 1


def test_only_the_new_files_travel_on_a_repeat_run():
    tree_src = {"/src": [("old.bak", 100, False), ("new.bak", 200, False)]}
    tree_dst = {"/dst": [("old.bak", 100, False), (".dbabrain-staging", 1, False)]}

    result = _prepare(tree_src, _Sftp(tree_dst))

    assert [(rel, size) for rel, size, _mtime in result.pending] == [("new.bak", 200)]
    assert result.skipped == 1
    assert result.as_dict()["pending"] == [{"path": "new.bak", "size_bytes": 200, "mtime": None}], \
        "what the caller hands to copy-files, and the time finish-staging then sets"


def test_a_same_size_file_the_caller_names_in_replace_travels_again():
    """Size and time cannot see a copy with other bytes; the caller's checksum gate can (0.34.0)."""
    same = {"/src": [("a.bak", 100, False), ("b.bak", 100, False)],
            "/dst": [("a.bak", 100, False), ("b.bak", 100, False), (".dbabrain-staging", 1, False)]}

    result = _prepare(same, _Sftp(same), replace=("b.bak",))

    assert [rel for rel, _size, _mtime in result.pending] == ["b.bak"]
    assert result.skipped == 1
    assert result.as_dict()["files"] == [{"path": "a.bak", "size_bytes": 100}, {"path": "b.bak", "size_bytes": 100}], \
        "every selected file is answered, travelling or not - the gate hashes them all"


def test_a_plan_lists_what_the_copy_would_select_and_never_opens_the_target():
    tree = {"/src": [("base", 0, True), ("wal", 0, True), ("old", 0, True)],
            "/src/base": [("F", 0, True)], "/src/base/F": [("backup_manifest", 7, False)],
            "/src/wal": [("000000010000000000000001", 16, False)], "/src/old": [("x.bak", 3, False)]}
    source = _Client(sftp=_Sftp(tree))

    plan = transfer.plan_backup_dir(source_session=source, source_dir="/src", include=("base/F", "wal/"))

    assert plan.as_dict()["files"] == [{"path": "base/F/backup_manifest", "size_bytes": 7},
                                       {"path": "wal/000000010000000000000001", "size_bytes": 16}]
    assert plan.pending == [], "which of them still travel is the target's to say, and no target was opened"
    assert source.commands == [], "nothing run"


def test_a_changed_size_travels_again():
    """Same name, different size = not the same file."""
    tree_src = {"/src": [("a.bak", 300, False)]}
    tree_dst = {"/dst": [("a.bak", 100, False), (".dbabrain-staging", 1, False)]}

    result = _prepare(tree_src, _Sftp(tree_dst))

    assert [(rel, size) for rel, size, _mtime in result.pending] == [("a.bak", 300)]
    assert result.skipped == 0


def test_a_staging_dir_the_ssh_user_cannot_write_fails_before_anything_is_sent():
    """A read-only target is invisible to every other check: it lists fine and refuses every file.

    Left to the tar stream this copy once was, `tar -x` complained once per file into a stderr
    nobody could read until the command ended - which it never did while the copy was still going -
    so the source pushed 8.25 GB into a stalled pipe, the target wrote nothing, and the run sat at
    RUNNING for its full two-hour timeout. Measured on CLOUD2 after a staging directory was
    recreated with sudo and came back root-owned. One probe file up front is the difference between
    that and a sentence - and it is still asked before a single file is named as travelling."""
    target_sftp = _Sftp({"/dst": []}, writable=False)

    with pytest.raises(PermissionError, match="not writable by the SSH user"):
        _prepare({"/src": [("a.bak", 100, False)]}, target_sftp)

    assert target_sftp.probed == [], "refused at the probe: not even the marker was written"


def test_the_probe_file_is_cleaned_up_on_a_writable_target():
    tree = {"/src": [("a.bak", 100, False)], "/dst": [("a.bak", 100, False), (".dbabrain-staging", 1, False)]}
    target_sftp = _RemovingSftp(tree)

    _prepare(tree, target_sftp)

    assert target_sftp.probed == ["/dst/.dbabrain_write_probe"]
    assert target_sftp.probe_removed == ["/dst/.dbabrain_write_probe"]


# The staging prune itself (`prune_target_dir`: one `find -delete`, one `rm -rf`) is gone since
# 0.38.0 - a staged file is deleted by its own verified command. What it held - a folder empty by
# design inside a live set stays, only a marked folder is cleaned, the marker is never deleted,
# no retention means no cleanup - is held in test_a_backup_file_is_deleted_only_by_a_verified_command.py.


class _RemovingSftp(_Sftp):
    """Records every delete asked of the target - the write probe's own apart."""

    def __init__(self, tree):
        super().__init__(tree)
        self.removed = []
        self.probe_removed = []
    def remove(self, path):
        if path.endswith(".dbabrain_write_probe"):      # the writability probe
            self.probe_removed.append(path)
        else:
            self.removed.append(path)
    def rmdir(self, path):
        self.removed.append(path)


def test_the_staging_names_what_the_source_no_longer_has_and_removes_nothing():
    """A source rebuilt under the same name left its previous life's pieces staged beside the new
    ones; a gvenzl Oracle lab has the same DBID in every life, so RMAN mixed the two and a
    point-in-time duplicate died on the old life's logs (RMAN-06054, 2026-09-25). The copy mirrored
    them away until 0.38.0 - a delete held to nothing. It names them now, and the restore deletes
    each by a verified command or does not run (test_a_backup_file_is_deleted_only_by_...)."""
    tree_src = {"/src": [("new_life.bkp", 100, False)]}
    tree_dst = {"/dst": [("new_life.bkp", 100, False), ("old_life.bkp", 100, False), (".dbabrain-staging", 1, False)]}
    target_sftp = _RemovingSftp(tree_dst)

    result = _prepare(tree_src, target_sftp)

    assert target_sftp.removed == []
    assert (result.absent, result.absent_dirs) == (["old_life.bkp"], [])
    assert result.as_dict()["absent_at_source"] == ["old_life.bkp"]
    assert result.as_dict()["folders_absent_at_source"] == []
    assert "removed_absent_at_source" not in result.as_dict()


def test_a_file_the_source_still_has_is_not_named_even_when_this_run_does_not_copy_it():
    """`include` narrows what one run COPIES, not what the staging folder may hold - an older point
    in time needs pieces a newest-chain copy skipped."""
    tree_src = {"/src": [("full_old.bak", 100, False), ("full_new.bak", 100, False)]}
    tree_dst = {"/dst": [("full_old.bak", 100, False), (".dbabrain-staging", 1, False)]}
    target_sftp = _RemovingSftp(tree_dst)

    result = _prepare(tree_src, target_sftp, include=("full_new",))

    assert result.absent == [], "judged against the whole source, not the included part"
    assert [rel for rel, _size, _mtime in result.pending] == ["full_new.bak"]
    assert target_sftp.removed == []


# --------------------------------------------------------------------------- #
# Only a marked staging directory is prepared (review 0.25.0, B4.7)
# --------------------------------------------------------------------------- #
def test_an_unmarked_directory_that_holds_files_is_refused_and_nothing_is_deleted():
    """A target_backup_dir pointed at the target host's own backups used to be emptied by the
    mirror: every file the source lacked was removed. Refused, it is not even named for deletion."""
    tree_src = {"/src": [("a.bak", 100, False)]}
    tree_dst = {"/dst": [("the_targets_own_backup.bak", 100, False)]}
    target_sftp = _RemovingSftp(tree_dst)

    with pytest.raises(transfer.StagingDirError, match="touch /dst/.dbabrain-staging"):
        _prepare(tree_src, target_sftp)
    assert target_sftp.removed == []
    assert "/dst/.dbabrain-staging" not in target_sftp.probed, "and it is not marked as this tool's either"


def test_an_empty_directory_is_marked_on_the_first_copy():
    target_sftp = _Sftp({"/dst": []})

    _prepare({"/src": [("a.bak", 100, False)]}, target_sftp)

    assert "/dst/.dbabrain-staging" in target_sftp.probed


def test_the_marker_is_never_named_as_absent_at_the_source():
    """The source has no marker, and the folder's own marker is not a file to delete for that."""
    tree_src = {"/src": [("a.bak", 100, False)]}
    target_sftp = _RemovingSftp({"/dst": [("a.bak", 100, False), (".dbabrain-staging", 1, False)]})

    result = _prepare(tree_src, target_sftp)

    assert result.absent == []
    assert target_sftp.removed == []


def test_an_earlier_staging_copy_of_this_source_is_adopted_without_a_manual_step():
    """A staging folder from before the marker shares files with its source: it is marked and
    used as usual, no `touch` needed (owner, 2026-10-01: fixes must not add manual steps)."""
    tree_src = {"/src": [("a.bak", 100, False), ("b.bak", 100, False)]}
    target_sftp = _RemovingSftp({"/dst": [("a.bak", 100, False), ("gone_at_source.bak", 100, False)]})
    lines = []

    result = _prepare(tree_src, target_sftp, log=lines.append)

    assert "/dst/.dbabrain-staging" in target_sftp.probed
    assert target_sftp.removed == []
    assert result.as_dict()["absent_at_source"] == ["gone_at_source.bak"]
    assert any("adopted" in line for line in lines)
    assert any("named, not removed" in line for line in lines)


# --------------------------------------------------------------------------- #
# The marker's name is the request's to state (0.29.0)
# --------------------------------------------------------------------------- #
EARLIER_MARKER = ".dbops-staging"


def test_a_folder_emptied_down_to_an_earlier_marker_is_refused_under_the_default():
    """Why the default could not simply take a new name. Retention prunes every staged file and
    never the marker, so between two runs the folder holds the marker alone - nothing that is at
    the source - and a marker under a name this code does not look for is one more unknown file."""
    target_sftp = _RemovingSftp({"/dst": [(EARLIER_MARKER, 1, False)]})

    with pytest.raises(transfer.StagingDirError, match="touch /dst/.dbabrain-staging"):
        _prepare({"/src": [("a.bak", 100, False)]}, target_sftp)
    assert target_sftp.removed == []


def test_the_same_folder_is_this_tools_when_the_request_names_the_marker_it_carries():
    target_sftp = _RemovingSftp({"/dst": [(EARLIER_MARKER, 1, False)]})

    result = _prepare({"/src": [("a.bak", 100, False)]}, target_sftp, marker=EARLIER_MARKER)

    assert result.absent == [], "the marker is the folder's own, not a file the source lacks"
    assert target_sftp.removed == []
    assert "/dst/.dbabrain-staging" not in target_sftp.probed, "no second marker beside the first"


def test_a_refusal_names_the_marker_the_request_asked_for():
    target_sftp = _RemovingSftp({"/dst": [("the_targets_own_backup.bak", 100, False)]})

    with pytest.raises(transfer.StagingDirError, match="touch /dst/.dbops-staging"):
        _prepare({"/src": [("a.bak", 100, False)]}, target_sftp, marker=EARLIER_MARKER)


@pytest.mark.parametrize("stated, name", [
    (None, ".dbabrain-staging"), ("", ".dbabrain-staging"), ("  .dbops-staging ", ".dbops-staging")])
def test_a_request_that_names_no_marker_takes_the_default(stated, name):
    assert transfer.marker_name(stated) == name


@pytest.mark.parametrize("stated", ["staging", "a.bak", "../.x", ".x/y", ".", "..", ".a b", r"C:\.x"])
def test_a_marker_is_one_hidden_file_name_and_never_a_path(stated):
    """It is joined to the staging folder and handed to `find -name`; and a name a backup set could
    carry would make the marker a file the staging and the lister treat as a backup."""
    with pytest.raises(ValueError, match="staging_marker"):
        transfer.marker_name(stated)
