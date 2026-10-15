"""dbabrain Python helpers for ORD 46."""

__all__ = ["__version__"]

# The version's one source is dbabrain/lib/version.py (0.24.0): `common` may import nothing but
# `lib`, and it reports the version. Re-exported so `dbabrain.__version__` reads as it always did.
from dbabrain.lib.version import __version__  # noqa: E402
