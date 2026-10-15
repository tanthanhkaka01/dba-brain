"""JSON that is safe to paste into an HTML ``<script>`` block.

``json.dumps`` escapes nothing an HTML parser cares about: a value containing ``</script>`` ends the
block and everything after it is parsed as markup. The reports embed values collected from the
monitored estate - database, login, job and linked-server names, error text - so anyone able to name
an object on a monitored instance could run script in every browser that opened a report, on the
console's origin (review 0.25.0, B7.1). Escaping the characters that can start a tag or an entity,
and the two line separators JavaScript treats as newlines, keeps the text valid JSON and closes it.
"""

from __future__ import annotations

import json
from typing import Any

__all__ = ["json_for_html"]

_ESCAPES = {"<": "\\u003c", ">": "\\u003e", "&": "\\u0026", " ": "\\u2028", " ": "\\u2029"}


def json_for_html(payload: Any, **dumps_kwargs: Any) -> str:
    """``json.dumps(payload, **dumps_kwargs)`` with ``< > &`` and U+2028/2029 escaped."""
    dumps_kwargs.setdefault("ensure_ascii", False)
    text = json.dumps(payload, **dumps_kwargs)
    for raw, escaped in _ESCAPES.items():
        text = text.replace(raw, escaped)
    return text
