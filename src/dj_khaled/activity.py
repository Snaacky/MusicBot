"""Plain text activity descriptions; the player card escapes them when rendered."""

from __future__ import annotations

import unicodedata

from .models import Track

_NOTICE_LIMIT = 240
_ACTOR_LIMIT = 40
_TITLE_LIMIT = 140


def _clean(value: str) -> str:
    # Keep word boundaries while removing controls that can hide or reorder text.
    printable = "".join(
        " " if character.isspace() else character
        for character in value
        if character.isspace() or unicodedata.category(character) not in {"Cc", "Cf", "Cs"}
    )
    return " ".join(printable.split())


def _bounded(value: str, limit: int) -> str:
    return value if len(value) <= limit else value[: max(0, limit - 1)].rstrip() + "…"


def _period(value: str) -> str:
    return "" if value.endswith((".", "!", "?", "…")) else "."


def actor_name(user: object) -> str:
    """Use a readable display name without manufacturing Discord mentions."""
    for attribute in ("display_name", "name"):
        candidate = getattr(user, attribute, None)
        if isinstance(candidate, str) and (name := _clean(candidate)):
            return _bounded(name, _ACTOR_LIMIT)
    return "User"


def _title(track: Track) -> str:
    return _bounded(_clean(track.title) or "Unknown track", _TITLE_LIMIT)


def added_tracks(user: object, tracks: list[Track], count: int) -> str:
    """Summarize one queue request, including a capacity-limited partial batch."""
    accepted = max(0, min(count, len(tracks)))
    omitted = len(tracks) - accepted
    actor = actor_name(user)
    suffix = f" ({omitted} skipped; queue full.)" if omitted else ""
    if not accepted:
        return f"{actor} added 0 tracks.{suffix}"
    noun = "track" if accepted == 1 else "tracks"
    prefix = f"{actor} added {accepted} {noun}: "
    rest = f" +{accepted - 1} more." if accepted > 1 else ""
    budget = min(_TITLE_LIMIT, _NOTICE_LIMIT - len(prefix) - len(rest) - len(suffix) - 1)
    title = _bounded(_title(tracks[0]), max(1, budget))
    punctuation = "" if rest else _period(title)
    return prefix + title + rest + punctuation + suffix


def track_action(user: object, verb: str, track: Track | None) -> str:
    """Describe a track control using its title as captured before the action."""
    action = _bounded(_clean(verb) or "updated", 40)
    if action == "wentbackto":
        action = "went back to"
    if track is not None:
        action += " " + _title(track)
    elif action == "went back to":
        action = "went back"
    return user_action(user, action)


def user_action(user: object, text: str) -> str:
    """Describe a common control without escaping text a second time."""
    actor = actor_name(user)
    action = _clean(text) or "updated playback"
    action = _bounded(action, _NOTICE_LIMIT - len(actor) - 2)
    return actor + " " + action + _period(action)
