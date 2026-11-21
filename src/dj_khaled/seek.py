"""Parse playback positions without applying a particular track's limits."""

from __future__ import annotations

import re

from .models import UserError

_FORMAT_ERROR = "Enter seconds, M:SS, or H:MM:SS (for example 90 or 1:30)."


def parse_position(text: str) -> int:
    value = text.strip()
    if len(value) > 32 or re.fullmatch(r"[0-9]+(?::[0-9]{1,2}){0,2}", value) is None:
        raise UserError(_FORMAT_ERROR)
    parts = [int(component) for component in value.split(":")]
    if len(parts) > 1 and parts[-1] >= 60 or len(parts) == 3 and parts[1] >= 60:
        raise UserError(_FORMAT_ERROR)
    total = 0
    for component in parts:
        total = total * 60 + component
    return total
