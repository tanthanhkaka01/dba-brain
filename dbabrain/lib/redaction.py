"""What a secret looks like on its way out, and taking it out (0.34.0; moved down in 0.38.0, 1.312).

Two layers, and both are needed:

* :func:`mask_sensitive_text` - the SHAPES a secret is printed in (``password=x``, ``-P x``,
  ``--key x``, ``sys/x@tns``, ``IDENTIFIED BY x``). It catches a value nobody told it about.
* :class:`Redactor` - every VALUE of the encrypted secret store, taken out wherever it appears, as
  itself and as it is spelt inside JSON. It catches a password printed in plain prose.

Moved here from ``telegram`` when a second app needed them: the MCP server (``dbabrain mcp``) hands
every answer to a model, exactly as the Telegram assistant does. Pure.
"""

from __future__ import annotations

import json
import re


def mask_sensitive_text(text: str) -> str:
    masked = re.sub(r"(?i)(password|token|secret|pwd)\s*[:=]\s*[^,\s]+", r"\1=***", str(text))
    masked = re.sub(r"(?i)(-P\s+)(\"[^\"]+\"|'[^']+'|\S+)", r"\1***", masked)
    masked = re.sub(r"(?i)(--key(?:-base64|_base64)?[=\s]+)(\"[^\"]+\"|'[^']+'|\S+)", r"\1***", masked)
    # Oracle/SQL shapes a DB tool echoes back on failure, which the key=value rules above
    # do not cover: `sys/secret@TNS` in a connect string, and `IDENTIFIED BY "secret"`.
    masked = re.sub(r"(?i)\b([A-Za-z][\w$#]*)/(\"[^\"]+\"|'[^']+'|[^\s@/]+)(@\S+)", r"\1/***\3", masked)
    masked = re.sub(r"(?i)(identified\s+by\s+)(\"[^\"]+\"|'[^']+'|\S+)", r"\1***", masked)
    return masked


class Redactor:
    """Every value of the encrypted secret store, taken out of a text wherever it appears."""

    def __init__(self, secrets: dict[str, str] | None = None) -> None:
        values = {str(value) for value in (secrets or {}).values() if len(str(value or "").strip()) >= 4}
        spellings: set[str] = set()
        for value in values:
            spellings.add(value)
            spellings.add(json.dumps(value)[1:-1])  # as it appears inside JSON
        # Longest first, so a secret that contains another is taken out whole.
        self.values = sorted(spellings, key=len, reverse=True)

    def __call__(self, text: str) -> str:
        out = str(text)
        for value in self.values:
            if value and value in out:
                out = out.replace(value, "***")
        return mask_sensitive_text(out)
