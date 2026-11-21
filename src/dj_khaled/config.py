"""Configuration without copying or logging credentials."""

from __future__ import annotations

import os
import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import quote

from dotenv import dotenv_values

_VARIABLE = re.compile(r"\$\{([^}:]*)(?::-([^}]*))?\}")
_MAX_EXPANSION = 65536
_MAX_DEPTH = 32
_TRIMMED_SETTINGS = {
    "DISCORD_TOKEN",
    "SPOTIFY_CLIENT_ID",
    "SPOTIFY_CLIENT_SECRET",
    "GENIUS_ACCESS_TOKEN",
    "YOUTUBE_PO_TOKEN",
}


def _normalize(key: str, value: str) -> str:
    if key in _TRIMMED_SETTINGS:
        value = value.strip()
    if key == "DISCORD_TOKEN" and value[:4].casefold() == "bot ":
        value = value[4:].strip()
    return value


def _configuration(root: Path) -> tuple[dict[str, str], dict[str, Path]]:
    """Select by precedence before expanding file references; external values are opaque."""
    candidates: dict[str, list[tuple[str, Path, bool]]] = {}

    def add(key: str, value: str, directory: Path, interpolate: bool) -> None:
        if normalized := _normalize(key, value):
            candidates.setdefault(key, []).append((normalized, directory, interpolate))

    for key, value in os.environ.items():
        add(key, value, root, False)
    # A standalone installation owns its configuration. Never inherit a sibling
    # project's credentials from the parent of the selected application home.
    for name in ("bot-settings.env", ".env.local", ".env"):
        for key, value in dotenv_values(root / name, interpolate=False).items():
            if value is not None:
                add(key, value, root, True)

    resolved: dict[str, str] = {}
    origins: dict[str, Path] = {}

    def expand(value: str, trail: tuple[str, ...]) -> str:
        pieces = []
        offset = size = 0
        for match in _VARIABLE.finditer(value):
            literal = value[offset : match.start()]
            name, default = match.groups()
            if trail and name == trail[-1] and default is not None:
                # Common dotenv templates use KEY=${KEY:-fallback}. With no
                # selected process override, its own fallback is the value.
                replacement = default
            else:
                replacement = (resolve(name, trail) if name in candidates else "") or (
                    default or ""
                )
            size += len(literal) + len(replacement)
            if size > _MAX_EXPANSION:
                raise ValueError("Configuration variable expansion is too large.")
            pieces.extend((literal, replacement))
            offset = match.end()
        tail = value[offset:]
        if size + len(tail) > _MAX_EXPANSION:
            raise ValueError("Configuration variable expansion is too large.")
        return "".join([*pieces, tail])

    def resolve(key: str, trail: tuple[str, ...] = ()) -> str:
        if key in resolved:
            return resolved[key]
        if key in trail:
            raise ValueError(f"Configuration variable {key} has a circular reference.")
        if len(trail) >= _MAX_DEPTH:
            raise ValueError("Configuration variable expansion is too deep.")
        for value, directory, interpolate in candidates[key]:
            result = _normalize(key, expand(value, (*trail, key)) if interpolate else value)
            if result:
                resolved[key] = result
                origins[key] = directory
                return result
        resolved[key] = ""
        return ""

    for key in candidates:
        resolve(key)
    return {key: value for key, value in resolved.items() if value}, origins


def project_directory() -> Path:
    if configured := os.getenv("DJ_KHALED_HOME"):
        return Path(configured).expanduser().resolve()
    source_root = Path(__file__).resolve().parents[2]
    if (source_root / "pyproject.toml").exists():
        return source_root
    return Path.cwd().resolve()


def _integer(values: dict[str, str], key: str, default: int, minimum: int = 0) -> int:
    try:
        value = int(values.get(key) or default)
    except ValueError:
        raise ValueError(f"{key} must be an integer.") from None
    if value < minimum:
        raise ValueError(f"{key} must be at least {minimum}.")
    return value


def _ids(value: str, key: str) -> tuple[int, ...]:
    try:
        identifiers = tuple(
            dict.fromkeys(int(item.strip()) for item in value.split(",") if item.strip())
        )
    except ValueError:
        raise ValueError(f"{key} must contain comma-separated Discord IDs.") from None
    if any(identifier <= 0 or identifier > 2**63 - 1 for identifier in identifiers):
        raise ValueError(f"{key} contains an invalid Discord ID.")
    return identifiers


@dataclass(frozen=True, slots=True)
class Settings:
    root: Path
    app_name: str = "DJ Khaled"
    token: str = field(default="", repr=False)
    client_id: int | None = None
    guild_ids: tuple[int, ...] = ()
    default_volume: int = 100
    audio_buffer_ms: int = 1000
    max_queue: int = 100
    max_playlist: int = 50
    prefetch_tracks: int = 2
    idle_timeout: int = 300
    alone_timeout: int = 120
    cache_max_bytes: int = 1024 * 1024 * 1024
    max_download_bytes: int = 256 * 1024 * 1024
    ffmpeg: str = "ffmpeg"
    deno: str | None = None
    spotify_id: str = field(default="", repr=False)
    spotify_secret: str = field(default="", repr=False)
    spotify_market: str = "GB"
    spotify_redirect_uri: str = "http://127.0.0.1:8888/callback"
    genius_token: str = field(default="", repr=False)
    cookies_file: str | None = field(default=None, repr=False)
    cookies_browser: str | None = None
    po_token: str | None = field(default=None, repr=False)
    proxy: str | None = field(default=None, repr=False)
    dj_roles: tuple[int, ...] = ()
    shard_count: int | None = None
    sync_bot_name: bool = True

    @property
    def data_directory(self) -> Path:
        return self.root / "data"

    @classmethod
    def load(cls, root: Path | None = None) -> Settings:
        root = (root or project_directory()).resolve()
        values, origins = _configuration(root)

        def executable(key: str, name: str, fallback: Path | None = None) -> str | None:
            if value := values.get(key):
                candidate = Path(value).expanduser()
                if candidate.is_absolute():
                    return str(candidate.resolve())
                # Bare names intentionally use PATH; explicit relative paths are
                # anchored to the declaring env file, never the launch directory.
                if candidate.name == value and (found := shutil.which(value)):
                    return str(Path(found).resolve())
                return str((origins[key] / candidate).resolve())
            if found := shutil.which(name):
                return str(Path(found).resolve())
            return str(fallback) if fallback and fallback.exists() else None

        proxy = None
        if values.get("YTDLP_PROXY_HOST") and values.get("YTDLP_PROXY_PORT"):
            auth = ""
            if username := values.get("YTDLP_PROXY_USERNAME"):
                password = values.get("YTDLP_PROXY_PASSWORD", "")
                auth = f"{quote(username, safe='')}:{quote(password, safe='')}@"
            proxy = f"https://{auth}{values['YTDLP_PROXY_HOST']}:{values['YTDLP_PROXY_PORT']}"
        executable_suffix = ".exe" if os.name == "nt" else ""
        deno_fallback = root / f".cache/runtime/deno/deno{executable_suffix}"
        ffmpeg_fallback = root / f"node_modules/ffmpeg-static/ffmpeg{executable_suffix}"
        cookies = values.get("COOKIES_FILE")
        if cookies:
            candidate = Path(cookies).expanduser()
            cookies = str(
                (origins["COOKIES_FILE"] / candidate).resolve()
                if not candidate.is_absolute()
                else candidate.resolve()
            )
        shards = values.get("TOTAL_SHARDS", "").strip().casefold()
        client_ids = _ids(values.get("CLIENT_ID", ""), "CLIENT_ID")
        if len(client_ids) > 1:
            raise ValueError("CLIENT_ID must contain one Discord application ID.")
        return cls(
            root=root,
            app_name=values.get("APP_NAME", "DJ Khaled"),
            token=values.get("DISCORD_TOKEN", ""),
            client_id=client_ids[0] if client_ids else None,
            guild_ids=_ids(values.get("GUILD_ID", ""), "GUILD_ID"),
            default_volume=min(100, _integer(values, "DEFAULT_VOLUME", 100)),
            audio_buffer_ms=min(5000, _integer(values, "AUDIO_BUFFER_MS", 1000, 100)),
            max_queue=_integer(values, "MAX_QUEUE_SIZE", 100, 1),
            max_playlist=_integer(values, "MAX_PLAYLIST_SIZE", 50, 1),
            prefetch_tracks=_integer(values, "PREFETCH_TRACKS", 2),
            idle_timeout=_integer(values, "IDLE_TIMEOUT", 300, 1),
            alone_timeout=_integer(values, "ALONE_TIMEOUT", 120, 1),
            cache_max_bytes=_integer(values, "CACHE_MAX_MB", 1024, 1) * 1024 * 1024,
            max_download_bytes=_integer(values, "MAX_DOWNLOAD_MB", 256, 1) * 1024 * 1024,
            ffmpeg=executable("FFMPEG_PATH", "ffmpeg", ffmpeg_fallback) or "ffmpeg",
            deno=executable("DENO_PATH", "deno", deno_fallback),
            spotify_id=values.get("SPOTIFY_CLIENT_ID", ""),
            spotify_secret=values.get("SPOTIFY_CLIENT_SECRET", ""),
            spotify_market=values.get("SPOTIFY_MARKET", "GB"),
            spotify_redirect_uri=values.get(
                "SPOTIFY_REDIRECT_URI", "http://127.0.0.1:8888/callback"
            ),
            genius_token=values.get("GENIUS_ACCESS_TOKEN", ""),
            cookies_file=cookies,
            cookies_browser=values.get("COOKIES_FROM_BROWSER"),
            po_token=values.get("YOUTUBE_PO_TOKEN"),
            proxy=proxy,
            dj_roles=_ids(values.get("DJ_ROLE_IDS", ""), "DJ_ROLE_IDS"),
            shard_count=(
                _integer(values, "TOTAL_SHARDS", 1, 1) if shards and shards != "auto" else None
            ),
            sync_bot_name=values.get("SYNC_BOT_NAME", "true").casefold() == "true",
        )
