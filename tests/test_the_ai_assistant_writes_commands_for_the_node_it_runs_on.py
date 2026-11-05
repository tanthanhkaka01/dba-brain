"""The assistant knows how its node runs, so the command lines it proposes are written for that
shell and that install (0.35.0, 1.267).

The operator, 2026-10-31: *say how and where each command runs - a CLI, a Telegram command, the
console - and for which environment: Windows, Docker, Ubuntu.* A line written for the wrong shell is a
line that fails: single-quoted JSON breaks in Windows PowerShell 5.1, a container is reached with
``docker exec`` from its host, a source checkout has no ``dbabrain`` on PATH.

* the node's facts come from ``lib.node_environment``, which ``self-status`` now imports too - one
  answer, not two;
* the summary names the form of the command for each install;
* the system prompt carries it, and says to ask when it is not known;
* the guide the assistant reads tells it to name each command's kind and place, and how to set up a
  new install - in the order ``agents_guide.md`` gives.
"""

from __future__ import annotations

from dbabrain.common import self_status
from dbabrain.lib import ai_assist as A
from dbabrain.lib import node_environment
from dbabrain.telegram import ai_assist as job

NODE = {"role": "worker", "backend": "postgresql", "timezone": "+07:00"}


def test_self_status_reads_the_same_facts_as_the_assistant():
    assert self_status.runtime is node_environment.runtime
    assert self_status.operating_system is node_environment.operating_system


def test_the_facts_are_the_four_and_where():
    facts = node_environment.describe(".")
    assert set(facts) == {"os", "runtime", "install", "shell", "tool_root", "python"}
    assert facts["install"] in {"pip", "source"}
    assert facts["shell"] in {"powershell-5.1", "powershell-7", "bash"}


def test_each_install_gets_its_own_form_of_the_command():
    base = {"os": "Windows 11", "shell": "powershell-5.1", "tool_root": r"D:\dbabrain",
            "python": r"D:\dbabrain\.venv\Scripts\python.exe"}
    source = node_environment.summary_line({**base, "runtime": "host", "install": "source"})
    assert r"D:\dbabrain\.venv\Scripts\python.exe -m dbabrain.<app>.cli" in source
    assert "never the one on PATH" in source
    pip = node_environment.summary_line({**base, "runtime": "host", "install": "pip"})
    assert "`dbabrain <app> ...`" in pip
    container = node_environment.summary_line({**base, "os": "Ubuntu 24.04", "shell": "bash",
                                               "runtime": "docker", "install": "pip"})
    assert "docker exec <container> dbabrain" in container
    assert "shell powershell-5.1" in source and "shell bash" in container


def test_the_system_prompt_says_how_the_node_runs():
    settings = A.parse_ai_assist({**A.shipped_document(), "users": []})
    line = "OS Ubuntu 24.04; shell bash; tool root /app/tools/dbabrain; inside a docker container"
    system = job.system_prompt(settings, node={**NODE, "environment": line})
    assert f"How it runs: {line}" in system


def test_without_the_facts_it_asks_instead_of_guessing():
    settings = A.parse_ai_assist({**A.shipped_document(), "users": []})
    assert "ask the user before writing a command line" in job.system_prompt(settings, node=NODE)


def test_the_guide_asks_for_kind_place_and_shell_and_orders_a_new_install():
    guide = job.GUIDE_PATH.read_text(encoding="utf-8")
    for needed in ("say how and where it runs", "**CLI**", "**Telegram command**", "**Web console**",
                   "**On another machine**", "@request.json", "docker exec", "A new install"):
        assert needed in guide, needed
    setup = guide[guide.index("## A new install"):]
    order = [setup.index(step) for step in ("timezone --set", "instance-add", "use-bot", "backup-add",
                                            "level `ai_assist`")]
    assert order == sorted(order), "the setup steps keep agents_guide.md's order"


def test_init_tells_a_new_install_the_assistant_exists_and_what_it_needs(tmp_path):
    from dbabrain.common import scaffold

    steps = scaffold.next_steps(tmp_path)
    for needed in ("AI assistant", "dbabrain[ai]", "--level ai_assist", "users[]"):
        assert needed in steps, needed
