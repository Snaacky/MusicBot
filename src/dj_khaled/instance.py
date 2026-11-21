"""Local process coordination without trusting PID files as process identity."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import BinaryIO

from .config import Settings
from .models import UserError


class InstanceLock:
    """Hold an OS lock for the full live session; never unlink its inode."""

    def __init__(self, directory: Path) -> None:
        self.path = directory / ".dj-khaled.lock"
        self._file: BinaryIO | None = None

    def __enter__(self) -> InstanceLock:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            stream = self.path.open("a+b")
        except OSError:
            raise UserError(
                "Cannot open DJ Khaled's data-directory lock. Check permissions."
            ) from None
        try:
            if os.name == "nt":
                import msvcrt

                if stream.seek(0, os.SEEK_END) == 0:
                    stream.write(b"\0")
                    stream.flush()
                stream.seek(0)
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            stream.close()
            raise UserError(
                "DJ Khaled cannot acquire its data-directory lock. Another instance may "
                "already be running; close it before starting this one."
            ) from None
        self._file = stream
        return self

    def __exit__(self, *_error: object) -> None:
        if stream := self._file:
            self._file = None
            try:
                if os.name == "nt":
                    import msvcrt

                    stream.seek(0)
                    msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
            finally:
                stream.close()


def _matches_entrypoint(
    executable: str, arguments: list[str], repository: Path, cwd: Path | None = None
) -> bool:
    if Path(executable).name.casefold() not in {"node", "node.exe"} or len(arguments) < 2:
        return False
    # Positively identify Node's entry script, never a substring in another
    # application's argument or eval expression. Unknown options fail closed.
    remaining = arguments[1:]
    if remaining and remaining[0] == "--":
        remaining = remaining[1:]
    if not remaining or remaining[0].startswith("-"):
        return False
    script = Path(remaining[0])
    if not script.is_absolute():
        if cwd is None:
            return False  # Windows cannot establish cwd from CIM; do not guess.
        script = cwd / script
    return script.resolve() in {
        (repository / "index.js").resolve(),
        (repository / "shard.js").resolve(),
    }


def _windows_legacy_command(pid: int) -> tuple[str, list[str]] | None:
    """Read the actual command line; executable location alone proves nothing."""
    import ctypes
    from ctypes import wintypes

    command = (
        "[Console]::OutputEncoding = [System.Text.Encoding]::UTF8; "
        f"Get-CimInstance Win32_Process -Filter 'ProcessId = {pid}' -ErrorAction Stop | "
        "Select-Object ExecutablePath, CommandLine | ConvertTo-Json -Compress"
    )
    result = subprocess.run(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", command],
        capture_output=True,
        timeout=5,
        check=False,
        creationflags=subprocess.CREATE_NO_WINDOW,
    )
    if result.returncode:
        return None
    record = json.loads(result.stdout.decode("utf-8-sig"))
    if not isinstance(record, dict) or not record.get("CommandLine"):
        return None
    shell = ctypes.WinDLL("shell32", use_last_error=True)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    shell.CommandLineToArgvW.argtypes = (wintypes.LPCWSTR, ctypes.POINTER(ctypes.c_int))
    shell.CommandLineToArgvW.restype = ctypes.POINTER(wintypes.LPWSTR)
    kernel.LocalFree.argtypes = (ctypes.c_void_p,)
    kernel.LocalFree.restype = ctypes.c_void_p
    count = ctypes.c_int()
    arguments = shell.CommandLineToArgvW(record["CommandLine"], ctypes.byref(count))
    if not arguments:
        return None
    try:
        return str(record.get("ExecutablePath") or ""), [
            arguments[index] for index in range(count.value)
        ]
    finally:
        kernel.LocalFree(arguments)


def legacy_process(settings: Settings) -> int | None:
    """Return only a positively identified legacy entry point; never signal it."""
    try:
        pid = int((settings.root / ".cache/musicbot.pid").read_text().strip())
        if pid <= 0 or pid > 2**32 - 1 or pid == os.getpid():
            return None
        if sys.platform == "win32":
            details = _windows_legacy_command(pid)
            if details and _matches_entrypoint(*details, settings.root):
                return pid
        else:
            process = Path(f"/proc/{pid}")
            arguments = [
                os.fsdecode(item)
                for item in (process / "cmdline").read_bytes().split(b"\0")
                if item
            ]
            executable = str((process / "exe").resolve(strict=True))
            cwd = (process / "cwd").resolve(strict=True)
            if _matches_entrypoint(executable, arguments, settings.root, cwd):
                return pid
    except OSError, ValueError, TypeError, subprocess.SubprocessError:
        pass
    return None
