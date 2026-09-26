"""The internal version - its one source (0.24.0).

It lived in ``db_ops/__init__.py`` until 0.24.0, which ``common/cli.py`` had to import to report
it - the one import outside ``lib`` that ``common`` kept (rules R04). ``db_ops/__init__.py``
re-exports it, so ``db_ops.__version__`` is unchanged for every reader; ``bump-version``, the
export, ``pyproject.toml`` and ``docker/build_and_package.ps1`` all read or write this file.
Format: MAJOR.MINOR.PATCH, zero-padded. The released number is ``PUBLIC_VERSION`` in
``lib/distribution.py``, which is separate.
"""

__version__ = "0.24.0"
