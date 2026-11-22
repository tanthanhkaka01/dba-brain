"""The worker's daemon is handed its passphrase in a file it reads once and removes (0.42.0, 1.409).

The worker's container used to carry `DBABRAIN_SECRET_KEY` in its environment, and Docker keeps a
container's environment in its configuration: `docker inspect` printed it to the `docker` group and
the container's `config.v2.json` held it in clear on the host's disk, through reboots and in every
copy of the VM (plan `audits/20261120_plan_worker_secret_exposure.md`, F1/F2). Now:

- a daemon whose node names `DBABRAIN_UNLOCK_FILE` starts **locked** - it opens nothing until a
  passphrase that opens the secret store arrives in that file, and removes the file whatever it
  held, so no secret is left at rest;
- the master writes it there (`control unlock-worker`) through `docker exec -i`, from a script on
  stdin, waits for the daemon to say `unlocked`, and takes back a file the daemon did not read;
- `deploy-code` turns the compose file's passphrase entry into the unlock file and mounts the
  host's tmpfs; `worker-run` and `worker-status` hand a command its passphrase on stdin - the
  latter used to put it on the host's argv.
"""

from __future__ import annotations

import base64
import json
import os
import shutil
import subprocess
import sys

import pytest

from dbabrain.control import deploy_code, worker_status, worker_unlock
from dbabrain.jobs import unlock
from dbabrain.lib.secret_text import encrypt_secret_text

SECRET = "pass phrase with 'quotes' and $dollars #1"


@pytest.fixture
def store(tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    (data / "encrypted_secret_text.json").write_text(
        json.dumps(encrypt_secret_text({"REF": "value"}, SECRET, iterations=1000)), encoding="utf-8")
    return data


# --------------------------------------------------------------------------- #
# The daemon's half
# --------------------------------------------------------------------------- #
def test_the_file_is_read_once_and_removed(tmp_path):
    path = tmp_path / "unlock"
    path.write_text(SECRET + "\n", encoding="utf-8")

    assert unlock.take(path) == SECRET
    assert not path.exists()
    assert unlock.take(path) is None


def test_a_passphrase_that_opens_the_store_unlocks_and_the_state_says_so(tmp_path, store):
    path = tmp_path / "unlock"
    path.write_text(SECRET, encoding="utf-8")
    events = []

    key = unlock.wait_for_unlock(path, data_dir=store, sleep=lambda _s: None,
                                 on_event=lambda name, fields: events.append(name))

    assert key == SECRET and not path.exists()
    state = unlock.read_state(path)
    assert state["state"] == "unlocked" and SECRET not in json.dumps(state)
    assert events == ["app.daemon.locked", "app.daemon.unlocked"]


def test_a_wrong_passphrase_is_refused_removed_and_said_and_the_daemon_keeps_waiting(tmp_path, store):
    path = tmp_path / "unlock"
    path.write_text("not-the-passphrase", encoding="utf-8")
    seen = []

    def sleep(_seconds):
        seen.append(unlock.read_state(path))
        assert not path.exists(), "a refused passphrase is not left at rest"
        path.write_text(SECRET, encoding="utf-8")       # the operator tries again

    assert unlock.wait_for_unlock(path, data_dir=store, sleep=sleep) == SECRET
    assert seen[0]["state"] == "locked" and "does not open the store" in seen[0]["reason"]
    assert "not-the-passphrase" not in json.dumps(seen[0])


def test_a_daemon_stopped_while_locked_leaves_a_state_that_says_stopped(tmp_path, store):
    path = tmp_path / "unlock"

    class Stop(Exception):
        pass

    def sleep(_seconds):
        raise Stop

    with pytest.raises(Stop):
        unlock.wait_for_unlock(path, data_dir=store, sleep=sleep)
    assert unlock.read_state(path)["state"] == "stopped"


def test_the_daemon_waits_for_the_passphrase_before_it_opens_the_store(tmp_path, monkeypatch):
    from dbabrain.jobs import daemon

    monkeypatch.delenv("DBABRAIN_SECRET_KEY", raising=False)
    monkeypatch.setenv("DBABRAIN_UNLOCK_FILE", str(tmp_path / "unlock"))
    order = []

    class Opened(Exception):
        pass

    def from_config(_config, **_kwargs):
        order.append(("store", os.environ.get("DBABRAIN_SECRET_KEY")))
        raise Opened

    monkeypatch.setattr(daemon, "load_config", lambda _path: type("C", (), {"log_dir": tmp_path,
                                                                            "runtime_dir": tmp_path})())
    monkeypatch.setattr(daemon, "resolve_config_path", lambda *_a: tmp_path / "config.json")
    monkeypatch.setattr(daemon, "patch_stdout", lambda *_a, **_k: None)
    monkeypatch.setattr(daemon, "setup_app_logger", lambda *_a, **_k: None)
    monkeypatch.setattr(daemon, "install_shutdown_handlers", lambda: None)
    monkeypatch.setattr(daemon.daemon_unlock, "wait_for_unlock",
                        lambda path, **_k: order.append(("wait", str(path))) or SECRET)
    monkeypatch.setattr(daemon.DbaBrainStore, "from_config", staticmethod(from_config))

    daemon.main(["--data-dir", str(tmp_path)])

    assert order == [("wait", str(tmp_path / "unlock")), ("store", SECRET)]
    monkeypatch.delenv("DBABRAIN_SECRET_KEY")


def test_a_node_given_its_passphrase_another_way_does_not_wait(tmp_path, monkeypatch):
    from dbabrain.jobs import daemon

    monkeypatch.setenv("DBABRAIN_SECRET_KEY", SECRET)
    monkeypatch.setenv("DBABRAIN_UNLOCK_FILE", str(tmp_path / "unlock"))
    monkeypatch.setattr(daemon, "load_config", lambda _path: type("C", (), {"log_dir": tmp_path,
                                                                            "runtime_dir": tmp_path})())
    monkeypatch.setattr(daemon, "resolve_config_path", lambda *_a: tmp_path / "config.json")
    monkeypatch.setattr(daemon, "patch_stdout", lambda *_a, **_k: None)
    monkeypatch.setattr(daemon, "setup_app_logger", lambda *_a, **_k: None)
    monkeypatch.setattr(daemon, "install_shutdown_handlers", lambda: None)
    monkeypatch.setattr(daemon.daemon_unlock, "wait_for_unlock",
                        lambda *_a, **_k: pytest.fail("the passphrase is already here"))

    def from_config(_config, **_kwargs):
        raise RuntimeError("stop here")

    monkeypatch.setattr(daemon.DbaBrainStore, "from_config", staticmethod(from_config))
    assert daemon.main(["--data-dir", str(tmp_path)]) != 0


# --------------------------------------------------------------------------- #
# The master's half
# --------------------------------------------------------------------------- #
class _Run:
    def __init__(self, stdout="", exit_code=0):
        self.stdout, self.stderr, self.exit_code = stdout, "", exit_code

    @property
    def ok(self):
        return self.exit_code == 0


class _Worker:
    """A container whose daemon reads the file the way `jobs.unlock` does."""

    def __init__(self, *, state="locked", taker=None, configured=True):
        self.state = {"state": state, "at": "0"}
        self.file = None
        self.taker = taker or (lambda key: {"state": "unlocked", "at": "1"})
        self.configured = configured
        self.scripts: list[str] = []
        self.commands: list[str] = []

    def run(self, command):
        self.commands.append(command)
        if "rm -f" in command:
            self.file = None
            return _Run()
        if not self.configured:
            return _Run(exit_code=3)
        if self.file is not None and self.taker:
            self.state, self.file = self.taker(self.file), None
        waiting = "pending" if self.file is not None else "none"
        return _Run(f"/run/dbabrain/unlock\n{waiting}\n{json.dumps(self.state)}\n")

    def run_script(self, script):
        self.scripts.append(script)
        encoded = script.split("printf %s '", 1)[1].split("'", 1)[0]
        self.file = base64.b64decode(encoded).decode("utf-8")
        return _Run()


def _unlock(worker, **kwargs):
    said = []
    code = worker_unlock.unlock_worker(worker, container="dbabrain", secret_key=SECRET,
                                       sleep=lambda _s: None, say=said.append, **kwargs)
    return code, said


def test_a_locked_daemon_is_handed_its_passphrase_and_says_unlocked():
    worker = _Worker()
    code, said = _unlock(worker)

    assert code == 0 and "unlocked" in said[-1]
    assert len(worker.scripts) == 1 and SECRET not in worker.scripts[0]
    first_line = worker.scripts[0].splitlines()[0]
    assert first_line.startswith("#"), "the one line the remote gate echoes carries nothing"


def test_a_daemon_that_refuses_the_passphrase_is_reported():
    worker = _Worker(taker=lambda key: {"state": "locked", "at": "1", "error": "wrong key or corrupted file"})
    code, said = _unlock(worker)

    assert code == 1 and "refused" in said[-1]


def test_a_file_the_daemon_does_not_take_is_removed_again():
    worker = _Worker(taker=lambda key: None)
    worker.taker = None
    code, said = _unlock(worker, wait_seconds=1)

    assert code == 1 and "removed" in said[-1]
    assert worker.file is None and any("rm -f" in command for command in worker.commands)


def test_an_unlocked_daemon_is_left_alone_unless_forced():
    worker = _Worker(state="unlocked")
    code, _said = _unlock(worker)
    assert code == 0 and worker.scripts == []

    code, _said = _unlock(worker, force=True)
    assert code == 0 and len(worker.scripts) == 1


def test_a_container_that_names_no_unlock_file_is_not_written_to():
    worker = _Worker(configured=False)
    code, said = _unlock(worker)
    assert code == 2 and worker.scripts == [] and "DBABRAIN_UNLOCK_FILE" in said[-1]


@pytest.mark.skipif(os.name == "nt" or shutil.which("sh") is None or shutil.which("base64") is None,
                    reason="runs the generated POSIX shell")
def test_the_scripts_hand_the_passphrase_over_on_no_argv(tmp_path):
    """`docker` is faked by a script that runs the `sh -c` it is given locally and records its argv."""
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    argv_log = tmp_path / "argv"
    (fake_bin / "docker").write_text(
        "#!/bin/sh\n"
        f'printf "%s\\n" "$*" >> {argv_log}\n'
        'while [ "$1" != "sh" ]; do shift; done\n'
        'exec "$@"\n', encoding="utf-8")
    (fake_bin / "docker").chmod(0o755)
    env = {**os.environ, "PATH": f"{fake_bin}:{os.environ['PATH']}",
           "DBABRAIN_UNLOCK_FILE": str(tmp_path / "run" / "unlock")}
    env.pop("DBABRAIN_SECRET_KEY", None)

    subprocess.run(["sh", "-s"], input=worker_unlock.unlock_write_script("dbabrain", SECRET),
                   text=True, check=True, env=env)
    written = tmp_path / "run" / "unlock"
    assert written.read_text(encoding="utf-8") == SECRET and oct(written.stat().st_mode)[-3:] == "600"

    shown = subprocess.run(["sh", "-s"], input=worker_unlock.keyed_exec_script(
        "dbabrain", SECRET, [sys.executable, "-c", "import os; print(os.environ['DBABRAIN_SECRET_KEY'])"]),
        text=True, check=True, env=env, capture_output=True)
    assert shown.stdout.strip() == SECRET
    assert SECRET not in argv_log.read_text(encoding="utf-8")


def test_worker_status_puts_the_passphrase_on_no_argv(monkeypatch):
    commands, scripts = [], []

    class Client:
        def run(self, command):
            return _Run(f"/run/dbabrain/unlock\nnone\n{json.dumps({'state': 'unlocked', 'at': '1'})}\n")

        def run_script(self, script):
            scripts.append(script)
            return _Run("status ok")

        def close(self):
            pass

    monkeypatch.setattr(worker_status, "ssh_connect", lambda *a, **k: Client())
    monkeypatch.setattr(worker_status, "ssh_capture",
                        lambda _c, command: commands.append(command) or (0, "dbabrain | Up", ""))
    worker_status.run_worker_status(host="192.0.2.10", user="ops", password="pw",
                                    key_base64=base64.b64encode(SECRET.encode()).decode())

    encoded = base64.b64encode(SECRET.encode()).decode()
    assert not any(encoded in command or "--key" in command for command in commands)
    assert len(scripts) == 1 and "dbabrain.db.cli node-status" in scripts[0] and SECRET not in scripts[0]


# --------------------------------------------------------------------------- #
# The compose file deploy-code writes
# --------------------------------------------------------------------------- #
LIST_STYLE = """services:
  dbabrain:
    image: dbabrain:1.0
    environment:
      - DBABRAIN_NODE_ROLE=worker
      - DBABRAIN_SECRET_KEY
    volumes:
      - ./data:/app/tools/dbabrain/data
"""


def test_a_list_style_entry_becomes_the_unlock_file_too():
    plan = deploy_code.plan_compose(LIST_STYLE, "dbabrain:1.1")

    assert "      - DBABRAIN_UNLOCK_FILE=/run/dbabrain/unlock\n" in plan.text
    assert "SECRET_KEY" not in plan.text
    assert plan.text.index("/run/dbabrain:/run/dbabrain") < plan.text.index("./data:")


def test_a_service_that_hands_over_no_passphrase_is_refused():
    with pytest.raises(deploy_code.DeployCodeError, match="no passphrase"):
        deploy_code.plan_compose(LIST_STYLE.replace("      - DBABRAIN_SECRET_KEY\n", ""), "dbabrain:1.1")


def test_another_services_secret_entry_is_not_touched():
    text = LIST_STYLE + "  other:\n    image: postgres:18\n    environment:\n      DBABRAIN_SECRET_KEY: x\n"
    plan = deploy_code.plan_compose(text, "dbabrain:1.1")
    assert plan.text.endswith("      DBABRAIN_SECRET_KEY: x\n")


def test_the_runtime_compose_file_of_a_full_deploy_hands_over_no_passphrase():
    from dbabrain.control._support import DBABRAIN_ROOT

    path = DBABRAIN_ROOT / "docker-compose.runtime.yml"
    if not path.is_file():
        pytest.skip("this tree does not carry the full deploy's compose file")
    text = path.read_text(encoding="utf-8")
    settings = [line for line in text.splitlines() if not line.lstrip().startswith("#")]
    assert not any("SECRET_KEY" in line for line in settings)
    assert "      DBABRAIN_UNLOCK_FILE: /run/dbabrain/unlock" in settings
    assert "      - /run/dbabrain:/run/dbabrain" in settings
