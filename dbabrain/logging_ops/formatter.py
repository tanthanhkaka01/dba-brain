from __future__ import annotations


LOG_HEADER = "DATE|LOGTYPE|APP|HOST|FUNCTION|TEXT"


def format_function_message(function_name: str, text: str = "") -> str:
    if text:
        return f"{function_name}|{text}"
    return f"{function_name}|"
