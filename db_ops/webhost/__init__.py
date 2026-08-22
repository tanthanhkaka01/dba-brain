"""db_ops web host app.

A small static web server (Python ``http.server``) that publishes the db_ops report
directory over HTTP so reports can be viewed via a stable link instead of opening files.

It is designed to run as a long-running daemon app_command on the worker node
(``repeat_interval=0`` run-once + ``timeout=0`` no-kill + ``retry_interval=0`` restart-now),
so the daemon starts it once and restarts it if it ever dies.
"""
