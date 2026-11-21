from __future__ import annotations

import hashlib
import math
import time
from dataclasses import asdict, dataclass
from enum import StrEnum
from typing import Any


def finite_seconds(value: object) -> float:
    try:
        number = float(value)
    except TypeError, ValueError, OverflowError:
        return 0.0
    return number if math.isfinite(number) and number >= 0 else 0.0


class LoopMode(StrEnum):
    OFF = "off"
    TRACK = "track"
    QUEUE = "queue"


class UserError(Exception):
    """A message which is safe to display to a Discord user."""


class MediaError(UserError):
    pass


@dataclass(slots=True)
class Track:
    title: str
    url: str
    artist: str = ""
    duration: float = 0
    thumbnail: str | None = None
    platform: str = "youtube"
    requester_id: int | None = None
    media_url: str | None = None
    start_seconds: float = 0
    added_at: float = 0
    age_restricted: bool = False

    def __post_init__(self) -> None:
        self.title = str(self.title or "Unknown track")
        self.artist = str(self.artist or "")
        if not isinstance(self.url, str) or not self.url.strip():
            raise ValueError("A track must have a URL.")
        if not isinstance(self.thumbnail, str) or not self.thumbnail.startswith(
            ("http://", "https://")
        ):
            self.thumbnail = None
        if not isinstance(self.platform, str) or self.platform.casefold() not in {
            "youtube",
            "spotify",
            "tidal",
            "soundcloud",
            "direct",
        }:
            self.platform = "youtube"
        else:
            self.platform = self.platform.casefold()
        if not isinstance(self.media_url, str) or not self.media_url:
            self.media_url = None
        self.duration = finite_seconds(self.duration)
        self.start_seconds = finite_seconds(self.start_seconds)
        self.added_at = finite_seconds(self.added_at)
        self.age_restricted = self.age_restricted is True
        if not self.added_at:
            self.added_at = time.time()

    @property
    def cache_key(self) -> str:
        return hashlib.sha256(self.url.encode()).hexdigest()

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Track:
        requester = data.get("requester_id") or data.get("requesterId")
        return cls(
            title=str(data.get("title") or "Unknown track"),
            url=data["url"],
            artist=str(data.get("artist") or ""),
            duration=finite_seconds(data.get("duration")),
            thumbnail=data.get("thumbnail"),
            platform=data.get("platform") or "youtube",
            requester_id=int(requester) if requester else None,
            media_url=data.get("media_url") or data.get("youtubeUrl"),
            start_seconds=finite_seconds(data["start_seconds"])
            if "start_seconds" in data
            else finite_seconds(data.get("startOffsetMs")) / 1000,
            added_at=finite_seconds(data.get("added_at") or data.get("addedAt")),
            age_restricted=data.get("age_restricted") is True,
        )


GENRES: dict[str, str] = {
    "pop": "pop official audio",
    "rock": "rock official audio",
    "hiphop": "hip hop official audio",
    "electronic": "electronic dance music official",
    "jazz": "jazz official audio",
    "classical": "classical music performance",
    "metal": "heavy metal official audio",
    "country": "country music official audio",
    "rnb": "r&b soul official audio",
    "indie": "indie official audio",
    "latin": "latin reggaeton official audio",
    "kpop": "kpop official music video",
    "anime": "anime opening official",
    "lofi": "lofi hip hop beats",
    "blues": "blues official audio",
    "reggae": "reggae official audio",
    "disco": "disco official audio",
    "punk": "punk rock official audio",
    "ambient": "ambient music official",
    "random": "music official audio",
}


def duration_text(seconds: float) -> str:
    total = int(finite_seconds(seconds))
    hours, remainder = divmod(total, 3600)
    minutes, seconds = divmod(remainder, 60)
    return f"{hours}:{minutes:02}:{seconds:02}" if hours else f"{minutes}:{seconds:02}"


def progress_text(position: float, duration: float) -> str:
    position, duration = finite_seconds(position), finite_seconds(duration)
    if duration <= 0:
        return f"`{duration_text(position)}` · Live / unknown duration"
    position = min(duration, max(0, position))
    filled = min(12, int(position / duration * 12))
    bar = "▰" * filled + "▱" * (12 - filled)
    return f"`{duration_text(position)}` {bar} `{duration_text(duration)}`"
