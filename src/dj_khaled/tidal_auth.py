"""Headless TIDAL sign-in and durable OAuth sessions for native API workers."""

from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import tempfile
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager, suppress
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import requests
import tidalapi
from tidalapi.exceptions import AuthenticationError, TidalAPIError, TooManyRequests

from .config import Settings
from .instance import InstanceLock
from .models import MediaError, UserError

_LIMIT = 64 * 1024
_RECONNECT = "Run dj-khaled --tidal-login to connect TIDAL again."


def _secret(value: object) -> bool:
    return (
        isinstance(value, str)
        and 0 < len(value) <= 8192
        and all(33 <= ord(character) <= 126 for character in value)
    )


def _expiry(value: object) -> datetime:
    if not isinstance(value, str) or len(value) > 100:
        raise ValueError("Invalid expiry.")
    result = datetime.fromisoformat(value)
    return result.replace(tzinfo=UTC) if result.tzinfo is None else result.astimezone(UTC)


def _credentials(data: object) -> dict[str, Any]:
    if not isinstance(data, dict):
        raise ValueError("Invalid credentials.")
    if (
        not _secret(data.get("access_token"))
        or not _secret(data.get("refresh_token"))
        or not isinstance(data.get("token_type"), str)
        or data["token_type"].casefold() != "bearer"
        or not isinstance(data.get("is_pkce"), bool)
    ):
        raise ValueError("Invalid credentials.")
    return {
        "token_type": "Bearer",
        "access_token": data["access_token"],
        "refresh_token": data["refresh_token"],
        "expiry_time": _expiry(data.get("expiry_time")).isoformat(),
        "is_pkce": data["is_pkce"],
    }


def _read_store(path: Path) -> dict[str, Any]:
    try:
        with path.open("rb") as stream:
            body = stream.read(_LIMIT + 1)
        if len(body) > _LIMIT:
            raise ValueError("Oversized credentials.")
        return _credentials(json.loads(body))
    except OSError, ValueError, TypeError, OverflowError:
        raise MediaError("The saved TIDAL sign-in cannot be read. " + _RECONNECT) from None


def _write_store(path: Path, data: dict[str, Any]) -> None:
    temporary = None
    try:
        body = _credentials(data)
        path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary = tempfile.mkstemp(prefix=".tidal-auth-", dir=path.parent)
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            os.chmod(temporary, 0o600)
            json.dump(body, stream)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except OSError, ValueError, TypeError, OverflowError:
        raise MediaError(
            "TIDAL sign-in could not be saved. Check the data directory permissions."
        ) from None
    finally:
        if temporary is not None:
            with suppress(OSError):
                Path(temporary).unlink(missing_ok=True)


@contextmanager
def _store_lock(settings: Settings) -> Iterator[None]:
    lock = InstanceLock(settings.data_directory / "tidal-auth")
    try:
        lock.__enter__()
    except UserError:
        raise MediaError(
            "TIDAL authentication is busy or its data directory is unavailable. Please retry."
        ) from None
    try:
        yield
    finally:
        lock.__exit__()


def _session_credentials(session: tidalapi.Session) -> dict[str, Any]:
    expiry = session.expiry_time
    return _credentials(
        {
            "token_type": session.token_type,
            "access_token": session.access_token,
            "refresh_token": session.refresh_token,
            "expiry_time": expiry.isoformat() if isinstance(expiry, datetime) else None,
            "is_pkce": session.is_pkce,
        }
    )


def _response_data(response: requests.Response) -> dict[str, Any]:
    if len(response.content) > _LIMIT:
        raise MediaError("TIDAL returned too much authentication data.")
    try:
        data = response.json()
    except ValueError:
        raise MediaError("TIDAL returned invalid authentication data.") from None
    if not isinstance(data, dict):
        raise MediaError("TIDAL returned invalid authentication data.")
    return data


class _BoundedRequests(requests.Session):
    def request(self, method: str, url: str, **kwargs: Any) -> requests.Response:
        kwargs.setdefault("timeout", (10, 20))
        return super().request(method, url, **kwargs)


class _TidalSession(tidalapi.Session):
    def __init__(self, quality: str, persist: Callable[[tidalapi.Session], None]) -> None:
        super().__init__(tidalapi.Config(quality=quality))
        self.request_session.close()
        self.request_session = _BoundedRequests()
        self._persist = persist
        self._refresh_budget = 2
        # The SDK logs request URLs containing account session IDs on failures.
        logging.getLogger("tidalapi").setLevel(logging.CRITICAL)

    def token_refresh(self, refresh_token: str) -> bool:
        # tidalapi 0.8.11 discards rotated refresh tokens. Persist the complete
        # replacement before returning to the API call that triggered refresh.
        if self._refresh_budget <= 0:
            raise MediaError("TIDAL sign-in could not be refreshed. " + _RECONNECT)
        self._refresh_budget -= 1
        response = self.request_session.post(
            self.config.api_oauth2_token,
            data={
                "grant_type": "refresh_token",
                "refresh_token": refresh_token,
                "client_id": self.config.client_id_pkce if self.is_pkce else self.config.client_id,
                "client_secret": (
                    self.config.client_secret_pkce if self.is_pkce else self.config.client_secret
                ),
            },
            allow_redirects=False,
        )
        with response:
            data = _response_data(response)
            if response.status_code != 200:
                if response.status_code == 429:
                    raise MediaError("TIDAL is rate limited. Please try again later.")
                raise MediaError("TIDAL account sign-in was rejected. " + _RECONNECT)
            try:
                lifetime = float(data.get("expires_in", 0))
                if not math.isfinite(lifetime) or lifetime <= 0:
                    raise ValueError("Invalid expiry.")
                expiry = datetime.now(UTC) + timedelta(seconds=lifetime)
                credentials = _credentials(
                    {
                        "token_type": data.get("token_type", self.token_type),
                        "access_token": data.get("access_token"),
                        "refresh_token": data.get("refresh_token", refresh_token),
                        "expiry_time": expiry.isoformat(),
                        "is_pkce": self.is_pkce,
                    }
                )
            except ValueError, TypeError, OverflowError:
                raise MediaError("TIDAL returned invalid authentication data.") from None
            self.token_type = credentials["token_type"]
            self.access_token = credentials["access_token"]
            self.refresh_token = credentials["refresh_token"]
            self.expiry_time = expiry
            self._persist(self)
            return True


class TidalUserAuth:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.path = settings.data_directory / "tidal-auth.json"
        self._lock = threading.Lock()
        self._session: _TidalSession | None = None
        self._saved: dict[str, Any] | None = None

    @property
    def configured(self) -> bool:
        return self.path.is_file()

    def _persist(self, session: tidalapi.Session) -> None:
        data = _session_credentials(session)
        _write_store(self.path, data)
        self._saved = data

    def _load(self, data: dict[str, Any]) -> _TidalSession:
        if self._session is not None:
            self._session.request_session.close()
            self._session = None
        session = _TidalSession(self.settings.tidal_quality, self._persist)
        try:
            session.token_type = data["token_type"]
            session.access_token = data["access_token"]
            session.refresh_token = data["refresh_token"]
            session.expiry_time = _expiry(data["expiry_time"])
            session.is_pkce = data["is_pkce"]
            self._refresh_if_needed(session)
            if not session.load_oauth_session(
                session.token_type,
                session.access_token,
                session.refresh_token,
                session.expiry_time,
                is_pkce=session.is_pkce,
            ):
                raise MediaError("TIDAL account sign-in was rejected. " + _RECONNECT)
            self._persist(session)
            self._session = session
            return session
        except BaseException:
            session.request_session.close()
            raise

    @staticmethod
    def _refresh_if_needed(session: _TidalSession) -> None:
        expiry = session.expiry_time
        if expiry is None or _expiry(expiry.isoformat()) <= datetime.now(UTC) + timedelta(
            seconds=60
        ):
            session.token_refresh(session.refresh_token)

    def run[T](self, operation: Callable[[tidalapi.Session], T]) -> T:
        with self._lock, _store_lock(self.settings):
            if not self.configured:
                raise MediaError("TIDAL is not connected. " + _RECONNECT)
            try:
                data = _read_store(self.path)
                session = self._session
                if session is None or data != self._saved:
                    session = self._load(data)
                else:
                    session._refresh_budget = 2
                self._refresh_if_needed(session)
                try:
                    return operation(session)
                finally:
                    if _session_credentials(session) != self._saved:
                        self._persist(session)
            except MediaError:
                raise
            except AuthenticationError:
                raise MediaError("TIDAL account sign-in expired. " + _RECONNECT) from None
            except requests.HTTPError as error:
                response = error.response
                status = response.status_code if response is not None else None
                if status == 401:
                    raise MediaError("TIDAL account sign-in expired. " + _RECONNECT) from None
                if status == 403:
                    raise MediaError(
                        "TIDAL denied access. Check your subscription and access to this item."
                    ) from None
                if status == 429:
                    raise MediaError("TIDAL is rate limited. Please try again later.") from None
                raise MediaError("TIDAL is temporarily unavailable. Please try again.") from None
            except requests.RequestException:
                raise MediaError("TIDAL is temporarily unavailable. Please try again.") from None
            except TooManyRequests:
                raise MediaError("TIDAL is rate limited. Please try again later.") from None
            except TidalAPIError:
                raise MediaError("TIDAL could not load that item. Check account access.") from None
            except ValueError, TypeError, KeyError, AttributeError, OverflowError:
                raise MediaError("TIDAL returned invalid account or media data.") from None

    def close(self) -> None:
        with self._lock:
            if self._session is not None:
                self._session.request_session.close()
                self._session = None
                self._saved = None


def _login_url(value: object) -> str:
    if not _secret(value):
        raise MediaError("TIDAL returned an invalid sign-in link.")
    url = value if "://" in value else "https://" + value
    parsed = urlsplit(url)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or not (parsed.hostname == "tidal.com" or parsed.hostname.endswith(".tidal.com"))
        or parsed.username is not None
        or parsed.password is not None
        or parsed.port not in {None, 443}
    ):
        raise MediaError("TIDAL returned an invalid sign-in link.")
    return url


def _login(settings: Settings, cancel: threading.Event, wait_seconds: float) -> None:
    auth = TidalUserAuth(settings)
    with auth._lock, _store_lock(settings):
        session = _TidalSession(settings.tidal_quality, auth._persist)
        try:
            link = session.get_link_login()
            url = _login_url(link.verification_uri_complete)
            interval, expiry = float(link.interval), float(link.expires_in)
            if not all(math.isfinite(value) and value > 0 for value in (interval, expiry)):
                raise MediaError("TIDAL returned invalid sign-in timing.")
            interval = min(30, max(1, interval))
            deadline = time.monotonic() + min(expiry, wait_seconds)
            print("Open this link in your browser to connect your TIDAL account:", flush=True)
            print(url, flush=True)
            while not cancel.is_set() and time.monotonic() < deadline:
                response = session.request_session.post(
                    session.config.api_oauth2_token,
                    data={
                        "client_id": session.config.client_id,
                        "client_secret": session.config.client_secret,
                        "device_code": link.device_code,
                        "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
                        "scope": "r_usr w_usr w_sub",
                    },
                    allow_redirects=False,
                )
                with response:
                    data = _response_data(response)
                    if response.status_code == 200:
                        if not session.process_auth_token(data, is_pkce_token=False):
                            raise MediaError("TIDAL account sign-in was rejected.")
                        auth._persist(session)
                        print("TIDAL connected. Restart the bot to use this account.", flush=True)
                        return
                error = data.get("error")
                if error == "slow_down":
                    interval = min(30, interval + 5)
                elif error not in {"authorization_pending", "pending"}:
                    if error == "expired_token":
                        break
                    raise MediaError("TIDAL sign-in was declined or expired. " + _RECONNECT)
                cancel.wait(min(interval, max(0, deadline - time.monotonic())))
            if cancel.is_set():
                return
            raise MediaError("TIDAL sign-in timed out. " + _RECONNECT)
        except MediaError:
            raise
        except requests.RequestException, AuthenticationError, TidalAPIError:
            raise MediaError(
                "TIDAL sign-in is temporarily unavailable. Please try again."
            ) from None
        except ValueError, TypeError, KeyError, AttributeError, OverflowError:
            raise MediaError("TIDAL returned invalid sign-in data. Please try again.") from None
        finally:
            session.request_session.close()


async def login(settings: Settings, *, wait_seconds: float = 300) -> None:
    cancel = threading.Event()
    task = asyncio.create_task(asyncio.to_thread(_login, settings, cancel, wait_seconds))
    try:
        await asyncio.shield(task)
    except asyncio.CancelledError:
        cancel.set()
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
