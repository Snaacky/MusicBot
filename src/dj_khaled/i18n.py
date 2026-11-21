from __future__ import annotations

import json
from importlib.resources import files
from typing import Any

from .storage import StateStore


class Languages:
    """English message catalog; legacy server preferences are intentionally ignored."""

    def __init__(self, store: StateStore) -> None:
        self.store = store
        resource = files("dj_khaled").joinpath("locales", "en.json")
        self.packs: dict[str, dict[str, Any]] = {
            "en": json.loads(resource.read_text(encoding="utf-8"))
        }

    async def code(self, guild_id: int) -> str:
        return "en"

    async def set(self, guild_id: int, code: str) -> None:
        if code != "en":
            raise ValueError("DJ Khaled supports English only.")

    def text(self, code: str, key: str, default: str | None = None, **values: object) -> str:
        for pack in (self.packs["en"],):
            current: Any = pack
            for part in key.split("."):
                if not isinstance(current, dict) or part not in current:
                    break
                current = current[part]
            else:
                if isinstance(current, list):
                    current = "\n".join(str(item) for item in current)
                if isinstance(current, str):
                    result = current.replace("Beatra", "DJ Khaled")
                    for name, value in values.items():
                        result = result.replace("{" + name + "}", str(value))
                    return result
        result = default if default is not None else key
        for name, value in values.items():
            result = result.replace("{" + name + "}", str(value))
        return result
