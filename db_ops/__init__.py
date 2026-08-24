"""DB Ops Python helpers for ORD 46."""

__all__ = ["__version__"]

# Single source of truth for the db_ops version. Bump this on every fix/change;
# the Docker build (docker/build_and_package.ps1) reads it to tag the image
# (db_ops:<version>). Format: MAJOR.MINOR.PATCH (zero-padded).
__version__ = "0.4.0"
