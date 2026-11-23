"""Optional, local Spotify sign-in for owner/collaborator playlist access."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import math
import os
import secrets
import tempfile
import webbrowser
from collections.abc import Coroutine, Iterator
from contextlib import contextmanager, suppress
from pathlib import Path
from typing import Any
from urllib.parse import urlencode, urlsplit

import aiohttp
from aiohttp import web

from .config import Settings
from .instance import InstanceLock
from .models import MediaError, UserError

_LIMIT = 64 * 1024
_SCOPES = frozenset({"playlist-read-private"})
_RECONNECT = "Run uv run dj-khaled --spotify-login locally to connect Spotify again."
_TOKEN_URL = "https://accounts.spotify.com/api/token"


@contextmanager
def _store_lock(settings: Settings) -> Iterator[None]:
    lock = InstanceLock(settings.data_directory / "spotify-auth")
    try:
        lock.__enter__()
    except UserError:
        raise MediaError(
            "Spotify authentication is busy or its data directory is unavailable. "
            "Please retry after the other sign-in or refresh finishes."
        ) from None
    try:
        yield
    finally:
        lock.__exit__()


async def _finish_transaction[T](operation: Coroutine[Any, Any, T]) -> T:
    """Keep exchanged/rotated credentials durable before releasing their lock."""
    task = asyncio.create_task(operation)
    cancelled = False
    while True:
        try:
            result = await asyncio.shield(task)
            break
        except asyncio.CancelledError:
            if task.cancelled():
                raise
            cancelled = True
        except Exception:
            if cancelled:
                raise asyncio.CancelledError from None
            raise
    if cancelled:
        raise asyncio.CancelledError
    return result


def _secret(value: Any) -> bool:
    return (
        isinstance(value, str)
        and 0 < len(value) <= 8192
        and all(33 <= ord(character) <= 126 for character in value)
    )


def _scope(value: Any) -> str:
    if not isinstance(value, str) or len(value) > 4096 or not _SCOPES <= set(value.split()):
        raise MediaError("Spotify playlist read permission is missing. " + _RECONNECT)
    return value


def _read_store(path: Path, client_id: str) -> dict[str, str]:
    try:
        with path.open("rb") as stream:
            body = stream.read(_LIMIT + 1)
        if len(body) > _LIMIT:
            raise ValueError
        data = json.loads(body)
        if not isinstance(data, dict) or not _secret(data.get("refresh_token")):
            raise ValueError
        if data.get("client_id") != client_id:
            raise MediaError("The saved Spotify sign-in belongs to a different app. " + _RECONNECT)
        return {
            "client_id": client_id,
            "refresh_token": data["refresh_token"],
            "scope": _scope(data.get("scope")),
        }
    except MediaError:
        raise
    except OSError, ValueError, TypeError, RecursionError:
        raise MediaError("The saved Spotify sign-in cannot be read. " + _RECONNECT) from None


def _write_store(path: Path, data: dict[str, str]) -> None:
    temporary = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary = tempfile.mkstemp(prefix=".spotify-auth-", dir=path.parent)
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            os.chmod(temporary, 0o600)
            json.dump(data, stream)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except OSError:
        raise MediaError(
            "Spotify sign-in could not be saved. Check the data directory permissions."
        ) from None
    finally:
        if temporary is not None:
            with suppress(OSError):
                Path(temporary).unlink(missing_ok=True)


async def _token_request(session: aiohttp.ClientSession, data: dict[str, str]) -> dict[str, Any]:
    try:
        async with session.post(
            _TOKEN_URL,
            data=data,
            allow_redirects=False,
            timeout=aiohttp.ClientTimeout(total=20),
        ) as response:
            body = bytearray()
            async for chunk in response.content.iter_chunked(8192):
                body.extend(chunk)
                if len(body) > _LIMIT:
                    raise MediaError("Spotify returned too much authentication data.")
            try:
                result = json.loads(body)
            except ValueError, RecursionError:
                raise MediaError("Spotify returned invalid authentication data.") from None
            if not isinstance(result, dict):
                raise MediaError("Spotify returned invalid authentication data.")
            if response.status != 200:
                if result.get("error") == "invalid_grant":
                    raise MediaError("Spotify sign-in was revoked or expired. " + _RECONNECT)
                if response.status == 429:
                    raise MediaError("Spotify is rate limited. Please try again later.")
                raise MediaError("Spotify rejected the account sign-in. " + _RECONNECT)
            return result
    except aiohttp.ClientError, TimeoutError:
        raise MediaError(
            "Spotify authentication is temporarily unavailable. Please try again."
        ) from None


def _access_data(data: dict[str, Any]) -> tuple[str, float]:
    token = data.get("access_token")
    try:
        lifetime = float(data.get("expires_in", 0))
    except TypeError, ValueError, OverflowError:
        lifetime = 0
    if (
        not _secret(token)
        or not math.isfinite(lifetime)
        or lifetime <= 0
        or str(data.get("token_type", "bearer")).casefold() != "bearer"
    ):
        raise MediaError("Spotify returned invalid authentication data.")
    return token, lifetime - min(30, lifetime * 0.1)


class SpotifyUserAuth:
    def __init__(
        self, settings: Settings, session: aiohttp.ClientSession, *, read_only: bool = False
    ) -> None:
        self.settings = settings
        self.session = session
        self.path = settings.data_directory / "spotify-auth.json"
        self.read_only = read_only
        self._lock = asyncio.Lock()
        self._token = ""
        self._expiry = 0.0
        self._credentials: dict[str, str] | None = None

    @property
    def configured(self) -> bool:
        return self.path.is_file()

    async def access_token(self) -> str:
        async with self._lock:
            if self._token and asyncio.get_running_loop().time() < self._expiry:
                return self._token
            if not self.settings.spotify_id:
                raise MediaError("Set SPOTIFY_CLIENT_ID before connecting a Spotify account.")
            if self.read_only:
                raise MediaError(
                    "Spotify user authentication needs permission to save rotated credentials."
                )
            return await _finish_transaction(self._refresh())

    async def _refresh(self) -> str:
        with _store_lock(self.settings):
            previous = await asyncio.to_thread(_read_store, self.path, self.settings.spotify_id)
            data = await _token_request(
                self.session,
                {
                    "grant_type": "refresh_token",
                    "refresh_token": previous["refresh_token"],
                    "client_id": self.settings.spotify_id,
                },
            )
            token, lifetime = _access_data(data)
            credentials = {
                "client_id": self.settings.spotify_id,
                "refresh_token": data.get("refresh_token", previous["refresh_token"]),
                "scope": _scope(data.get("scope", previous["scope"])),
            }
            if not _secret(credentials["refresh_token"]):
                raise MediaError("Spotify returned invalid authentication data.")
            if credentials != previous:
                await asyncio.to_thread(_write_store, self.path, credentials)
            self._credentials = credentials
            self._token = token
            self._expiry = asyncio.get_running_loop().time() + lifetime
            return token

    async def invalidate(self, token: str) -> None:
        async with self._lock:
            if self._token == token:
                self._expiry = 0


def _redirect(value: str) -> tuple[str, int, str]:
    try:
        parsed = urlsplit(value)
        if (
            parsed.scheme != "http"
            or parsed.hostname != "127.0.0.1"
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
            or not parsed.path.startswith("/")
            or not parsed.port
        ):
            raise ValueError
        return parsed.hostname, parsed.port, parsed.path
    except TypeError, ValueError:
        raise MediaError(
            "SPOTIFY_REDIRECT_URI must be a local address such as "
            "http://127.0.0.1:8888/callback, also registered in Spotify app settings."
        ) from None


def _authorization(client_id: str, redirect_uri: str) -> tuple[str, str, str]:
    state = secrets.token_urlsafe(32)
    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest())
    url = "https://accounts.spotify.com/authorize?" + urlencode(
        {
            "client_id": client_id,
            "response_type": "code",
            "redirect_uri": redirect_uri,
            "scope": " ".join(sorted(_SCOPES)),
            "state": state,
            "code_challenge": challenge.decode("ascii").rstrip("="),
            "code_challenge_method": "S256",
        }
    )
    return url, state, verifier


def _callback(state: str, result: asyncio.Future[str]):
    async def callback(request: web.Request) -> web.Response:
        query = request.query
        if (
            len(query.getall("state", [])) != 1
            or not _secret(query.get("state"))
            or not secrets.compare_digest(query.get("state", ""), state)
            or len(query.getall("code", [])) > 1
            or len(query.getall("error", [])) > 1
            or ("code" in query and "error" in query)
        ):
            return web.Response(status=400, text="Invalid Spotify sign-in response. Try again.")
        if result.done():
            return web.Response(status=409, text="This sign-in has already been used.")
        if query.get("error"):
            result.set_exception(MediaError("Spotify sign-in was declined. No access was saved."))
            return web.Response(text="Spotify sign-in was declined. You can close this page.")
        code = query.get("code", "")
        if not _secret(code):
            return web.Response(status=400, text="Spotify did not provide a valid sign-in code.")
        result.set_result(code)
        return web.Response(text="Spotify approval received. You can close this page.")

    return callback


async def login(
    settings: Settings, *, open_browser: bool = True, wait_seconds: float = 300
) -> None:
    if not _secret(settings.spotify_id):
        raise MediaError("Set SPOTIFY_CLIENT_ID before connecting a Spotify account.")
    redirect_uri = getattr(settings, "spotify_redirect_uri", "http://127.0.0.1:8888/callback")
    host, port, path = _redirect(redirect_uri)
    url, state, verifier = _authorization(settings.spotify_id, redirect_uri)
    result = asyncio.get_running_loop().create_future()
    application = web.Application(client_max_size=_LIMIT)
    application.router.add_get(path, _callback(state, result), allow_head=False)
    runner = web.AppRunner(application, access_log=None)
    try:
        await runner.setup()
        try:
            await web.TCPSite(runner, host, port).start()
        except OSError:
            raise MediaError(
                "The Spotify callback port is unavailable. Close another sign-in window or "
                "choose a different SPOTIFY_REDIRECT_URI and register it in Spotify settings."
            ) from None
        print("Sign in with the Spotify account that owns or collaborates on the playlist.")
        print("Approve playlist read access in your browser:")
        print(url, flush=True)
        if open_browser:
            try:
                await asyncio.to_thread(webbrowser.open, url)
            except OSError, webbrowser.Error:
                print("The browser could not be opened. Open the sign-in URL above manually.")
        try:
            async with asyncio.timeout(wait_seconds):
                code = await result
        except TimeoutError:
            raise MediaError("Spotify sign-in timed out. " + _RECONNECT) from None

        async def exchange() -> None:
            with _store_lock(settings):
                async with aiohttp.ClientSession() as session:
                    data = await _token_request(
                        session,
                        {
                            "grant_type": "authorization_code",
                            "client_id": settings.spotify_id,
                            "code": code,
                            "redirect_uri": redirect_uri,
                            "code_verifier": verifier,
                        },
                    )
                _access_data(data)
                credentials = {
                    "client_id": settings.spotify_id,
                    "refresh_token": data.get("refresh_token"),
                    "scope": _scope(data.get("scope")),
                }
                if not _secret(credentials["refresh_token"]):
                    raise MediaError("Spotify did not provide a refresh token. " + _RECONNECT)
                await asyncio.to_thread(
                    _write_store, settings.data_directory / "spotify-auth.json", credentials
                )

        await _finish_transaction(exchange())
        print("Spotify connected. Restart DJ Khaled to use the new account sign-in.")
    finally:
        if not result.done():
            result.cancel()
        elif not result.cancelled():
            result.exception()
        await _finish_transaction(runner.cleanup())
