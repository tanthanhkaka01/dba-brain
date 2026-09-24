"""`create-db-docker` says *Image not found* only when the tag is not in the registry.

On 2026-09-24, creating a PostgreSQL 18 lab on a lab host failed with

    Image not found: postgres:18. --version must be a tag that exists in the registry.
    Valid postgres tags include: 18, 17, 16 (or 18-alpine).

- refusing 18 and recommending it in the next line. A minute later, on the same host, `docker
manifest inspect postgres:18` answered 0 and the retry created the lab: the registry had failed
for a moment. Every non-zero exit was reported as a missing tag, and docker's own words were
thrown away, so a rate limit or a timeout sent the operator hunting for a typo.
"""

from __future__ import annotations

import types

import pytest

from db_ops.sre.docker_db import provisioner
from db_ops.sre.docker_db.models import DockerDbSpec
from db_ops.sre.docker_db.provisioner import ProvisionError

SPEC = DockerDbSpec(name="pg_lab", engine="postgres", version="18", host_port=5432, password_env="X")


def _answer(returncode, stderr):
    return lambda command, **_: types.SimpleNamespace(returncode=returncode, stdout="", stderr=stderr)


@pytest.mark.parametrize("stderr", [
    "no such manifest: docker.io/library/postgres:99",
    "manifest unknown: manifest unknown",
    "Error response from daemon: postgres:99 not found",
])
def test_a_tag_the_registry_does_not_have_is_image_not_found(stderr):
    with pytest.raises(ProvisionError, match="Image not found: postgres:18"):
        provisioner.check_image_exists(SPEC, _answer(1, stderr))


@pytest.mark.parametrize("stderr", [
    "toomanyrequests: You have reached your pull rate limit.",
    "Get \"https://registry-1.docker.io/v2/\": net/http: request canceled while waiting for connection",
    "dial tcp: lookup registry-1.docker.io: no such host",
])
def test_a_registry_that_does_not_answer_is_said_to_be_that(stderr):
    with pytest.raises(ProvisionError) as raised:
        provisioner.check_image_exists(SPEC, _answer(1, stderr))

    text = str(raised.value)
    assert "Image not found" not in text and "not a wrong --version" in text
    assert stderr.split(":")[0] in text, "docker's own words are kept"


def test_a_failure_with_nothing_said_names_the_exit_code():
    with pytest.raises(ProvisionError, match="exited 1"):
        provisioner.check_image_exists(SPEC, _answer(1, ""))


def test_a_tag_that_exists_passes():
    provisioner.check_image_exists(SPEC, _answer(0, ""))
