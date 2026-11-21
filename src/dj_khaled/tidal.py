"""TIDAL metadata and clear audio streams through python-tidal."""

from __future__ import annotations

import base64
import binascii
import ipaddress
import json
import re
from collections.abc import Callable
from typing import Any
from urllib.parse import urlsplit
from uuid import UUID

from .config import Settings
from .models import MediaError, Track, finite_seconds
from .tidal_auth import TidalUserAuth

_HOSTS = frozenset({"tidal.com", "www.tidal.com", "listen.tidal.com"})
_MANIFEST_LIMIT = 2 * 1024 * 1024
_BTS = "application/vnd.tidal.bts"
_NUMERIC_ID = re.compile(r"[1-9][0-9]{0,19}")


def tidal_link(query: str) -> tuple[str, str]:
    """Parse supported share/listen links without following arbitrary redirects."""
    try:
        parsed = urlsplit(query)
        if (
            parsed.scheme not in {"http", "https"}
            or parsed.hostname not in _HOSTS
            or parsed.username is not None
            or parsed.password is not None
            or parsed.port not in {None, 80 if parsed.scheme == "http" else 443}
        ):
            raise ValueError
        parts = parsed.path.strip("/").split("/")
        if parts and parts[0] == "browse":
            parts = parts[1:]
        if len(parts) == 4 and parts[0] == "album" and parts[2] == "track":
            if not _NUMERIC_ID.fullmatch(parts[1]):
                raise ValueError
            parts = parts[2:]
        if len(parts) != 2:
            raise ValueError
        kind, identity = parts
        if kind in {"track", "album"} and _NUMERIC_ID.fullmatch(identity):
            return kind, identity
        if kind == "playlist" and re.fullmatch(
            r"[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}", identity
        ):
            return kind, str(UUID(identity))
    except ValueError, TypeError:
        pass
    raise MediaError("Use a TIDAL track, album, or playlist share link.")


def _public_https(value: Any) -> str:
    if not isinstance(value, str) or not value or len(value) > 16384:
        raise ValueError("Invalid TIDAL media URL.")
    parsed = urlsplit(value)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.port not in {None, 443}
        or parsed.hostname == "localhost"
        or parsed.hostname.endswith(".localhost")
    ):
        raise ValueError("Invalid TIDAL media URL.")
    try:
        address = ipaddress.ip_address(parsed.hostname)
    except ValueError:
        address = None
    if address is not None and not address.is_global:
        raise ValueError("Invalid TIDAL media URL.")
    return value


def _model_track(item: Any) -> Track | None:
    identity = str(getattr(item, "id", ""))
    media_type = getattr(item, "type", None)
    if (
        not _NUMERIC_ID.fullmatch(identity)
        or (media_type is not None and str(media_type).casefold() != "track")
        or getattr(item, "available", None) is False
        or getattr(item, "allow_streaming", None) is False
    ):
        return None
    artists = getattr(item, "artists", None) or [getattr(item, "artist", None)]
    names = [str(artist.name) for artist in artists if getattr(artist, "name", None)]
    thumbnail = None
    album = getattr(item, "album", None)
    if album is not None:
        try:
            thumbnail = _public_https(album.image(320))
        except Exception:
            pass
    return Track(
        title=getattr(item, "full_name", None)
        or getattr(item, "name", None)
        or getattr(item, "title", None)
        or "Unknown track",
        artist=", ".join(names),
        duration=finite_seconds(getattr(item, "duration", 0)),
        thumbnail=thumbnail,
        url=f"https://tidal.com/browse/track/{identity}",
        platform="tidal",
    )


class TidalService:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.auth = TidalUserAuth(settings)

    def _run[T](self, operation: Callable[[Any], T], message: str) -> T:
        try:
            return self.auth.run(operation)
        except MediaError:
            raise
        except Exception:
            # SDK exception text can contain signed URLs or account credentials.
            raise MediaError(message) from None

    def resolve(self, query: str) -> list[Track]:
        kind, identity = tidal_link(query)

        def metadata(session: Any) -> list[Track]:
            if kind == "track":
                track = _model_track(session.track(identity))
                if track is None:
                    raise MediaError("That TIDAL track is unavailable for the connected account.")
                return [track]
            collection = session.album(identity) if kind == "album" else session.playlist(identity)
            tracks: list[Track] = []
            offset = 0
            # Bound work even if a provider returns repeated or unavailable pages.
            page_budget = max(20, (self.settings.max_playlist + 99) // 100 + 1)
            for _ in range(page_budget):
                limit = min(100, self.settings.max_playlist - len(tracks))
                if limit <= 0:
                    break
                page = collection.tracks(limit=limit, offset=offset)
                if not isinstance(page, list):
                    raise MediaError("TIDAL returned invalid playlist information.")
                for item in page[:limit]:
                    if (track := _model_track(item)) is not None:
                        tracks.append(track)
                offset += len(page)
                total = getattr(collection, "num_tracks", None)
                if (
                    len(page) < limit
                    or len(tracks) >= self.settings.max_playlist
                    or (isinstance(total, int) and total >= 0 and offset >= total)
                ):
                    break
            if not tracks:
                raise MediaError("That TIDAL collection contains no available tracks.")
            return tracks

        return self._run(metadata, "TIDAL could not load that music. Try another TIDAL link.")

    def stream_url(self, track: Track) -> str:
        kind, identity = tidal_link(track.url)
        if kind != "track":
            raise MediaError("Use a TIDAL track link for playback.")

        def stream_info(session: Any) -> str:
            source = session.track(identity)
            if _model_track(source) is None or str(source.id) != identity:
                raise MediaError("That TIDAL track is unavailable for the connected account.")
            stream = source.get_stream()
            if str(getattr(stream, "manifest_mime_type", "")) != _BTS:
                raise MediaError(
                    "This TIDAL stream format is unsupported. Set TIDAL_QUALITY=HIGH and retry."
                )
            encoded = getattr(stream, "manifest", None)
            if not isinstance(encoded, str) or not encoded or len(encoded) > _MANIFEST_LIMIT:
                raise MediaError("TIDAL returned an invalid audio manifest.")
            try:
                payload = base64.b64decode(encoded, validate=True)
                data = json.loads(payload)
            except ValueError, TypeError, binascii.Error:
                raise MediaError("TIDAL returned an invalid audio manifest.") from None
            if not isinstance(data, dict):
                raise MediaError("TIDAL returned an invalid audio manifest.")
            if data.get("encryptionType") != "NONE" or data.get("keyId"):
                raise MediaError("This TIDAL stream is encrypted and cannot be played by the bot.")
            if str(data.get("mimeType", "")).split(";", 1)[0].strip().casefold() not in {
                "audio/aac",
                "audio/flac",
                "audio/x-flac",
                "audio/mp3",
                "audio/mpeg",
                "audio/mp4",
                "audio/m4a",
            }:
                raise MediaError("TIDAL returned an unsupported audio manifest.")
            if str(data.get("codecs", "")).upper().split(".")[0] not in {
                "AAC",
                "MP4A",
                "MP3",
                "FLAC",
            }:
                raise MediaError(
                    "This TIDAL audio codec is unsupported. Set TIDAL_QUALITY=HIGH and retry."
                )
            urls = data.get("urls")
            if not isinstance(urls, list) or not 1 <= len(urls) <= 16:
                raise MediaError("TIDAL returned an invalid audio manifest.")
            try:
                for url in urls:
                    _public_https(url)
                manifest = stream.get_stream_manifest()
                selected = manifest.get_urls()
                if selected != urls:
                    raise ValueError
                return _public_https(selected[0])
            except Exception:
                raise MediaError("TIDAL returned an invalid audio manifest.") from None

        return self._run(
            stream_info,
            "TIDAL could not provide audio. Check the connected account's playback access.",
        )

    def close(self) -> None:
        self.auth.close()
