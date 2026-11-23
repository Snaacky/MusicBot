"""Read-only verification of the YouTube session used for playback."""

from __future__ import annotations

import io
import math
import re
from pathlib import Path
from typing import Any

import yt_dlp


class _QuietLogger:
    # Cookie-loading failures can include private browser paths or proxy details.
    def debug(self, _message: str) -> None:
        pass

    def warning(self, _message: str) -> None:
        pass

    def error(self, _message: str) -> None:
        pass


def _cookie_snapshot(path: str) -> io.StringIO:
    contents = Path(path).read_text(encoding="utf-8")
    # yt-dlp prints malformed cookie lines directly to stderr, bypassing its
    # logger. Validate a snapshot first so no cookie data reaches that path.
    for line in contents.splitlines():
        if line.startswith("#HttpOnly_"):
            line = line.removeprefix("#HttpOnly_")
        if not line.strip() or line.startswith("#"):
            continue
        domain, subdomains, _path, secure, expires, _name, _value = line.split("\t")
        if (
            subdomains not in {"TRUE", "FALSE"}
            or secure not in {"TRUE", "FALSE"}
            or (subdomains == "TRUE") != domain.startswith(".")
            or (
                expires
                and (
                    not re.fullmatch(r"[0-9]+(?:\.[0-9]+)?", expires)
                    or not math.isfinite(float(expires))
                )
            )
        ):
            raise ValueError("Invalid cookie file.")
    return io.StringIO(contents)


def check_status(options: dict[str, Any]) -> dict[str, Any]:
    configured = bool(options.get("cookiesfrombrowser") or options.get("cookiefile"))
    result: dict[str, Any] = {
        "configured": configured,
        "logged_in": False,
        "status": "logged_out",
        "message": "YouTube cookies are not configured. Regular videos use guest playback; "
        "age-restricted and account-only videos require a signed-in session.",
    }
    if not configured:
        return result

    result.update(
        logged_in=None,
        status="unavailable",
        message="YouTube login status could not be verified. Check cookies and connectivity.",
    )
    try:
        if options.get("cookiefile"):
            options = options | {"cookiefile": _cookie_snapshot(options["cookiefile"])}
        downloader = yt_dlp.YoutubeDL(
            options
            | {
                "cachedir": False,
                "logger": _QuietLogger(),
                "socket_timeout": 10,
                "retries": 0,
                "extractor_retries": 0,
            }
        )
        try:
            try:
                # Load the same cookies, then prevent close() from saving them.
                # This must also happen when loading fails partway through.
                _ = downloader.cookiejar
            finally:
                downloader.params["cookiefile"] = None
            extractor = downloader.get_info_extractor("Youtube")
            extractor.initialize()
            webpage = extractor._download_webpage(
                "https://www.youtube.com/",
                "login-status",
                note=False,
                headers={"Cache-Control": "no-cache"},
            )
            config = extractor.extract_ytcfg("login-status", webpage)
            logged_in = config.get("LOGGED_IN") if isinstance(config, dict) else None
            # Local auth-cookie presence is insufficient: cookies may be revoked.
            if type(logged_in) is bool:
                result.update(
                    logged_in=logged_in,
                    status="logged_in" if logged_in else "logged_out",
                    message=(
                        "YouTube confirmed the playback session is signed in."
                        if logged_in
                        else "YouTube did not accept the playback cookies as a signed-in session."
                    ),
                )
        finally:
            downloader.close()
    except Exception:
        # Keep cookie values, browser paths and authenticated proxy URLs private.
        result.update(
            logged_in=None,
            status="unavailable",
            message="YouTube login status could not be verified. Check cookies and connectivity.",
        )
    return result
