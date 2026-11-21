"""Best-effort, application-owned monochrome icons for Discord controls."""

from __future__ import annotations

import asyncio
import logging
import struct
from importlib.resources import files
from typing import Any

import discord

log = logging.getLogger(__name__)
ICON_NAMES = ("play", "pause", "skip", "queue", "disconnect", "volume", "seek", "previous")
EMOJI_PREFIX = "dk_"
ICON_LOAD_TIMEOUT = 30.0
MAX_IMAGE_BYTES = 256 * 1024


def get_icon(bot: Any, name: str) -> discord.PartialEmoji | None:
    """Return a loaded custom icon; callers keep readable labels as fallback."""
    icon = getattr(bot, "control_icons", {}).get(name)
    return icon if isinstance(icon, discord.PartialEmoji) and icon.id else None


def _asset_images() -> dict[str, bytes]:
    images = {}
    root = files("dj_khaled").joinpath("assets", "controls")
    for name in ICON_NAMES:
        try:
            data = root.joinpath(name + ".png").read_bytes()
        except OSError:
            continue
        # Only the bundled static 128 px PNG exports are eligible for upload.
        if (
            33 <= len(data) <= MAX_IMAGE_BYTES
            and data[:8] == b"\x89PNG\r\n\x1a\n"
            and data[12:16] == b"IHDR"
            and struct.unpack(">II", data[16:24]) == (128, 128)
        ):
            images[name] = data
    return images


def _partial(emoji: Any, expected_name: str) -> discord.PartialEmoji | None:
    identity = getattr(emoji, "id", None)
    if (
        getattr(emoji, "name", None) != expected_name
        or not isinstance(identity, int)
        or isinstance(identity, bool)
        or identity <= 0
        or getattr(emoji, "available", True) is False
    ):
        return None
    return discord.PartialEmoji(
        name=expected_name, id=identity, animated=bool(getattr(emoji, "animated", False))
    )


async def load_control_icons(bot: Any) -> None:
    """Fetch once after login, then create at most eight missing app assets.

    No guild emoji permissions are used. An uncertain create outcome is never
    retried in this process; a later startup fetches the authoritative inventory.
    """
    lock = getattr(bot, "_control_icons_lock", None)
    if lock is None:
        lock = bot._control_icons_lock = asyncio.Lock()
    async with lock:
        if getattr(bot, "_control_icons_loaded", False):
            return
        bot._control_icons_loaded = True
        bot.control_icons = {}
        if not getattr(bot, "application_id", None):
            return
        try:
            images = _asset_images()
            if not images:
                log.warning("Control icon assets are unavailable; using text controls.")
                return
            async with asyncio.timeout(ICON_LOAD_TIMEOUT):
                emojis = await bot.fetch_application_emojis()
                existing = {emoji.name: emoji for emoji in emojis}
                for name in ICON_NAMES:
                    application_name = EMOJI_PREFIX + name
                    if application_name in existing:
                        icon = _partial(existing[application_name], application_name)
                    elif name in images:
                        emoji = await bot.create_application_emoji(
                            name=application_name, image=images[name]
                        )
                        icon = _partial(emoji, application_name)
                    else:
                        continue
                    if icon is not None:
                        bot.control_icons[name] = icon
            log.info("Loaded %s custom control icons", len(bot.control_icons))
        except Exception as error:
            # Icons are cosmetic. Preserve any successes and let startup proceed,
            # without exposing HTTP bodies, headers or credentials in this log.
            log.warning(
                "Custom control icons unavailable (%s); using text fallback.", type(error).__name__
            )
