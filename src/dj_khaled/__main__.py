from __future__ import annotations

import argparse
import asyncio
import importlib.metadata
import json
import logging
import os
import shutil
import signal
import sqlite3
import subprocess
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from logging.handlers import RotatingFileHandler
from pathlib import Path

import aiohttp
import discord

from . import __version__
from .bot import DJKhaled
from .config import Settings
from .instance import InstanceLock, legacy_process
from .media import MediaResolver, PublicResolver
from .models import UserError
from .spotify_auth import login as spotify_login


class RedactingFormatter(logging.Formatter):
    def __init__(self, settings: Settings) -> None:
        super().__init__("%(asctime)s %(levelname)-7s %(name)s: %(message)s")
        self.secrets = tuple(
            value
            for value in (
                settings.token,
                settings.spotify_secret,
                settings.genius_token,
                settings.po_token,
                settings.proxy,
            )
            if value
        )

    def format(self, record: logging.LogRecord) -> str:
        result = super().format(record)
        for value in self.secrets:
            result = result.replace(value, "[redacted]")
        return result


class _LiveFileHandler(RotatingFileHandler):
    def handleError(self, _record: logging.LogRecord) -> None:
        # Default logging error diagnostics print raw record arguments, which can
        # contain credentials. Disable a failed file sink and retain the console.
        logging.getLogger().removeHandler(self)
        try:
            self.close()
        except OSError:
            pass
        logging.getLogger(__name__).warning("File logging failed; continuing with console logs.")


@contextmanager
def live_log(settings: Settings) -> Iterator[None]:
    """Called only while the live instance lock is held."""
    handler = None
    try:
        directory = settings.data_directory / "logs"
        directory.mkdir(parents=True, exist_ok=True)
        handler = _LiveFileHandler(
            directory / "dj-khaled.log",
            maxBytes=5 * 1024 * 1024,
            backupCount=3,
            encoding="utf-8",
        )
        handler.setFormatter(RedactingFormatter(settings))
        logging.getLogger().addHandler(handler)
    except OSError:
        logging.getLogger(__name__).warning(
            "File logging is unavailable; continuing with console logs."
        )
    try:
        yield
    finally:
        if handler is not None:
            logging.getLogger().removeHandler(handler)
            try:
                handler.close()
            except OSError:
                logging.getLogger(__name__).warning("The file log could not be closed cleanly.")


def _tool_output(executable: str, *arguments: str) -> str:
    try:
        result = subprocess.run(
            [executable, *arguments],
            capture_output=True,
            timeout=5,
            check=False,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
    except OSError, subprocess.SubprocessError:
        raise UserError("A configured audio runtime could not execute. Check its path.") from None
    if result.returncode:
        raise UserError("A configured audio runtime failed its local dependency check.")
    return (result.stdout + result.stderr).decode("utf-8", errors="replace")


def validate(settings: Settings, *, require_token: bool) -> dict[str, str]:
    if sys.version_info[:2] != (3, 14):
        raise UserError("DJ Khaled requires Python 3.14. Use uv run dj-khaled.")
    if require_token and (not settings.token or settings.token.startswith("YOUR_")):
        raise UserError("Set DISCORD_TOKEN in the ignored .env.local file before starting.")
    if settings.shard_count is not None and settings.shard_count < 1:
        raise UserError("TOTAL_SHARDS must be a positive integer or auto.")
    if not shutil.which(settings.ffmpeg) and not Path(settings.ffmpeg).is_file():
        raise UserError("FFmpeg was not found. Install it on PATH or set FFMPEG_PATH.")
    if not settings.deno or (not shutil.which(settings.deno) and not Path(settings.deno).is_file()):
        raise UserError("Deno was not found. Install it on PATH or set DENO_PATH for YouTube.")
    ffmpeg_version = _tool_output(settings.ffmpeg, "-version")
    if not ffmpeg_version.casefold().startswith("ffmpeg version"):
        raise UserError("FFMPEG_PATH does not identify FFmpeg.")
    if "libopus" not in _tool_output(settings.ffmpeg, "-hide_banner", "-encoders"):
        raise UserError("FFmpeg needs the libopus encoder for audio caching.")
    deno_version = _tool_output(settings.deno, "--version")
    if not deno_version.casefold().startswith("deno "):
        raise UserError("DENO_PATH does not identify Deno.")
    try:
        davey_version = importlib.metadata.version("davey")
        yt_dlp_version = importlib.metadata.version("yt-dlp")
    except importlib.metadata.PackageNotFoundError:
        raise UserError("Voice/media dependencies are missing. Run uv sync --frozen.") from None
    if not discord.voice_client.has_nacl or not discord.voice_client.has_dave:
        raise UserError("Discord voice dependencies are missing. Run uv sync --frozen.")
    # Keep this compatibility check aligned with the package's DAVE API cap.
    if not (0, 1, 7) <= tuple(int(part) for part in davey_version.split(".")[:3]) < (0, 2):
        raise UserError(
            "This build requires davey 0.1.7 or newer, below 0.2. Run uv sync --frozen."
        )
    try:
        encoder = discord.opus.Encoder()
        encoder.encode(bytes(3840), 960)
        opus_version = encoder.get_opus_version()
    except Exception:
        raise UserError(
            "Opus encoding is unavailable. Install libopus for Discord voice."
        ) from None
    if (
        settings.cookies_file
        and not settings.po_token
        and not settings.cookies_browser
        and not Path(settings.cookies_file).is_file()
    ):
        raise UserError("COOKIES_FILE does not identify an existing file.")
    return {
        "ffmpeg_version": ffmpeg_version.splitlines()[0],
        "deno_version": deno_version.splitlines()[0],
        "davey": davey_version,
        "yt_dlp": yt_dlp_version,
        "opus": opus_version,
    }


async def probe(settings: Settings, query: str) -> None:
    async with aiohttp.ClientSession(
        connector=aiohttp.TCPConnector(resolver=PublicResolver()),
        timeout=aiohttp.ClientTimeout(total=60),
    ) as session:
        resolver = MediaResolver(settings, session, read_only=True)
        try:
            tracks = await resolver.resolve(query)
            print(json.dumps({"tracks": [track.to_dict() for track in tracks]}, ensure_ascii=False))
        finally:
            await resolver.close()


async def run(settings: Settings) -> None:
    loop = asyncio.get_running_loop()
    task = asyncio.current_task()
    terminate_handler = False
    if sys.platform != "win32" and task is not None:
        loop.add_signal_handler(signal.SIGTERM, task.cancel)
        terminate_handler = True
    try:
        async with DJKhaled(settings) as bot:
            await bot.start(settings.token)
    finally:
        if terminate_handler:
            loop.remove_signal_handler(signal.SIGTERM)


def main() -> None:
    parser = argparse.ArgumentParser(prog="dj-khaled", description="DJ Khaled Discord music bot")
    diagnostics = parser.add_mutually_exclusive_group()
    diagnostics.add_argument(
        "--check", action="store_true", help="Check dependencies without connecting"
    )
    diagnostics.add_argument(
        "--probe", metavar="QUERY", help="Resolve media through yt-dlp without Discord"
    )
    diagnostics.add_argument(
        "--spotify-login", action="store_true", help="Connect Spotify for playlist access"
    )
    parser.add_argument(
        "--replace-javascript",
        action="store_true",
        help="Retired: stop the legacy bot manually before starting Python",
    )
    arguments = parser.parse_args()
    try:
        settings = Settings.load()
        handler = logging.StreamHandler()
        handler.setFormatter(RedactingFormatter(settings))
        logging.basicConfig(level=logging.INFO, handlers=[handler], force=True)
        # This logger reports UDP send failures; HTTP and gateway payloads stay private.
        logging.getLogger("discord.voice_client").setLevel(logging.DEBUG)
        if arguments.spotify_login:
            asyncio.run(spotify_login(settings))
            return
        dependencies = validate(settings, require_token=not (arguments.check or arguments.probe))
        if arguments.check:
            print(
                json.dumps(
                    {
                        "app": settings.app_name,
                        "version": __version__,
                        "python": sys.version.split()[0],
                        "discord_py": discord.__version__,
                        **dependencies,
                        "ffmpeg": settings.ffmpeg,
                        "deno": settings.deno,
                        "audio_buffer_ms": settings.audio_buffer_ms,
                        "token_configured": bool(settings.token)
                        and not settings.token.startswith("YOUR_"),
                        "spotify_mode": (
                            "api"
                            if settings.spotify_id and settings.spotify_secret
                            else "public_tracks"
                        ),
                        "spotify_client_id_configured": bool(settings.spotify_id),
                        "spotify_client_secret_configured": bool(settings.spotify_secret),
                        "spotify_user_login_saved": (
                            settings.data_directory / "spotify-auth.json"
                        ).is_file(),
                        "guild_ids": settings.guild_ids,
                        "javascript_bot_running": legacy_process(settings) is not None,
                    },
                    indent=2,
                )
            )
            return
        if arguments.probe:
            asyncio.run(probe(settings, arguments.probe))
            return
        if arguments.replace_javascript:
            raise UserError(
                "Automatic JavaScript termination has been retired. Stop that bot manually, "
                "then run uv run dj-khaled without --replace-javascript."
            )
        with InstanceLock(settings.data_directory):
            if legacy_process(settings) is not None:
                raise UserError(
                    "The local JavaScript bot is still running. Stop it manually before "
                    "starting DJ Khaled."
                )
            with live_log(settings):
                logging.getLogger(__name__).info(
                    "Starting %s %s with Python %s, discord.py %s, davey %s",
                    settings.app_name,
                    __version__,
                    sys.version.split()[0],
                    discord.__version__,
                    importlib.metadata.version("davey"),
                )
                asyncio.run(run(settings))
    except (UserError, ValueError) as error:
        print(f"DJ Khaled: {error}", file=sys.stderr)
        raise SystemExit(1) from None
    except sqlite3.Error:
        logging.getLogger(__name__).exception("Playback database could not be accessed")
        print(
            "DJ Khaled: Cannot open or read the playback database. Keep a backup of "
            "data/state.sqlite3 before repairing it, and check directory/file permissions. "
            "Existing state has not been reset.",
            file=sys.stderr,
        )
        raise SystemExit(1) from None
    except discord.LoginFailure:
        print(
            "Discord rejected the bot token. Use the token from Developer Portal > Bot.",
            file=sys.stderr,
        )
        raise SystemExit(1) from None
    except KeyboardInterrupt, asyncio.CancelledError:
        pass


if __name__ == "__main__":
    main()
