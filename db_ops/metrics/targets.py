"""``db_ops.metrics.targets`` is ``db_ops.lib.data_sources.collection_targets`` (moved in 0.24.0).

The loader answers "what is configured", which is ``lib.data_sources``' question, and
``common.cli check-credentials`` needs it - ``common`` may import only ``lib`` (rules R04, R41). The
module object itself is replaced, so both names are one module and a patch through either is seen
by both, as ``db_ops.config`` is ``db_ops.lib.config``.
"""

import sys

from db_ops.lib.data_sources import collection_targets as _collection_targets

sys.modules[__name__] = _collection_targets
