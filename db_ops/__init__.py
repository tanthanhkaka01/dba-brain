"""DB Ops Python helpers for ORD 46."""

__all__ = ["__version__"]

# The version's one source is db_ops/lib/version.py (0.24.0): `common` may import nothing but
# `lib`, and it reports the version. Re-exported so `db_ops.__version__` reads as it always did.
from db_ops.lib.version import __version__  # noqa: E402
