"""An SSH hop dbabrain runs pins the host key it meets, and refuses one that changes (1.223, 2026-11-11).

Why it matters: the operator, 2026-11-11 - *1.223: do it*. The SRE app's hop through the bastion and
the lab AG helper ran ``ssh -o StrictHostKeyChecking=no`` (the hop also with ``/dev/null`` as its
known hosts): any key, every time, so a machine answering in a node's place was trusted. They now
pin the key on first use in the bastion's known_hosts and refuse a changed one (``accept-new``).

The one place left as it is: ``vmware-common.ps1``'s guest SSH, which provisions lab VMs that are
destroyed and rebuilt on the same address with a new key each time - pinning there would refuse
every rebuild. It is named here so a new one cannot slip in beside it.
"""

from __future__ import annotations

import re
from pathlib import Path

from dbabrain.sre import service

ROOT = Path(service.__file__).resolve().parents[1]
#: Rebuilt-on-the-same-address lab VMs (see the docstring) - the only shell-out allowed "no".
ALLOWED = {"sre/automation/powershell/vmware-common.ps1"}


def test_the_bastion_hop_pins_the_host_key_on_first_use():
    assert service.HOP_HOST_KEY_OPTIONS == "-o StrictHostKeyChecking=accept-new"
    source = Path(service.__file__).read_text(encoding="utf-8")
    hop = source[source.index("hop = ("):source.index("if target_host != bastion")]
    assert "HOP_HOST_KEY_OPTIONS" in hop and "/dev/null" not in hop


def test_no_shell_out_turns_host_key_checking_off_but_the_lab_vm_provisioning():
    found = []
    for path in ROOT.rglob("*"):
        if path.suffix not in (".py", ".sh", ".ps1") or "__pycache__" in path.parts:
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        for line in text.splitlines():
            code = line.split("#", 1)[0] if path.suffix != ".py" else line
            if re.search(r"StrictHostKeyChecking=no\b", code) and "It was StrictHostKeyChecking=no" not in line:
                found.append(path.relative_to(ROOT).as_posix())
    assert set(found) <= ALLOWED, sorted(set(found) - ALLOWED)
