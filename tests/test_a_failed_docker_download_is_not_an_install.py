"""A Docker install that downloaded nothing must not report success - and must try apt instead.

On 2026-09-30 ``/spbot_create_db_docker ... install_docker=yes`` against a new test VM answered
*Docker is installed on 172.17.180.251 but not usable as 'tuser' without sudo* - on a host where not
even root had a ``docker``. The install step was ``curl -fsSL https://get.docker.com | sh`` under
``set -e`` and without ``pipefail``: the VM's DNS could not resolve ``get.docker.com``, ``curl``
failed, ``sh`` read an empty script and exited 0, and the pipe's status is its last command's. The
distro packages, which that VM's mirror could install, were tried only when ``curl`` was absent.

These run the real script in a real shell, with ``curl`` and ``apt-get`` replaced by stubs.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from db_ops.common.docker_db.remote_host import RemoteHostError, docker_install_script, ensure_docker

SH = shutil.which("sh")
pytestmark = pytest.mark.skipif(SH is None, reason="needs a POSIX sh")


def _stub(folder: Path, name: str, body: str) -> None:
    path = folder / name
    path.write_text("#!/bin/sh\n" + body + "\n", encoding="utf-8", newline="\n")
    path.chmod(0o755)


def _run(tmp_path: Path, *, apt_installs: bool) -> tuple[subprocess.CompletedProcess, str]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    log = tmp_path / "apt.log"
    # The download fails the way the VM's did: DNS, exit 6.
    _stub(bin_dir, "curl", "exit 6")
    _stub(bin_dir, "wget", "exit 4")
    installed_docker = f'printf "#!/bin/sh\\nexit 0\\n" > "{bin_dir.as_posix()}/docker"; chmod +x "{bin_dir.as_posix()}/docker"'
    _stub(bin_dir, "apt-get",
          f'echo "$@" >> "{log.as_posix()}"; '
          f'case "$1" in update) exit 100;; install) {installed_docker if apt_installs else "exit 100"};; esac')
    env = {**os.environ, "PATH": bin_dir.as_posix() + os.pathsep + os.environ.get("PATH", "")}
    if sys.platform == "win32":
        env["PATH"] = str(bin_dir) + os.pathsep + os.environ.get("PATH", "")
    run = subprocess.run([SH, "-c", docker_install_script()], env=env, capture_output=True, text=True)
    return run, (log.read_text(encoding="utf-8") if log.exists() else "")


def test_a_download_that_fails_falls_back_to_the_distro_packages(tmp_path):
    run, apt = _run(tmp_path, apt_installs=True)

    assert "install -y docker.io docker-compose-v2" in apt
    assert run.returncode == 0, run.stderr


def test_a_failing_apt_update_does_not_stop_the_install(tmp_path):
    """One unreachable index (a security mirror) fails `apt-get update`; the package still installs."""
    run, apt = _run(tmp_path, apt_installs=True)

    assert apt.splitlines()[0].startswith("update")
    assert run.returncode == 0


def test_when_nothing_could_install_docker_the_step_fails(tmp_path):
    run, _ = _run(tmp_path, apt_installs=False)

    assert run.returncode != 0


class _HostWithoutDocker:
    """The VM of 2026-09-30: every install step returns 0, and docker is never there."""

    user = "tuser"
    host = "10.0.0.9"

    def run(self, argv, **_):
        return subprocess.CompletedProcess(args=argv, returncode=127, stdout="", stderr="")

    def run_sudo(self, command_str, sudo_password, **_):
        code = 127 if command_str.startswith("docker ") else 0
        return subprocess.CompletedProcess(args=command_str, returncode=code, stdout="", stderr="")

    def reconnect(self):
        pass


def test_a_docker_that_even_root_cannot_find_is_called_not_installed():
    with pytest.raises(RemoteHostError, match="NOT installed") as caught:
        ensure_docker(_HostWithoutDocker(), sudo_password="pw")

    assert "not usable as" not in str(caught.value)
