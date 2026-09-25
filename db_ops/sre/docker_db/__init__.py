"""The connection registry for the lab database instances ``sre`` asks ``common`` to build.

``sre.cli create-db-docker`` / ``move-db-docker`` resolve the passwords and the SSH logins, call
``common.cli`` to do the work (:mod:`db_ops.common.docker_db`), and record the result here, in
``data/docker_db_connections.json``. The provisioner and the mover were this package until
0.23.0; what stayed is the part that is ``sre``'s own file.
"""
