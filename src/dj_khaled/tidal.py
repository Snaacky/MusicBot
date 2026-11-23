"""TIDAL metadata and clear audio streams through python-tidal."""

from __future__ import annotations

import base64
import binascii
import ipaddress
import json
import re
import unicodedata
from collections.abc import Callable
from html import unescape
from typing import Any
from urllib.parse import urlsplit
from uuid import UUID

import tidalapi
from tidalapi.exceptions import InvalidISRC, ObjectNotFound

from .config import Settings
from .models import MediaError, Track, finite_seconds, normalize_isrc
from .tidal_auth import TidalUserAuth

_HOSTS = frozenset({"tidal.com", "www.tidal.com", "listen.tidal.com"})
_MANIFEST_LIMIT = 2 * 1024 * 1024
_BTS = "application/vnd.tidal.bts"
_NUMERIC_ID = re.compile(r"[1-9][0-9]{0,19}")
_MATCH_LIMIT = 10
_NO_MATCH = "No reliable TIDAL match was found for that Spotify song. Try a TIDAL track link."
_VERSION_WORDS = {
    "cover": r"\bcover(?:ed)?\b",
    "karaoke": r"\bkaraoke\b",
    "instrumental": r"\binstrumental\b",
    "live": r"\blive\b",
    "remix": r"\bremix(?:ed)?\b",
    "acoustic": r"\bacoustic\b",
    "demo": r"\bdemo\b",
    "edit": r"\bedit\b",
    "radio": r"\bradio\b",
    "single": r"\bsingle\b",
    "extended": r"\bextended\b",
    "sped up": r"\b(?:sped|speed)\s+up\b",
    "slowed": r"\bslowed\b",
    "nightcore": r"\bnightcore\b",
    "remaster": r"\bremaster(?:ed)?\b",
    "rerecorded": r"\bre[\s-]?record(?:ed|ing)\b",
    "mono": r"\bmono\b",
    "stereo": r"\bstereo\b",
}


def _normalized(value: str) -> str:
    value = unicodedata.normalize("NFKD", unescape(value).casefold())
    value = "".join(character for character in value if not unicodedata.combining(character))
    return " ".join(re.findall(r"[^\W_]+", value, re.UNICODE))


def _artist_credits(value: str) -> frozenset[str]:
    return frozenset(
        name
        for item in re.split(r"\s*(?:,|;|&|\band\b)\s*", value, flags=re.IGNORECASE)
        if (name := _normalized(item))
    )


def _title_identity(value: str) -> tuple[str, tuple[tuple[Any, ...], ...]]:
    versions: list[tuple[Any, ...]] = []

    def qualifier(text: str) -> str:
        text = text.strip()
        if re.match(r"^(?:feat\.?|ft\.?|featuring)\s+", text, re.IGNORECASE):
            return ""
        normalized = _normalized(text)
        if normalized in {
            "album version",
            "original version",
            "studio version",
            "original mix",
            "official audio",
            "official video",
            "audio",
            "lyrics",
        }:
            return ""
        flags = tuple(
            sorted(
                name
                for name, pattern in _VERSION_WORDS.items()
                if re.search(pattern, text, re.IGNORECASE)
            )
        )
        if not flags:
            return text
        years = tuple(sorted(re.findall(r"\b(?:19|20)[0-9]{2}\b", text)))
        remainder = re.sub(r"\b(?:19|20)[0-9]{2}\b", " ", text)
        for pattern in _VERSION_WORDS.values():
            remainder = re.sub(pattern, " ", remainder, flags=re.IGNORECASE)
        remainder = re.sub(
            r"\b(?:version|mix|by|at|from|recorded|digital)\b", " ", remainder, flags=re.IGNORECASE
        )
        versions.append((flags, years, _normalized(remainder)))
        return ""

    value = re.sub(
        r"\(([^()]*)\)|\[([^\[\]]*)\]",
        lambda match: " " + qualifier(match.group(1) or match.group(2) or "") + " ",
        value,
    )
    parts = re.split(r"\s+[–—-]\s+", value)
    value = " ".join([parts[0], *(qualifier(part) for part in parts[1:])])
    value = re.sub(r"\s+(?:feat\.?|ft\.?|featuring)\s+.*$", "", value, flags=re.IGNORECASE)
    return _normalized(value), tuple(sorted(versions))


def _match_score(wanted: Track, candidate: Track) -> float:
    wanted_artist, actual_artist = _normalized(wanted.artist), _normalized(candidate.artist)
    if not wanted_artist or not actual_artist:
        return -1
    if wanted_artist != actual_artist and (
        _artist_credits(wanted.artist) != _artist_credits(candidate.artist)
    ):
        return -1
    featured = r"\b(?:feat\.?|ft\.?|featuring)\s+([^\])]+)"
    wanted_featured = re.findall(featured, wanted.title, re.IGNORECASE)
    actual_featured = re.findall(featured, candidate.title, re.IGNORECASE)
    if (
        wanted_featured
        and actual_featured
        and (
            _artist_credits(" ".join(wanted_featured)) != _artist_credits(" ".join(actual_featured))
        )
    ):
        return -1
    if (
        wanted_featured
        and not actual_featured
        and not (_artist_credits(" ".join(wanted_featured)) <= _artist_credits(candidate.artist))
    ):
        return -1
    if (
        actual_featured
        and not wanted_featured
        and not (_artist_credits(" ".join(actual_featured)) <= _artist_credits(wanted.artist))
    ):
        return -1
    title, versions = _title_identity(wanted.title)
    if not title or (title, versions) != _title_identity(candidate.title):
        return -1
    if wanted.isrc and candidate.isrc and wanted.isrc != candidate.isrc:
        return -1
    if wanted.duration and candidate.duration:
        tolerance = max(3, min(8, wanted.duration * 0.04))
        difference = abs(wanted.duration - candidate.duration)
        if difference > tolerance:
            return -1
        return 1 - 0.05 * difference / tolerance
    return 0.95


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
        if parts[-1] == "u":
            parts = parts[:-1]
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
        isrc=normalize_isrc(getattr(item, "isrc", None)),
    )


class TidalService:
    def __init__(self, settings: Settings, *, read_only: bool = False) -> None:
        self.settings = settings
        self.auth = TidalUserAuth(settings, read_only=read_only)

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

    def match(self, track: Track) -> Track:
        """Find a playable recording using ISRC first, then conservative metadata."""
        if not track.title.strip() or not track.artist.strip():
            raise MediaError(_NO_MATCH)

        def matching(session: Any) -> Track:
            def best(items: list[Any]) -> Track | None:
                ranked = [
                    (score, candidate)
                    for item in items[:_MATCH_LIMIT]
                    if (candidate := _model_track(item)) is not None
                    and (score := _match_score(track, candidate)) >= 0.95
                ]
                return max(ranked, key=lambda item: item[0])[1] if ranked else None

            isrc = normalize_isrc(track.isrc)
            if isrc:
                try:
                    result = session.request.request(
                        "GET",
                        "tracks",
                        params={"filter[isrc]": isrc},
                        base_url=session.config.openapi_v2_location,
                    ).json()
                except ObjectNotFound, InvalidISRC:
                    result = {"data": []}
                if not isinstance(result, dict) or not isinstance(result.get("data"), list):
                    raise MediaError("TIDAL returned invalid recording information.")
                candidates = []
                for item in result["data"][:_MATCH_LIMIT]:
                    if not isinstance(item, dict) or not _NUMERIC_ID.fullmatch(
                        str(item.get("id", ""))
                    ):
                        continue
                    try:
                        candidates.append(session.track(str(item["id"])))
                    except ObjectNotFound:
                        continue
                if (candidate := best(candidates)) is not None:
                    return candidate
            title, _versions = _title_identity(track.title)
            query = f"{track.artist[:240]} {title[:250]}"
            result = session.search(query, models=[tidalapi.Track], limit=_MATCH_LIMIT)
            if not isinstance(result, dict) or not isinstance(result.get("tracks"), list):
                raise MediaError("TIDAL returned invalid search information.")
            if (candidate := best(result["tracks"])) is not None:
                return candidate
            raise MediaError(_NO_MATCH)

        return self._run(matching, "TIDAL could not match that Spotify song. Please try again.")

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
            if not isinstance(urls, list) or not urls:
                raise MediaError("TIDAL returned an invalid audio manifest.")
            if len(urls) > 1:
                # Playing only the first part would silently cut the song short.
                raise MediaError(
                    "This TIDAL stream is split into parts. Set TIDAL_QUALITY=HIGH and retry."
                )
            try:
                _public_https(urls[0])
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
