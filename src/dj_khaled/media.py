"""Provider lookups and downloads through yt-dlp's Python API."""

from __future__ import annotations

import asyncio
import base64
import copy
import ipaddress
import json
import logging
import math
import re
import shutil
import socket
import tempfile
import threading
from collections import OrderedDict
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import replace
from difflib import SequenceMatcher
from pathlib import Path
from time import monotonic
from typing import Any
from urllib.parse import ParseResult, parse_qs, urlencode, urljoin, urlparse, urlunparse

import aiohttp
import yt_dlp
from aiohttp.resolver import DefaultResolver
from bs4 import BeautifulSoup, SoupStrainer

from .config import Settings
from .models import MediaError, Track, finite_seconds
from .spotify_auth import SpotifyUserAuth
from .tidal import TidalService
from .youtube_auth import check_status as check_youtube_status

log = logging.getLogger(__name__)
RESOLUTION_CACHE_SECONDS = 120.0
RESOLUTION_CACHE_ENTRIES = 128
EXTRACTION_CACHE_SECONDS = 60.0
EXTRACTION_CACHE_ENTRIES = 16
_sanitize_download_info = yt_dlp.YoutubeDL.sanitize_info
AUDIO_EXTENSIONS = {".mp3", ".wav", ".ogg", ".flac", ".m4a", ".aac", ".opus", ".webm", ".mp4"}
MUSIC_BLOCKLIST = (
    "tutorial",
    "lesson",
    "podcast",
    "interview",
    "speech",
    "lecture",
    "review",
    "unboxing",
    "reaction",
    "gameplay",
    "full movie",
    "full album",
    "how to",
    "vlog",
    "audiobook",
    "compilation",
)


class _SpotifyNotFound(MediaError):
    def __init__(self) -> None:
        super().__init__("Spotify could not find that track or collection.")


class _YouTubeAgeRestricted(Exception):
    """An explicit age gate needs account credentials or account verification."""


class _YouTubeCookieRetryFailed(Exception):
    """Configured account cookies could not complete a YouTube bot check."""


class _YouTubeCookieRequired(Exception):
    """Cached metadata was obtained using an authenticated YouTube session."""


class _SpotifyUnauthorized(Exception):
    def __init__(self, *, user_required: bool = False) -> None:
        self.user_required = user_required


class _SpotifyAccessDenied(MediaError):
    def __init__(self) -> None:
        super().__init__(
            "Spotify denied this app access. Some artist and playlist features "
            "are unavailable to development apps or need user access. "
            "Try a single song link or /search instead."
        )


SPOTIFY_PLAYLIST_LOGIN = (
    "Spotify needs a user sign-in to read playlist songs. "
    "Run `dj-khaled --spotify-login` on the bot computer. "
    "The account must own or collaborate on that playlist."
)


def _parse_url(value: str) -> ParseResult:
    try:
        return urlparse(value)
    except ValueError as error:
        raise MediaError("Use a valid public HTTP or HTTPS music link.") from error


def public_http_url(value: str) -> str:
    parsed = _parse_url(value)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username:
        raise MediaError("Use a public HTTP or HTTPS music link.")
    try:
        address = ipaddress.ip_address(parsed.hostname)
    except ValueError:
        address = None
    if address is not None and not address.is_global:
        raise MediaError("Links to private or local network addresses are not supported.")
    if parsed.hostname.casefold() == "localhost":
        raise MediaError("Links to localhost are not supported.")
    return value


class PublicResolver(DefaultResolver):
    async def resolve(self, host: str, port: int = 0, family: int = socket.AF_INET):
        results = await super().resolve(host, port, family)
        if not results or any(not ipaddress.ip_address(row["host"]).is_global for row in results):
            raise OSError("The destination resolves to a non-public network address.")
        return results


def platform_for(query: str) -> str:
    if query.casefold().startswith("spotify:"):
        return "spotify"
    parsed = _parse_url(query)
    if not parsed.scheme or (
        "://" not in query and parsed.scheme not in {"file", "data", "javascript", "ftp"}
    ):
        return "youtube"
    public_http_url(query)
    host = (parsed.hostname or "").casefold()
    if host == "youtu.be" or host == "youtube.com" or host.endswith(".youtube.com"):
        return "youtube"
    if host in {"open.spotify.com", "spotify.link"}:
        return "spotify"
    if host in {"tidal.com", "www.tidal.com", "listen.tidal.com"}:
        return "tidal"
    if host == "soundcloud.com" or host.endswith(".soundcloud.com"):
        return "soundcloud"
    if host == "soundcloud.app.goo.gl":
        return "soundcloud"
    if Path(parsed.path).suffix.casefold() in AUDIO_EXTENSIONS:
        return "direct"
    raise MediaError(
        "That site is not supported. Use YouTube, Spotify, TIDAL, SoundCloud, or an audio file."
    )


def start_offset(url: str) -> float:
    parsed = _parse_url(url)
    query = parse_qs(parsed.query) | parse_qs(parsed.fragment)
    for name in ("t", "start", "time_continue"):
        value = query.get(name, [""])[0]
        if value.isdigit():
            return finite_seconds(value)
        if re.fullmatch(r"\d+(?::\d+)+", value):
            return finite_seconds(
                sum(int(part) * 60**i for i, part in enumerate(reversed(value.split(":"))))
            )
        if re.fullmatch(r"(?:\d+h)?(?:\d+m)?(?:\d+s)?", value) and value:
            return finite_seconds(
                sum(
                    int(n) * {"h": 3600, "m": 60, "s": 1}[u]
                    for n, u in re.findall(r"(\d+)([hms])", value)
                )
            )
    return 0


def normalize_youtube(query: str, allow_playlist: bool) -> str:
    parsed = _parse_url(query)
    if parsed.scheme not in {"http", "https"}:
        return query
    if platform_for(query) != "youtube":
        return query
    params = parse_qs(parsed.query, keep_blank_values=True)
    params.pop("pp", None)
    if not allow_playlist:
        if parsed.path.rstrip("/") == "/playlist":
            raise MediaError("Set playlist:true to play a YouTube playlist link.")
        for name in ("list", "index", "start_radio"):
            params.pop(name, None)
    return urlunparse(parsed._replace(query=urlencode(params, doseq=True)))


def _youtube_video_id(url: str) -> str | None:
    """Recognize only direct public video links, never redirects or collections."""
    try:
        parsed = _parse_url(public_http_url(url))
        if parsed.port not in {None, 80 if parsed.scheme == "http" else 443}:
            return None
        if parsed.hostname == "youtu.be":
            identity = parsed.path.strip("/")
        elif parsed.hostname in {
            "youtube.com",
            "www.youtube.com",
            "m.youtube.com",
            "music.youtube.com",
        }:
            parts = parsed.path.strip("/").split("/")
            if parts == ["watch"]:
                identity = parse_qs(parsed.query).get("v", [""])[0]
            elif len(parts) == 2 and parts[0] in {"shorts", "live", "embed"}:
                identity = parts[1]
            else:
                return None
        else:
            return None
        return identity if re.fullmatch(r"[A-Za-z0-9_-]{11}", identity) else None
    except MediaError, ValueError:
        return None


def is_music(track: Track) -> bool:
    return 30 <= track.duration <= 600 and not any(
        word in track.title.casefold() for word in MUSIC_BLOCKLIST
    )


class MediaResolver:
    def __init__(
        self,
        settings: Settings,
        session: aiohttp.ClientSession,
        *,
        read_only: bool = False,
        on_youtube_status: Callable[[], None] | None = None,
    ) -> None:
        self.settings = settings
        self.http = session
        self._read_only = read_only
        self._workers = asyncio.Semaphore(2)
        self._native_tasks: dict[asyncio.Task[Any], asyncio.Event] = {}
        self._resolution_cache: OrderedDict[tuple[str, bool], tuple[float, tuple[Track, ...]]] = (
            OrderedDict()
        )
        self._extraction_cache: OrderedDict[str, tuple[float, dict[str, Any]]] = OrderedDict()
        self._extraction_lock = threading.Lock()
        self._metadata_cache: OrderedDict[tuple[str, int, int, int], tuple[float, float]] = (
            OrderedDict()
        )
        self._metadata_locks: dict[tuple[str, int, int, int], tuple[asyncio.Lock, int]] = {}
        self._closed = False
        self._spotify_lock = asyncio.Lock()
        self._spotify_token = ""
        self._spotify_expiry = 0.0
        self.spotify_user = SpotifyUserAuth(settings, session)
        self.tidal = TidalService(settings)
        self._spotify_public_workers = asyncio.Semaphore(4)
        self._youtube_status_task: asyncio.Task[dict[str, Any]] | None = None
        self._youtube_status_cache: tuple[float, dict[str, Any]] | None = None
        self._on_youtube_status = on_youtube_status

    @property
    def youtube_auth_configured(self) -> bool:
        return bool(self.settings.cookies_browser or self.settings.cookies_file)

    def _start_youtube_status_check(self) -> asyncio.Task[dict[str, Any]]:
        if self._youtube_status_task is not None:
            return self._youtube_status_task

        async def check() -> dict[str, Any]:
            result = await self._run_native(
                lambda: check_youtube_status(self.options(use_cookies=True))
            )
            self._youtube_status_cache = (monotonic() + 30, result)
            if self._on_youtube_status is not None:
                self._on_youtube_status()
            return result

        task = asyncio.create_task(check(), name="youtube-login-status")
        self._youtube_status_task = task

        def finished(completed: asyncio.Task[dict[str, Any]]) -> None:
            self._youtube_status_task = None
            if not completed.cancelled():
                completed.exception()

        task.add_done_callback(finished)
        return task

    def youtube_status_label(self) -> str:
        """Render cached sign-in state and start a shared check without waiting."""
        if not self.youtube_auth_configured:
            return "Guest - account cookies are not configured"
        if self._closed:
            return "Could not verify sign-in"
        cached = self._youtube_status_cache
        if cached is None or monotonic() >= cached[0]:
            self._start_youtube_status_check()
            return "Checking..."
        logged_in = cached[1].get("logged_in")
        if logged_in is True:
            return "Signed in"
        if logged_in is False:
            return "Signed out - ordinary tracks still play as guests"
        return "Could not verify sign-in"

    @staticmethod
    def _youtube_age_failure(error: Exception) -> bool:
        if isinstance(error, _YouTubeAgeRestricted):
            return True
        message = str(error).casefold()
        return any(
            phrase in message
            for phrase in (
                "confirm your age",
                "verify your age",
                "age-restricted",
                "age restricted",
                "age_verification_required",
                "age_check_required",
                "account age-verification",
            )
        )

    @staticmethod
    def _youtube_bot_failure(error: Exception) -> bool:
        if isinstance(error, _YouTubeCookieRequired):
            return True
        return any(
            phrase in str(error).casefold()
            for phrase in ("not a bot", "unusual traffic", "automated queries")
        )

    def _with_youtube_cookies[T](
        self, operation: Callable[[bool, bool], T], *, youtube: bool, allow_age_retry: bool = True
    ) -> T:
        """Try guests first, then configured cookies for a bot check or age gate."""
        try:
            return operation(False, False)
        except yt_dlp.utils.DownloadCancelled:
            raise
        except (
            yt_dlp.utils.DownloadError,
            yt_dlp.utils.ExtractorError,
            yt_dlp.utils.ReExtractInfo,
            _YouTubeAgeRestricted,
            _YouTubeCookieRequired,
        ) as error:
            age_restricted = self._youtube_age_failure(error)
            if not youtube or not (age_restricted or self._youtube_bot_failure(error)):
                raise
            if age_restricted and not allow_age_retry:
                raise _YouTubeAgeRestricted from error
            if not self.youtube_auth_configured:
                if age_restricted:
                    raise _YouTubeAgeRestricted from error
                raise
            failure = _YouTubeAgeRestricted if age_restricted else _YouTubeCookieRetryFailed
            if not self.settings.cookies_browser and self.settings.cookies_file:
                try:
                    with Path(self.settings.cookies_file).open("rb"):
                        pass
                except OSError, ValueError:
                    raise failure from error
            try:
                return operation(True, age_restricted)
            except yt_dlp.utils.DownloadCancelled:
                raise
            except (yt_dlp.utils.YoutubeDLError, OSError, ValueError) as auth_error:
                if self._youtube_age_failure(auth_error):
                    raise _YouTubeAgeRestricted from auth_error
                raise failure from auth_error

    @staticmethod
    def _youtube_auth_failure(error: Exception) -> bool:
        return any(
            phrase in str(error).casefold()
            for phrase in (
                "sign in",
                "sign-in",
                "log in",
                "login required",
                "not a bot",
                "confirm your age",
                "age-restricted",
                "age restricted",
            )
        )

    def _youtube_auth_error(self) -> MediaError:
        label = self.youtube_status_label()
        return MediaError(
            "This video is age-restricted (18+) and requires a signed-in, age-verified "
            f"YouTube account. YouTube sign-in: {label}."
        )

    def _youtube_cookie_error(self) -> MediaError:
        return MediaError(
            "YouTube's bot check could not be completed with the configured cookies. "
            f"YouTube sign-in: {self.youtube_status_label()}. Check the cookies and try again."
        )

    async def youtube_status(self) -> dict[str, Any]:
        if self._closed:
            raise MediaError("The media service is shutting down.")
        cached = self._youtube_status_cache
        if cached is not None and monotonic() < cached[0]:
            return dict(cached[1])
        # A timed-out HTTP caller must not spawn another cookie reader while the
        # original native check is still running.
        return dict(await asyncio.shield(self._start_youtube_status_check()))

    async def _run_native[T](self, operation: Callable[[], T]) -> T:
        if self._closed:
            raise MediaError("The media service is shutting down.")
        started = asyncio.Event()

        async def work() -> T:
            async with self._workers:
                started.set()
                return await asyncio.to_thread(operation)

        task = asyncio.create_task(work())
        self._native_tasks[task] = started

        def finished(completed: asyncio.Task[T]) -> None:
            self._native_tasks.pop(completed, None)
            if not completed.cancelled():
                completed.exception()

        task.add_done_callback(finished)
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            # Threads cannot be cancelled: keep their permit until they actually exit.
            # Work which has not acquired a permit can still be discarded immediately.
            if not started.is_set():
                task.cancel()
            raise

    async def close(self) -> None:
        self._closed = True
        if self._youtube_status_task is not None:
            self._youtube_status_task.cancel()
            await asyncio.gather(self._youtube_status_task, return_exceptions=True)
        tasks = list(self._native_tasks)
        for task in tasks:
            if not self._native_tasks[task].is_set():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self.tidal.close()
        self._resolution_cache.clear()
        with self._extraction_lock:
            self._extraction_cache.clear()

    def _remember_extraction(
        self, query: str, info: dict[str, Any], *, requires_cookies: bool = False
    ) -> None:
        identity = _youtube_video_id(query)
        formats = info.get("formats")
        live_status = info.get("live_status")
        if (
            identity is None
            or info.get("id") != identity
            or info.get("_type", "video") != "video"
            or str(info.get("extractor_key", "")).casefold() != "youtube"
            or _youtube_video_id(str(info.get("webpage_url", ""))) != identity
            or info.get("is_live")
            or (live_status is not None and not isinstance(live_status, str))
            or live_status in ("is_live", "is_upcoming", "post_live")
            or info.get("has_drm")
            or info.get("additional_urls")
            or info.get("entries")
            or info.get("section_start") is not None
            or info.get("section_end") is not None
            or not isinstance(formats, list)
            or not formats
        ):
            return
        try:
            for selected in formats:
                if not isinstance(selected, dict) or not isinstance(selected.get("url"), str):
                    return
                public_http_url(selected["url"])
            # This is the same portable representation yt-dlp accepts for an
            # info-file download. Drop old filename/selection/private state.
            portable = _sanitize_download_info(info, remove_private_keys=True)
            if requires_cookies:
                portable["_dj_khaled_requires_cookies"] = True
            if len(json.dumps(portable).encode()) > 2 * 1024 * 1024:
                return
        except MediaError, TypeError, ValueError, OverflowError, RecursionError:
            return
        with self._extraction_lock:
            if self._closed:
                return
            now = monotonic()
            for key, (expiry, _) in list(self._extraction_cache.items()):
                if expiry <= now:
                    self._extraction_cache.pop(key)
            self._extraction_cache[identity] = (now + EXTRACTION_CACHE_SECONDS, portable)
            self._extraction_cache.move_to_end(identity)
            while len(self._extraction_cache) > EXTRACTION_CACHE_ENTRIES:
                self._extraction_cache.popitem(last=False)

    def _cached_extraction(self, query: str) -> tuple[str, dict[str, Any]] | None:
        identity = _youtube_video_id(query)
        if identity is None:
            return None
        with self._extraction_lock:
            cached = self._extraction_cache.get(identity)
            if cached is None:
                return None
            expiry, info = cached
            if expiry <= monotonic():
                self._extraction_cache.pop(identity)
                return None
            self._extraction_cache.move_to_end(identity)
            return identity, copy.deepcopy(info)

    def options(self, *, use_cookies: bool = False, **extra: Any) -> dict[str, Any]:
        options: dict[str, Any] = {
            "quiet": True,
            "noprogress": True,
            "no_warnings": True,
            "noplaylist": True,
            "socket_timeout": 15,
            "retries": 2,
            "fragment_retries": 2,
            "extractor_retries": 1,
            "ffmpeg_location": self.settings.ffmpeg,
            "cachedir": False
            if self._read_only
            else str(self.settings.data_directory / "extractor-cache"),
        }
        if self.settings.deno:
            options["js_runtimes"] = {"deno": {"path": self.settings.deno}}
        if self.settings.po_token:
            options["extractor_args"] = {
                "youtube": {"po_token": [f"web+{self.settings.po_token}"], "player_client": ["web"]}
            }
        if use_cookies and self.settings.cookies_browser:
            browser, _, profile = self.settings.cookies_browser.partition(":")
            options["cookiesfrombrowser"] = (browser, profile or None, None, None)
        elif use_cookies and self.settings.cookies_file:
            options["cookiefile"] = self.settings.cookies_file
        if self.settings.proxy:
            options["proxy"] = self.settings.proxy
        return options | extra

    @contextmanager
    def _downloader(self, *, use_cookies: bool, **extra: Any) -> Iterator[yt_dlp.YoutubeDL]:
        options = self.options(use_cookies=use_cookies, **extra)
        with yt_dlp.YoutubeDL(options) as downloader:
            if options.get("cookiefile"):
                try:
                    # Keep exported cookies readable on read-only Docker mounts.
                    _ = downloader.cookiejar
                finally:
                    downloader.params["cookiefile"] = None
            yield downloader

    async def extract(self, query: str, **options: Any) -> dict[str, Any]:
        youtube = platform_for(query) == "youtube"

        def retrieve(use_cookies: bool, age_restricted: bool) -> dict[str, Any]:
            with self._downloader(use_cookies=use_cookies, **options) as downloader:
                info = downloader.extract_info(query, download=False) or {}
                if age_restricted:
                    info["age_limit"] = max(18, finite_seconds(info.get("age_limit")))
                self._remember_extraction(query, info, requires_cookies=use_cookies)
                return info

        try:
            return await self._run_native(
                lambda: self._with_youtube_cookies(
                    retrieve,
                    youtube=youtube,
                    allow_age_retry=(
                        _youtube_video_id(query) is not None
                        and options.get("noplaylist", True) is True
                    ),
                )
            )
        except _YouTubeAgeRestricted as error:
            raise self._youtube_auth_error() from error
        except _YouTubeCookieRetryFailed as error:
            raise self._youtube_cookie_error() from error
        except (yt_dlp.utils.DownloadError, yt_dlp.utils.ExtractorError) as error:
            if youtube and self._youtube_auth_failure(error):
                raise MediaError(
                    "YouTube blocked guest access to this video. "
                    "Try again later or choose another track."
                ) from error
            raise MediaError(
                "The music provider could not load that track. Try another link."
            ) from error

    @staticmethod
    def track(info: dict[str, Any], platform: str, fallback: str = "") -> Track:
        url = info.get("webpage_url") or info.get("original_url") or info.get("url") or fallback
        if platform == "youtube" and info.get("id") and not str(url).startswith("http"):
            url = "https://www.youtube.com/watch?v=" + info["id"]
        return Track(
            title=info.get("title") or "Unknown track",
            artist=info.get("artist") or info.get("uploader") or info.get("channel") or "",
            url=str(url),
            duration=finite_seconds(info.get("duration")),
            age_restricted=finite_seconds(info.get("age_limit")) >= 18,
            thumbnail=info.get("thumbnail"),
            platform=platform,
        )

    async def search(self, query: str, limit: int = 9) -> list[Track]:
        info = await self.extract(f"ytsearch{min(limit, 20)}:{query}", extract_flat="in_playlist")
        return [self.track(entry, "youtube") for entry in info.get("entries", []) if entry]

    async def resolve(self, query: str, allow_playlist: bool = False) -> list[Track]:
        query = query.strip()
        if self._closed:
            raise MediaError("The media service is shutting down.")
        if not query or len(query) > 2000:
            raise MediaError("Enter a song name or a music link of at most 2,000 characters.")
        platform = platform_for(query)
        cacheable = (
            platform == "youtube"
            and not allow_playlist
            and (
                _parse_url(query).scheme not in {"http", "https"}
                or _youtube_video_id(query) is not None
            )
        )
        key = (query, allow_playlist)
        if cacheable:
            cached = self._resolution_cache.get(key)
            if cached is not None:
                expiry, tracks = cached
                if expiry > monotonic():
                    self._resolution_cache.move_to_end(key)
                    return [replace(track, requester_id=None, added_at=0) for track in tracks]
                self._resolution_cache.pop(key)
        tracks = await self._resolve(query, allow_playlist)
        if cacheable and tracks and not self._closed:
            now = monotonic()
            for previous, (expiry, _) in list(self._resolution_cache.items()):
                if expiry <= now:
                    self._resolution_cache.pop(previous)
            self._resolution_cache[key] = (
                now + RESOLUTION_CACHE_SECONDS,
                tuple(replace(track, requester_id=None, added_at=0) for track in tracks),
            )
            self._resolution_cache.move_to_end(key)
            while len(self._resolution_cache) > RESOLUTION_CACHE_ENTRIES:
                self._resolution_cache.popitem(last=False)
        return tracks

    async def _resolve(self, query: str, allow_playlist: bool = False) -> list[Track]:
        platform = platform_for(query)
        if platform == "spotify":
            return await self.spotify(query)
        if platform == "tidal":
            return await self._run_native(lambda: self.tidal.resolve(query))
        if platform == "direct":
            return [await self.direct_info(query)]
        if _parse_url(query).scheme not in {"http", "https"}:
            return await self.search(query, 1)
        if platform == "youtube":
            query = normalize_youtube(query, allow_playlist)
        playlist = allow_playlist or (platform == "soundcloud" and "/sets/" in query)
        info = await self.extract(
            query,
            noplaylist=not playlist,
            extract_flat="in_playlist",
            playlistend=self.settings.max_playlist,
        )
        entries = info.get("entries")
        if entries is not None:
            return [self.track(entry, platform) for entry in entries if entry][
                : self.settings.max_playlist
            ]
        track = self.track(info, platform, query)
        track.start_seconds = start_offset(query)
        return [track] if info else []

    async def _public_response(
        self, method: str, url: str, *, headers: dict[str, str] | None = None
    ) -> aiohttp.ClientResponse:
        for _ in range(6):
            options = {"headers": headers} if headers is not None else {}
            response = await self.http.request(
                method, public_http_url(url), allow_redirects=False, **options
            )
            if response.status in {301, 302, 303, 307, 308}:
                location = response.headers.get("Location")
                response.release()
                if not location:
                    break
                url = urljoin(url, location)
                continue
            try:
                response.raise_for_status()
            except aiohttp.ClientError:
                response.release()
                raise
            return response
        raise MediaError("The audio link redirected too many times.")

    async def direct_info(self, url: str) -> Track:
        try:
            try:
                response = await self._public_response("HEAD", url)
            except aiohttp.ClientResponseError as error:
                if error.status not in {405, 501}:
                    raise
                response = await self._public_response("GET", url)
            try:
                try:
                    size = max(0, int(response.headers.get("Content-Length", 0)))
                except TypeError, ValueError, OverflowError:
                    size = 0
                if size > self.settings.max_download_bytes:
                    raise MediaError("That audio file exceeds the configured download limit.")
                name = Path(_parse_url(str(response.url)).path).stem.replace("_", " ")
                # A CDN redirect may expire before this queued song starts. Keep
                # the stable source so downloading follows a fresh redirect.
                return Track(title=name or "Audio file", url=url, platform="direct")
            finally:
                response.release()
        except (aiohttp.ClientError, TimeoutError, ValueError) as error:
            raise MediaError("That audio file is not accessible.") from error

    async def download(self, track: Track, directory: Path, cancel: threading.Event) -> Path:
        if track.platform == "direct":
            return await self._download_direct(track, directory, cancel)
        if track.platform == "tidal":
            if cancel.is_set():
                raise MediaError("Playback request cancelled.")
            try:
                stream_url = await self._run_native(lambda: self.tidal.stream_url(track))
                source = replace(track, url=public_http_url(stream_url), media_url=None)
                result = await self._download_direct(source, directory, cancel)
                track.duration = source.duration
                return result
            except MediaError as error:
                # CDN errors may contain short-lived signed URLs. Keep them out
                # of exception chains and retain the canonical TIDAL track URL.
                raise MediaError(str(error)) from None
        if track.platform == "spotify" and not track.media_url:
            results = await self.search(f"{track.title} {track.artist}", 5)
            if not results:
                raise MediaError("No YouTube match was found for that Spotify song.")

            candidate = max(results, key=lambda item: self._spotify_match_score(track, item))
            if self._spotify_match_score(track, candidate) < 0.65:
                raise MediaError(
                    "No reliable YouTube match was found for that Spotify song. "
                    "Try /search with its title and artist."
                )
            track.media_url = candidate.url
            track.age_restricted = candidate.age_restricted

        def run(use_cookies: bool, age_restricted: bool) -> Path:
            def progress(status: dict[str, Any]) -> None:
                if cancel.is_set():
                    raise yt_dlp.utils.DownloadCancelled("Playback request cancelled")
                if (status.get("downloaded_bytes") or 0) > self.settings.max_download_bytes:
                    raise yt_dlp.utils.DownloadCancelled("Audio exceeds the download limit")

            if cancel.is_set():
                raise MediaError("Playback request cancelled.")
            if (
                track.platform in {"youtube", "spotify"}
                and track.age_restricted
                and not use_cookies
            ):
                raise _YouTubeAgeRestricted
            if age_restricted:
                track.age_restricted = True
            query = track.media_url or track.url

            def retrieve(output: Path, cached: dict[str, Any] | None = None) -> Path:
                with self._downloader(
                    use_cookies=use_cookies,
                    format="bestaudio/best",
                    outtmpl=str(output / "source.%(ext)s"),
                    max_filesize=self.settings.max_download_bytes,
                    progress_hooks=[progress],
                ) as downloader:
                    info = (
                        downloader.process_ie_result(cached, download=True)
                        if cached is not None
                        else downloader.extract_info(query, download=True)
                    )
                    if not info:
                        raise MediaError("The provider did not return audio.")
                    track.age_restricted = (
                        track.age_restricted or finite_seconds(info.get("age_limit")) >= 18
                    )
                    track.duration = finite_seconds(info.get("duration")) or finite_seconds(
                        track.duration
                    )
                    downloads = info.get("requested_downloads") or [{}]
                    filename = downloads[0].get("filepath") or downloader.prepare_filename(info)
                    result = Path(filename).resolve()
                    if not result.is_relative_to(output.resolve()) or not result.is_file():
                        raise MediaError("The provider did not finish downloading the audio.")
                    return result

            if use_cookies:
                # Authenticated retries must not reuse a guest .part file.
                output = Path(tempfile.mkdtemp(prefix="authenticated-", dir=directory)).resolve()
                if not output.is_relative_to(directory.resolve()):
                    raise MediaError("The audio download directory is unavailable.")
                return retrieve(output)

            cached = self._cached_extraction(query)
            if cached is not None:
                identity, info = cached
                if finite_seconds(info.get("age_limit")) >= 18:
                    track.age_restricted = True
                    raise _YouTubeAgeRestricted
                if info.pop("_dj_khaled_requires_cookies", False):
                    raise _YouTubeCookieRequired
                # An isolated attempt prevents stale .part files or a completed
                # stale attempt being mistaken for the fresh fallback's output.
                attempt = Path(tempfile.mkdtemp(prefix="cached-", dir=directory)).resolve()
                if not attempt.is_relative_to(directory.resolve()):
                    raise MediaError("The audio download directory is unavailable.")
                succeeded = False
                try:
                    result = retrieve(attempt, info)
                    succeeded = True
                    return result
                except (
                    yt_dlp.utils.DownloadError,
                    yt_dlp.utils.ExtractorError,
                    yt_dlp.utils.ReExtractInfo,
                ) as error:
                    with self._extraction_lock:
                        self._extraction_cache.pop(identity, None)
                    if cancel.is_set():
                        raise yt_dlp.utils.DownloadCancelled("Playback request cancelled") from None
                    if track.platform in {"youtube", "spotify"} and (
                        self._youtube_bot_failure(error) or self._youtube_age_failure(error)
                    ):
                        raise
                    # Retry one URL extraction with the same permit and limits.
                    # DownloadCancelled (including size limits) bypasses this.
                finally:
                    if not succeeded:
                        shutil.rmtree(attempt, ignore_errors=True)
            return retrieve(directory)

        try:
            return await self._run_native(
                lambda: self._with_youtube_cookies(
                    run, youtube=track.platform in {"youtube", "spotify"}
                )
            )
        except _YouTubeAgeRestricted as error:
            track.age_restricted = True
            raise self._youtube_auth_error() from error
        except _YouTubeCookieRetryFailed as error:
            raise self._youtube_cookie_error() from error
        except (
            yt_dlp.utils.DownloadError,
            yt_dlp.utils.ExtractorError,
            yt_dlp.utils.ReExtractInfo,
        ) as error:
            if track.platform in {"youtube", "spotify"} and self._youtube_auth_failure(error):
                raise MediaError(
                    "YouTube blocked guest access to this video. "
                    "Try again later or choose another track."
                ) from error
            raise MediaError(
                "Audio download failed. Try another link or try again later."
            ) from error

    @staticmethod
    def _spotify_match_score(track: Track, candidate: Track) -> float:
        def normalized(value: str) -> str:
            return re.sub(r"[^\w]+", " ", value.casefold()).strip()

        def title(value: str) -> str:
            value = re.sub(
                r"[\[(](?:official (?:music )?(?:video|audio)|"
                r"lyrics?|audio|video|visualizer|hd|4k)[)\]]",
                "",
                value,
                flags=re.IGNORECASE,
            )
            value = re.sub(
                r"\s+(?:[-–—|]\s*)?(?:official (?:music )?(?:video|audio)|lyrics?|hd|4k)$",
                "",
                value,
                flags=re.IGNORECASE,
            )
            value = re.sub(r"\s+(?:ft\.?|feat\.?|featuring)\s+.+$", "", value, flags=re.IGNORECASE)
            return normalized(value)

        wanted_artist = normalized(track.artist.split(",")[0])
        actual_artist = normalized(
            re.sub(
                r"(?:vevo|\s*(?:-\s*)?topic|\s+official)$",
                "",
                candidate.artist,
                flags=re.IGNORECASE,
            )
        )
        wanted_title, actual_title = title(track.title), title(candidate.title)
        artist_in_title = bool(
            wanted_artist
            and (actual_title == wanted_artist or actual_title.startswith(wanted_artist + " "))
        )
        if artist_in_title:
            actual_title = actual_title[len(wanted_artist) :].strip()
        if wanted_artist and wanted_title.startswith(wanted_artist + " "):
            wanted_title = wanted_title[len(wanted_artist) :].strip()
        artist_score = SequenceMatcher(None, wanted_artist, actual_artist).ratio()
        if artist_in_title:
            artist_score = 1.0
        title_score = SequenceMatcher(None, wanted_title, actual_title).ratio()
        if not wanted_artist or not wanted_title or artist_score < 0.6 or title_score < 0.65:
            return -1
        for version in (
            "cover",
            "karaoke",
            "instrumental",
            "live",
            "remix",
            "sped up",
            "slowed",
            "nightcore",
        ):
            if bool(re.search(rf"\b{version}\b", actual_title)) != bool(
                re.search(rf"\b{version}\b", wanted_title)
            ):
                return -1
        duration_penalty = 0.0
        if track.duration and candidate.duration:
            difference = abs(track.duration - candidate.duration)
            if difference > max(20, track.duration * 0.2):
                return -1
            duration_penalty = difference / track.duration * 0.2
        return title_score * 0.75 + artist_score * 0.25 - duration_penalty

    async def _download_direct(
        self, track: Track, directory: Path, cancel: threading.Event
    ) -> Path:
        target = directory / "source.audio"
        try:
            if cancel.is_set():
                raise MediaError("Playback request cancelled.")
            response = await self._public_response("GET", track.url)
            try:
                count = 0
                pending = bytearray()
                written = False
                async for chunk in response.content.iter_chunked(65536):
                    if cancel.is_set():
                        raise MediaError("Playback request cancelled.")
                    count += len(chunk)
                    if count > self.settings.max_download_bytes:
                        raise MediaError("The audio file exceeds the download limit.")
                    pending.extend(chunk)
                    if len(pending) >= 1024 * 1024:
                        await self._write_direct_batch(target, bytes(pending), append=written)
                        pending.clear()
                        written = True
                if cancel.is_set():
                    raise MediaError("Playback request cancelled.")
                if not count:
                    raise MediaError("The audio file is empty.")
                if pending:
                    await self._write_direct_batch(target, bytes(pending), append=written)
            finally:
                response.release()
        except (aiohttp.ClientError, TimeoutError) as error:
            raise MediaError(
                "That audio file could not be downloaded. Please try again."
            ) from error
        except OSError as error:
            raise MediaError(
                "The audio file could not be saved. Check the server's storage."
            ) from error
        await self.update_metadata(track, target)
        return target

    @staticmethod
    async def _write_direct_batch(path: Path, data: bytes, *, append: bool) -> None:
        def write() -> None:
            with path.open("ab" if append else "wb") as output:
                output.write(data)

        task = asyncio.create_task(asyncio.to_thread(write))
        cancelled = False
        while True:
            try:
                await asyncio.shield(task)
                break
            except asyncio.CancelledError:
                # Finish the current write before cache cleanup can remove its directory.
                if task.cancelled():
                    raise
                cancelled = True
            except Exception:
                if cancelled:
                    raise asyncio.CancelledError from None
                raise
        if cancelled:
            raise asyncio.CancelledError

    async def update_metadata(self, track: Track, path: Path) -> None:
        """Fill missing duration from local audio without changing the track's identity."""
        track.duration = finite_seconds(track.duration)
        if track.duration > 0:
            return
        try:
            info = await asyncio.to_thread(path.stat)
        except OSError:
            return
        # Published cache files are immutable. Their mtime is deliberately updated
        # on access, while file identity survives publication from the temp path.
        key = (track.cache_key, info.st_dev, info.st_ino, info.st_size)
        lock, callers = self._metadata_locks.get(key, (asyncio.Lock(), 0))
        self._metadata_locks[key] = (lock, callers + 1)
        try:
            async with lock:
                now = asyncio.get_running_loop().time()
                cached = self._metadata_cache.get(key)
                if cached and cached[0] > now:
                    self._metadata_cache.move_to_end(key)
                    track.duration = cached[1]
                    return
                duration = await self._probe_duration(path)
                self._metadata_cache[key] = (now + (3600 if duration else 300), duration)
                self._metadata_cache.move_to_end(key)
                while len(self._metadata_cache) > 256:
                    self._metadata_cache.popitem(last=False)
                track.duration = duration
        finally:
            _, callers = self._metadata_locks[key]
            if callers == 1:
                self._metadata_locks.pop(key)
            else:
                self._metadata_locks[key] = (lock, callers - 1)

    async def _probe_duration(self, path: Path) -> float:
        process = None
        try:
            process = await asyncio.create_subprocess_exec(
                self.settings.ffmpeg,
                "-hide_banner",
                "-nostdin",
                "-protocol_whitelist",
                "file,pipe",
                "-i",
                str(path),
                "-t",
                "0",
                "-f",
                "null",
                "-",
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
            )
            async with asyncio.timeout(15):
                output = bytearray()
                assert process.stderr is not None
                while chunk := await process.stderr.read(65536):
                    output.extend(chunk)
                    if len(output) > 1024 * 1024:
                        return 0
                await process.wait()
            match = re.search(
                rb"(?m)^  Duration: (\d+):([0-5]\d):([0-5]\d(?:\.\d+)?),",
                output,
            )
            if match:
                hours, minutes, seconds = (float(part) for part in match.groups())
                return finite_seconds(hours * 3600 + minutes * 60 + seconds)
        except OSError, TimeoutError:
            log.debug("Duration metadata was unavailable for a direct audio file.")
        finally:
            if process is not None:
                await self._close_probe(process)
        return 0

    @staticmethod
    async def _close_probe(process: asyncio.subprocess.Process) -> None:
        async def finish() -> None:
            if process.returncode is None:
                try:
                    process.kill()
                except ProcessLookupError:
                    pass
            try:
                async with asyncio.timeout(5):
                    if process.stderr is not None:
                        # A killed child can still have unread pipe data. Drain it
                        # without retaining it so both the pipe and process close.
                        while await process.stderr.read(65536):
                            pass
                    await process.wait()
            except TimeoutError, OSError:
                # asyncio.Process has no public close(). A descendant retaining a
                # pipe must not keep the local transport alive indefinitely.
                transport = getattr(process, "_transport", None)
                if transport is not None:
                    transport.close()
                try:
                    async with asyncio.timeout(1):
                        await process.wait()
                except TimeoutError, OSError:
                    log.warning("The duration probe did not finish closing its process.")

        cleanup = asyncio.create_task(finish())
        cancelled = False
        while True:
            try:
                await asyncio.shield(cleanup)
                break
            except asyncio.CancelledError:
                if cleanup.cancelled():
                    raise
                cancelled = True
        if cancelled:
            raise asyncio.CancelledError

    async def _spotify_request(self, method: str, url: str, **kwargs: Any) -> dict[str, Any]:
        try:
            for attempt in range(3):
                async with getattr(self.http, method)(url, **kwargs) as response:
                    if response.status == 429:
                        try:
                            delay = float(response.headers.get("Retry-After", "1"))
                        except ValueError:
                            delay = 1.0
                        if not math.isfinite(delay) or delay < 0:
                            delay = 1.0
                        if attempt == 2 or delay > 5:
                            raise MediaError("Spotify is rate limited. Please try again later.")
                    else:
                        if response.status == 401:
                            body = bytearray()
                            async for chunk in response.content.iter_chunked(4096):
                                body.extend(chunk)
                                if len(body) > 16384:
                                    break
                            user_required = False
                            if len(body) <= 16384:
                                try:
                                    data = json.loads(body)
                                    detail = data.get("error") if isinstance(data, dict) else None
                                    message = (
                                        detail.get("message") if isinstance(detail, dict) else None
                                    )
                                    user_required = (
                                        isinstance(message, str)
                                        and message.strip().casefold()
                                        == "valid user authentication required"
                                    )
                                except ValueError:
                                    pass
                            raise _SpotifyUnauthorized(user_required=user_required)
                        if response.status == 404:
                            raise _SpotifyNotFound
                        if response.status == 403:
                            raise _SpotifyAccessDenied
                        if response.status != 200:
                            raise MediaError(
                                f"Spotify could not load that link (HTTP {response.status})."
                            )
                        body = bytearray()
                        async for chunk in response.content.iter_chunked(65536):
                            body.extend(chunk)
                            if len(body) > 2 * 1024 * 1024:
                                raise MediaError(
                                    "Spotify returned too much data. Try another link."
                                )
                        try:
                            result = json.loads(body)
                        except ValueError as error:
                            raise MediaError("Spotify returned an invalid response.") from error
                        if not isinstance(result, dict):
                            raise MediaError("Spotify returned an invalid response.")
                        return result
                # Release the response/connection before waiting to retry.
                await asyncio.sleep(delay)
        except (aiohttp.ClientError, TimeoutError) as error:
            raise MediaError("Spotify is temporarily unavailable. Please try again.") from error
        raise MediaError("Spotify is temporarily unavailable. Please try again.")

    async def _spotify_access_token(self) -> str:
        async with self._spotify_lock:
            if asyncio.get_running_loop().time() >= self._spotify_expiry:
                if not self.settings.spotify_id or not self.settings.spotify_secret:
                    raise MediaError(
                        "Spotify links need SPOTIFY_CLIENT_ID and SPOTIFY_CLIENT_SECRET."
                    )
                try:
                    data = await self._spotify_request(
                        "post",
                        "https://accounts.spotify.com/api/token",
                        headers={
                            "Authorization": "Basic "
                            + base64.b64encode(
                                f"{self.settings.spotify_id}:{self.settings.spotify_secret}".encode()
                            ).decode("ascii")
                        },
                        data={"grant_type": "client_credentials"},
                    )
                except _SpotifyUnauthorized as error:
                    raise MediaError(
                        "Spotify authentication failed. Check its credentials."
                    ) from error
                token = data.get("access_token")
                try:
                    expires = float(data.get("expires_in", 0))
                except TypeError, ValueError:
                    expires = 0
                if (
                    not isinstance(token, str)
                    or not token
                    or not math.isfinite(expires)
                    or expires <= 0
                ):
                    raise MediaError("Spotify returned invalid authentication data.")
                self._spotify_token = token
                self._spotify_expiry = asyncio.get_running_loop().time() + max(1, expires - 30)
            return self._spotify_token

    async def _spotify_api(self, endpoint: str) -> dict[str, Any]:
        playlist = endpoint.startswith("playlists/")
        user_auth = playlist and self.spotify_user.configured
        for attempt in range(2):
            token = (
                await self.spotify_user.access_token()
                if user_auth
                else await self._spotify_access_token()
            )
            try:
                return await self._spotify_request(
                    "get",
                    "https://api.spotify.com/v1/" + endpoint,
                    headers={"Authorization": "Bearer " + token},
                )
            except _SpotifyUnauthorized as error:
                if error.user_required and playlist and not user_auth:
                    raise MediaError(SPOTIFY_PLAYLIST_LOGIN) from error
                if user_auth:
                    await self.spotify_user.invalidate(token)
                else:
                    async with self._spotify_lock:
                        if self._spotify_token == token:
                            self._spotify_expiry = 0
                if attempt:
                    if playlist:
                        raise MediaError(SPOTIFY_PLAYLIST_LOGIN) from error
                    raise MediaError(
                        "Spotify rejected this app's authorization after renewing its token. "
                        "Check the Spotify app's credentials and API access."
                    ) from error
            except _SpotifyAccessDenied as error:
                if playlist:
                    raise MediaError(
                        "Spotify denied playlist access. "
                        + (
                            "The connected account must own or collaborate on the playlist, "
                            "and the app must have permission to read it. "
                            "Try a song link or /search."
                            if user_auth
                            else SPOTIFY_PLAYLIST_LOGIN
                        )
                    ) from error
                raise
        raise MediaError("Spotify authentication failed.")

    async def _spotify_public_body(self, url: str, limit: int) -> bytes:
        response = await self._public_response("GET", url, headers={"Accept-Language": "en"})
        try:
            if _parse_url(str(response.url)).hostname != "open.spotify.com":
                raise MediaError("Spotify redirected its track details to an unsupported site.")
            body = bytearray()
            async for chunk in response.content.iter_chunked(65536):
                body.extend(chunk)
                if len(body) > limit:
                    raise MediaError("Spotify returned too much public track data. Try /search.")
            return bytes(body)
        finally:
            response.release()

    @staticmethod
    def _spotify_page_metadata(body: bytes) -> dict[str, str]:
        soup = BeautifulSoup(
            body.decode("utf-8", errors="replace"),
            "html.parser",
            parse_only=SoupStrainer(["meta", "title"]),
        )
        metadata = {
            str(tag.get("property") or tag.get("name")): str(tag.get("content") or "")
            for tag in soup.find_all("meta")
        }
        title = metadata.get("og:title", "")
        artist = ""
        page_title = soup.title.get_text() if soup.title else ""
        match = re.fullmatch(r"(.+) - song and lyrics by (.+) \| Spotify", page_title)
        if match and match[1].strip().casefold() == title.strip().casefold():
            artist = match[2]
        else:
            parts = re.split(r"\s*[·•]\s*", metadata.get("og:description", ""))
            if len(parts) >= 3 and any(part.casefold() == "song" for part in parts[1:]):
                artist = parts[0]
        return {
            "title": title,
            "artist": artist,
            "type": metadata.get("og:type", ""),
            "duration": metadata.get("music:duration", ""),
        }

    async def _spotify_public_track(self, identity: str) -> Track:
        canonical = "https://open.spotify.com/track/" + identity
        try:
            async with asyncio.timeout(20), self._spotify_public_workers:
                raw = await self._spotify_public_body(
                    "https://open.spotify.com/oembed?" + urlencode({"url": canonical}), 256 * 1024
                )
                data = json.loads(raw)
                if not isinstance(data, dict) or data.get("provider_name") != "Spotify":
                    raise ValueError("Invalid oEmbed provider")
                title = data.get("title")
                if not isinstance(title, str):
                    raise ValueError("Invalid oEmbed title")
                title = " ".join(title.split())
                page = await self._spotify_public_body(canonical, 2 * 1024 * 1024)
                metadata = await asyncio.to_thread(self._spotify_page_metadata, page)
                page_title = " ".join(metadata["title"].split())
                artist = " ".join(metadata["artist"].split())
                if (
                    not title
                    or len(title) > 500
                    or not artist
                    or len(artist) > 500
                    or metadata["type"] != "music.song"
                    or page_title.casefold() != title.casefold()
                ):
                    raise MediaError(
                        "Spotify did not provide a matching song title and artist. "
                        "Try /search with the song's title and artist."
                    )
                thumbnail = data.get("thumbnail_url")
                if not isinstance(thumbnail, str):
                    thumbnail = None
                elif thumbnail:
                    try:
                        public_http_url(thumbnail)
                    except MediaError:
                        thumbnail = None
                return Track(
                    title=title,
                    artist=artist,
                    url=canonical,
                    duration=finite_seconds(metadata["duration"]),
                    thumbnail=thumbnail,
                    platform="spotify",
                )
        except (aiohttp.ClientError, OSError, TimeoutError, ValueError, RecursionError) as error:
            raise MediaError(
                "Spotify's public song details are unavailable. "
                "Try again, or use /search with the song's title and artist."
            ) from error

    @staticmethod
    def _spotify_identity(query: str) -> tuple[str, str]:
        parsed = _parse_url(query)
        if parsed.scheme == "spotify":
            parts = parsed.path.split(":")
        else:
            public_http_url(query)
            if parsed.hostname != "open.spotify.com":
                raise MediaError("Use an open.spotify.com song, album, playlist, or artist link.")
            parts = parsed.path.strip("/").split("/")
            if parts and re.fullmatch(r"intl-[\w-]+", parts[0], re.IGNORECASE):
                parts.pop(0)
            if parts and parts[0].casefold() == "embed":
                parts.pop(0)
        if (
            len(parts) != 2
            or parts[0].casefold() not in {"track", "album", "playlist", "artist"}
            or not re.fullmatch(r"[A-Za-z0-9]{1,64}", parts[1])
        ):
            raise MediaError("Use a Spotify track, album, playlist, or artist link.")
        return parts[0].casefold(), parts[1]

    async def spotify(self, query: str) -> list[Track]:
        if _parse_url(query).hostname == "spotify.link":
            try:
                async with asyncio.timeout(20):
                    try:
                        response = await self._public_response("HEAD", query)
                    except aiohttp.ClientResponseError as error:
                        if error.status not in {405, 501}:
                            raise
                        response = await self._public_response("GET", query)
                    try:
                        query = str(response.url)
                    finally:
                        response.release()
            except (aiohttp.ClientError, TimeoutError) as error:
                raise MediaError(
                    "That Spotify short link could not be opened. Try its full song link."
                ) from error
        kind, identity = self._spotify_identity(query)
        user_playlist = kind == "playlist" and self.spotify_user.configured
        if not (self.settings.spotify_id and self.settings.spotify_secret) and not user_playlist:
            if kind != "track":
                raise MediaError(
                    "Without Spotify API credentials, I can open single song links only. "
                    "Albums, playlists and artists need API credentials and permitted app access. "
                    "Use a single song link or /search."
                )
            return [await self._spotify_public_track(identity)]
        album: dict[str, Any] = {}
        if kind == "track":
            entries = [await self._spotify_api("tracks/" + identity)]
        elif kind == "artist":
            try:
                result = await self._spotify_api(
                    f"artists/{identity}/top-tracks?market={self.settings.spotify_market}"
                )
            except _SpotifyNotFound as error:
                raise MediaError(
                    "Spotify could not find or load this artist's tracks. Artist top tracks are "
                    "unavailable to development apps. Try a single song link or /search."
                ) from error
            entries = result.get("tracks", [])
        else:
            if kind == "album":
                album = await self._spotify_api("albums/" + identity)
                endpoint = f"albums/{identity}/tracks?limit=50"
            else:
                endpoint = f"playlists/{identity}/items?limit=50"
            tracks = []
            visited = set()
            while endpoint and len(tracks) < self.settings.max_playlist:
                if endpoint in visited or len(visited) >= max(20, self.settings.max_playlist):
                    break
                visited.add(endpoint)
                try:
                    page = await self._spotify_api(endpoint)
                except _SpotifyNotFound:
                    if kind != "playlist" or "/items?" not in endpoint:
                        raise
                    page = await self._spotify_api(endpoint.replace("/items?", "/tracks?"))
                tracks.extend(self._spotify_tracks(page.get("items", []), album))
                next_page = page.get("next") or ""
                if not isinstance(next_page, str) or (
                    next_page and not next_page.startswith("https://api.spotify.com/v1/")
                ):
                    raise MediaError("Spotify returned an invalid collection page.")
                endpoint = next_page.removeprefix("https://api.spotify.com/v1/")
            return tracks[: self.settings.max_playlist]
        return self._spotify_tracks(entries, album)[: self.settings.max_playlist]

    @staticmethod
    def _spotify_tracks(entries: list[Any], album: dict[str, Any]) -> list[Track]:
        tracks = []
        if not isinstance(entries, list):
            return tracks
        for item in entries:
            if not isinstance(item, dict):
                continue
            if ("track" in item and item["track"] is None) or (
                "item" in item and item["item"] is None
            ):
                continue
            data = item.get("track") or item.get("item") or item
            if (
                not isinstance(data, dict)
                or not data.get("id")
                or data.get("is_local")
                or data.get("type", "track") != "track"
            ):
                continue
            album_data = data.get("album") or album
            images = album_data.get("images", []) if isinstance(album_data, dict) else []
            thumbnail = (
                images[0].get("url")
                if isinstance(images, list) and images and isinstance(images[0], dict)
                else None
            )
            artists = data.get("artists")
            artist = (
                ", ".join(
                    str(item["name"])
                    for item in artists
                    if isinstance(item, dict) and item.get("name")
                )
                if isinstance(artists, list)
                else ""
            )
            urls = data.get("external_urls")
            url = urls.get("spotify") if isinstance(urls, dict) else None
            try:
                duration = float(data.get("duration_ms") or 0) / 1000
            except TypeError, ValueError:
                duration = 0
            if not math.isfinite(duration) or duration < 0:
                duration = 0
            tracks.append(
                Track(
                    title=str(data.get("name") or "Unknown track"),
                    artist=artist,
                    url=str(url or f"https://open.spotify.com/track/{data['id']}"),
                    duration=duration,
                    thumbnail=thumbnail,
                    platform="spotify",
                )
            )
        return tracks
