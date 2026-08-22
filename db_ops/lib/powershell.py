"""Shaping PowerShell text: quoting, encoding, and the ``Invoke-Command`` wrapper.

Split out of ``common/remote_exec.py`` on 2026-08-15. Opening a WinRM session is an operation and
stayed there; **building the script is not an execution**, and two ``backup_restore`` modules had
been importing the transport to get one.

That separation is older than the split and is the reason these functions exist at all:
``backup_restore`` composes a remote script, hands the finished argv to *its own* runner — which
owns retry, timeout and progress logging — and then asserts on the built text to prove a command
is aimed at the host it was configured for. Those callers need the string, never a session.

Three quoting rules live here so nothing re-derives them:

* **Single quotes double.** ``'`` inside a PowerShell single-quoted literal is written ``''``;
  everything else is literal, which is what makes a password or a path safe to embed.
* **``-EncodedCommand`` sidesteps every quoting layer** between here and the remote shell — the
  script may contain quotes, newlines and non-ASCII text with no escaping at all. It is UTF-16LE
  then base64, in that order; base64 of UTF-8 produces a script PowerShell reads as mojibake.
* **``-ComputerName`` stays immediately after ``Invoke-Command``**, so the target-context guards
  can find it by prefix rather than by parsing PowerShell.
"""

from __future__ import annotations

import base64
from typing import Sequence

from db_ops.lib.shell import powershell_executable

__all__ = [
    "build_invoke_command_argv",
    "build_invoke_command_script",
    "encode_powershell_command",
    "quote_powershell",
]


def quote_powershell(value: str) -> str:
    """Quote a value as a PowerShell single-quoted string literal."""
    return "'" + str(value).replace("'", "''") + "'"


def encode_powershell_command(script_text: str) -> str:
    """Base64/UTF-16LE encoding used by ``powershell -EncodedCommand``."""
    return base64.b64encode(str(script_text).encode("utf-16le")).decode("ascii")


def build_invoke_command_script(
    *,
    host: str,
    script_body: str | Sequence[str],
    username: str = "",
    password: str = "",
    arguments: Sequence[str] = (),
    open_timeout_ms: int | None = None,
    operation_timeout_ms: int | None = None,
) -> str:
    """The PowerShell text that runs ``script_body`` on ``host`` via ``Invoke-Command``.

    What every caller shares, and all this builds, is the wrapper: the ``PSCredential`` lines, the
    session-option line, and the ``Invoke-Command`` invocation itself.
    """
    body = script_body if isinstance(script_body, str) else "\n".join(script_body)
    lines: list[str] = []
    credential_arg = ""
    if username and password:
        lines += [
            f"$securePassword = ConvertTo-SecureString {quote_powershell(password)} -AsPlainText -Force",
            f"$credential = New-Object System.Management.Automation.PSCredential "
            f"({quote_powershell(username)}, $securePassword)",
        ]
        credential_arg = " -Credential $credential"

    session_arg = ""
    if open_timeout_ms is not None or operation_timeout_ms is not None:
        options = []
        if open_timeout_ms is not None:
            options.append(f"-OpenTimeout {int(open_timeout_ms)}")
        if operation_timeout_ms is not None:
            options.append(f"-OperationTimeout {int(operation_timeout_ms)}")
        lines.append("$sessionOption = New-PSSessionOption " + " ".join(options))
        session_arg = " -SessionOption $sessionOption"

    argument_list = ""
    if arguments:
        argument_list = " -ArgumentList " + ", ".join(quote_powershell(arg) for arg in arguments)

    lines.append(
        f"Invoke-Command -ComputerName {quote_powershell(host)}{credential_arg}{session_arg} -ScriptBlock {{"
    )
    lines.append(body)
    lines.append("}" + argument_list)
    return "\n".join(lines)


def build_invoke_command_argv(
    *,
    host: str,
    script_body: str | Sequence[str],
    username: str = "",
    password: str = "",
    arguments: Sequence[str] = (),
    open_timeout_ms: int | None = None,
    operation_timeout_ms: int | None = None,
) -> list[str]:
    """:func:`build_invoke_command_script` wrapped in a local PowerShell argv, ready to run."""
    script = build_invoke_command_script(
        host=host,
        script_body=script_body,
        username=username,
        password=password,
        arguments=arguments,
        open_timeout_ms=open_timeout_ms,
        operation_timeout_ms=operation_timeout_ms,
    )
    return [powershell_executable(), "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", script]
