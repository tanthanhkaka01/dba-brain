"""SRE lab database Docker provisioning.

Create/manage single or HA-lab database Docker instances (postgres/mysql/mssql)
on a worker, and register their connection details under ``data/``. Designed to
run inside the worker container via ``control.cli worker-run`` (reusing the
existing remote-execution path), or directly on any host with Docker.
"""

from __future__ import annotations

from db_ops.sre.docker_db.models import (
    ENGINE_META,
    HA_SUPPORTED_ENGINES,
    VALID_ENGINES,
    VALID_MODES,
    DockerDbSpec,
)
from db_ops.sre.docker_db.provisioner import (
    DEFAULT_CONTAINERS_DIR,
    ProvisionError,
    provision,
)

__all__ = [
    "DockerDbSpec",
    "ENGINE_META",
    "HA_SUPPORTED_ENGINES",
    "VALID_ENGINES",
    "VALID_MODES",
    "DEFAULT_CONTAINERS_DIR",
    "ProvisionError",
    "provision",
]
