"""``db_ops.config`` is ``db_ops.lib.config`` - the configuration parser moved into ``lib`` in 0.24.0.

``common`` may import nothing but ``lib`` (rules R04), and it reads the configuration its
registrars write; ``lib/notify.py`` and ``lib/telegram_route.py`` read the notify levels from it,
and ``lib`` may import nothing outside ``lib`` (R06). So the parser is ``lib``'s now, and this name
stays for the sixty-odd importers and every monkeypatch that says ``db_ops.config``: the module
object itself is replaced, so both names are one module and a patch through either is seen by both.
"""

import sys

from db_ops.lib import config as _config

sys.modules[__name__] = _config
