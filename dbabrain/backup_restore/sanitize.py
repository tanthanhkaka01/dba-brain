from __future__ import annotations

import re
from pathlib import Path
from typing import Any


SENSITIVE_KEYS = {
    "access_key",
    "api_key",
    "authorization",
    "credential",
    "key",
    "passwd",
    "password",
    "private_key",
    "private_key_password",
    "pwd",
    "secret",
    "token",
}


def sanitize_text(value: object) -> str:
    text = str(value)
    replacements = (
        (r"(?i)(\bsqlcmd\b(?:.|\n)*?\s-P\s+)'[^']*'", r"\1'***'"),
        (r"(?i)(\bsqlcmd\b(?:.|\n)*?\s-P\s+)\"[^\"]*\"", r'\1"***"'),
        (r"(?i)(\bsqlcmd\b(?:.|\n)*?\s-P\s+)(?!['\"])\S+", r"\1***"),
        (r"(?i)(^|\s)(-P\s+)'[^']*'", r"\1\2'***'"),
        (r"(?i)(^|\s)(-P\s+)\"[^\"]*\"", r'\1\2"***"'),
        (r"(?i)(^|\s)(-P\s+)(?!['\"])\S+", r"\1\2***"),
        (r"(?i)('-P'\s*,\s*)'[^']*'", r"\1'***'"),
        (r'(?i)("-P"\s*,\s*)"[^"]*"', r'\1"***"'),
        (r"(?i)(ConvertTo-SecureString\s+)'[^']*'", r"\1'***'"),
        (r"(?i)(ConvertTo-SecureString\s+)\"[^\"]*\"", r'\1"***"'),
        (r"(?i)(ConvertTo-SecureString\s+)(?!['\"])\S+", r"\1***"),
        (r"(?i)(DECRYPTION\s+BY\s+PASSWORD\s*=\s*N?)(?:'[^']*'|\"[^\"]*\")", r"\1'***'"),
        (r"(?i)(Authorization\s*[:=]\s*Bearer\s+)(\S+)", r"\1***"),
        (r"(?i)\b(password|passwd|pwd|secret|token|api_key|credential|access_key|private_key)\s*=\s*([^;\s,\}\]]+)", r"\1=***"),
        (r"(?i)(/pass:)(\S+)", r"\1***"),
    )
    for pattern, replacement in replacements:
        text = re.sub(pattern, replacement, text)
    return text


def sanitize_value(value: Any) -> Any:
    if isinstance(value, dict):
        sanitized: dict[Any, Any] = {}
        for key, item in value.items():
            key_text = str(key).lower()
            if any(sensitive in key_text for sensitive in SENSITIVE_KEYS):
                sanitized[key] = "***"
            else:
                sanitized[key] = sanitize_value(item)
        return sanitized
    if isinstance(value, list):
        sanitized_list: list[Any] = []
        redact_next = False
        for item in value:
            if redact_next:
                sanitized_list.append("***")
                redact_next = False
                continue
            sanitized_item = sanitize_value(item)
            sanitized_list.append(sanitized_item)
            if isinstance(item, str) and item.lower() in {"-p", "/pass"}:
                redact_next = True
        return sanitized_list
    if isinstance(value, tuple):
        return tuple(sanitize_value(item) for item in value)
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, str):
        return sanitize_text(value)
    return value


def compact_log_value(value: object) -> str:
    return sanitize_text(value).replace("\r", "\\r").replace("\n", "\\n")
