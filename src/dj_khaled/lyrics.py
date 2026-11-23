from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from collections import OrderedDict
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from difflib import SequenceMatcher
from typing import Any
from urllib.parse import urljoin, urlsplit

import aiohttp
from bs4 import BeautifulSoup

log = logging.getLogger(__name__)
MAX_RESPONSE_BYTES = 2 * 1024 * 1024
LOOKUP_TIMEOUT = 30.0
MAX_REDIRECTS = 3
_PROVIDER_HOSTS = {
    "api.genius.com": frozenset({"api.genius.com"}),
    "genius.com": frozenset({"genius.com", "www.genius.com"}),
    "www.genius.com": frozenset({"genius.com", "www.genius.com"}),
    "lrclib.net": frozenset({"lrclib.net"}),
}


@dataclass(frozen=True, slots=True)
class Lyrics:
    text: str
    source: str


def paginate(text: str, limit: int = 3500) -> list[str]:
    if limit < 1:
        raise ValueError("The page limit must be positive.")
    pages = []
    text = text.strip()
    while text:
        if len(text) <= limit:
            pages.append(text)
            break
        boundary = text.rfind("\n", 0, limit + 1)
        if boundary < limit // 2:
            boundary = limit
        pages.append(text[:boundary].strip())
        text = text[boundary:].lstrip()
    return pages


class LyricsService:
    def __init__(self, session: aiohttp.ClientSession, genius_token: str = "") -> None:
        self.http = session
        self.genius_token = genius_token
        self.cache: OrderedDict[tuple[str, str], tuple[float, Lyrics | None]] = OrderedDict()
        self._pending: dict[tuple[str, str], asyncio.Task[Lyrics | None]] = {}
        self._workers = asyncio.Semaphore(4)
        self._closed = False

    @staticmethod
    def clean(title: str) -> str:
        def annotation(match: re.Match[str]) -> str:
            if re.fullmatch(
                r"(?:(?:official|music|lyrics?|audio|video|visualizer|hd|4k)\s*)+",
                match[1].strip(),
                flags=re.IGNORECASE,
            ):
                return ""
            return match[0]

        title = re.sub(r"[\[(]([^)\]]*)[)\]]", annotation, title)
        title = re.sub(
            r"\s+(?:[-–—|]\s*)?(?:official (?:music )?(?:video|audio)|lyrics?|4k|hd)$",
            "",
            title,
            flags=re.IGNORECASE,
        )
        return re.sub(r"\s+", " ", title).strip(" -–—|")

    async def fetch(self, title: str, artist: str) -> Lyrics | None:
        if self._closed:
            return None
        key = (title.casefold(), artist.casefold())
        cached = self.cache.get(key)
        if cached and cached[0] > time.monotonic():
            self.cache.move_to_end(key)
            return cached[1]
        task = self._pending.get(key)
        if task is None:
            if len(self._pending) >= 64:
                return None

            async def lookup() -> Lyrics | None:
                try:
                    async with asyncio.timeout(LOOKUP_TIMEOUT):
                        async with self._workers:
                            return await self._fetch(title, artist)
                except TimeoutError:
                    return None

            task = asyncio.create_task(lookup())
            self._pending[key] = task
            task.add_done_callback(lambda completed: self._complete(key, completed))
        return await asyncio.shield(task)

    def _complete(self, key: tuple[str, str], task: asyncio.Task[Lyrics | None]) -> None:
        if self._pending.get(key) is task:
            self._pending.pop(key, None)
        if task.cancelled():
            return
        try:
            result = task.result()
        except Exception as error:
            log.debug("A lyrics lookup failed (%s).", type(error).__name__)
            return
        if not self._closed:
            self.cache[key] = (time.monotonic() + (3600 if result else 300), result)
            self.cache.move_to_end(key)
            while len(self.cache) > 256:
                self.cache.popitem(last=False)

    @staticmethod
    async def _body(response: aiohttp.ClientResponse) -> bytes:
        body = bytearray()
        async for chunk in response.content.iter_chunked(65536):
            body.extend(chunk)
            if len(body) > MAX_RESPONSE_BYTES:
                raise ValueError("The lyrics response exceeds the size limit.")
        return bytes(body)

    @asynccontextmanager
    async def _get(self, url: str, **options: Any) -> AsyncIterator[aiohttp.ClientResponse]:
        """Follow only the provider's HTTPS redirects, including literal-IP checks."""
        allowed_hosts = _PROVIDER_HOSTS.get(urlsplit(url).hostname or "", frozenset())
        for redirects in range(MAX_REDIRECTS + 1):
            address = urlsplit(url)
            if (
                address.scheme != "https"
                or address.hostname not in allowed_hosts
                or address.username is not None
                or address.password is not None
                or address.port not in {None, 443}
            ):
                raise ValueError("The lyrics provider returned an unsupported address.")
            async with self.http.get(url, allow_redirects=False, **options) as response:
                if response.status not in {301, 302, 303, 307, 308}:
                    yield response
                    return
                location = response.headers.get("Location")
                if not location or redirects == MAX_REDIRECTS:
                    raise ValueError("The lyrics provider exceeded the redirect limit.")
                target = urljoin(url, location)
                if urlsplit(target).netloc != address.netloc:
                    if "headers" in options:
                        options["headers"] = {
                            name: value
                            for name, value in options["headers"].items()
                            if name.casefold()
                            not in {"authorization", "proxy-authorization", "cookie"}
                        }
                options.pop("params", None)
                url = target

    async def _parse_html(self, html: str) -> str:
        """Keep the worker permit and lookup owned until its parser actually exits."""
        task = asyncio.create_task(asyncio.to_thread(self._parse_genius, html))
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            while not task.done():
                try:
                    await asyncio.shield(task)
                except asyncio.CancelledError:
                    continue
                except Exception:
                    break
            if not task.cancelled():
                task.exception()
            raise

    @staticmethod
    def _parse_genius(html: str) -> str:
        soup = BeautifulSoup(html, "html.parser")
        return "\n".join(
            item.get_text("\n", strip=True)
            for item in soup.select('[data-lyrics-container="true"]')
        ).strip()[:100000]

    @classmethod
    def _score(cls, title: str, artist: str, candidate_title: str, candidate_artist: str) -> float:
        def normalized(value: str) -> str:
            return re.sub(r"[^\w]+", " ", value.casefold()).strip()

        def artist_name(value: str) -> str:
            return normalized(re.sub(r"(?:vevo|\s+official)$", "", value, flags=re.IGNORECASE))

        wanted_artist, actual_artist = artist_name(artist), artist_name(candidate_artist)
        wanted_title, actual_title = (
            normalized(cls.clean(title)),
            normalized(cls.clean(candidate_title)),
        )
        for name in (wanted_artist, actual_artist):
            if name and wanted_title.startswith(name + " "):
                wanted_title = wanted_title[len(name) + 1 :]
            if name and actual_title.startswith(name + " "):
                actual_title = actual_title[len(name) + 1 :]
        title_score = SequenceMatcher(None, wanted_title, actual_title).ratio()
        artist_score = SequenceMatcher(None, wanted_artist, actual_artist).ratio()
        if not wanted_title or title_score < 0.65:
            return -1
        if wanted_artist and (not actual_artist or artist_score < 0.6):
            return -1
        return title_score * 0.8 + (artist_score if wanted_artist else 1) * 0.2

    @classmethod
    def _best_lrclib(cls, title: str, artist: str, candidates: Any) -> Lyrics | None:
        if not isinstance(candidates, list):
            return None
        matches = [
            (
                cls._score(
                    title,
                    artist,
                    str(candidate.get("trackName") or ""),
                    str(candidate.get("artistName") or ""),
                ),
                candidate["plainLyrics"],
            )
            for candidate in candidates
            if isinstance(candidate, dict)
            and isinstance(candidate.get("plainLyrics"), str)
            and candidate["plainLyrics"].strip()
        ]
        if not matches:
            return None
        score, text = max(matches, key=lambda item: item[0])
        return Lyrics(text[:100000], "LRCLIB") if score >= 0 else None

    async def _fetch(self, title: str, artist: str) -> Lyrics | None:
        clean_title = self.clean(title)
        if self.genius_token:
            try:
                async with self._get(
                    "https://api.genius.com/search",
                    params={"q": f"{artist} {clean_title}"},
                    headers={"Authorization": "Bearer " + self.genius_token},
                ) as response:
                    response.raise_for_status()
                    data = json.loads(await self._body(response))
                    hits = data.get("response", {}).get("hits", [])
                matches = []
                for hit in hits if isinstance(hits, list) else []:
                    result = hit.get("result") if isinstance(hit, dict) else None
                    if not isinstance(result, dict):
                        continue
                    primary_artist = result.get("primary_artist") or {}
                    score = self._score(
                        clean_title,
                        artist,
                        str(result.get("title") or ""),
                        str(primary_artist.get("name") or ""),
                    )
                    if score >= 0:
                        matches.append((score, str(result.get("url") or "")))
                for _, url in sorted(matches, reverse=True)[:3]:
                    if not url.startswith(("https://genius.com/", "https://www.genius.com/")):
                        continue
                    async with self._get(url) as response:
                        response.raise_for_status()
                        html = (await self._body(response)).decode("utf-8", errors="replace")
                    text = await self._parse_html(html)
                    if text:
                        return Lyrics(text[:100000], "Genius")
            except (
                aiohttp.ClientError,
                ValueError,
                TypeError,
                AttributeError,
                TimeoutError,
                RecursionError,
            ):
                pass
        for params in (
            {"track_name": clean_title, "artist_name": artist},
            {"track_name": clean_title},
        ):
            try:
                async with self._get("https://lrclib.net/api/search", params=params) as response:
                    response.raise_for_status()
                    candidates = json.loads(await self._body(response))
                if result := self._best_lrclib(clean_title, artist, candidates):
                    return result
            except aiohttp.ClientError, ValueError, TimeoutError, RecursionError:
                continue
        return None

    async def close(self) -> None:
        self._closed = True
        tasks = list(self._pending.values())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self._pending.clear()
