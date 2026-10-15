"""dbabrain control app — master-side operations (deploy, versioning, inventory build).

Runs on the master node (the PC) to drive the worker daemon. Like the other dbabrain apps
it exposes a CLI (``python -m dbabrain.control.cli``) and is fully self-contained; it invokes
other apps (e.g. the reports ``build-inventory-health`` command) over SSH/subprocess
rather than importing them.
"""
