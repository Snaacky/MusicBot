"""Transactional per-server state, shared safely by shard connections."""

from __future__ import annotations

import asyncio
import json
import logging
import math
import sqlite3
import time
from collections.abc import Callable
from contextlib import closing
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)
_NONFINITE = object()


def _guild_id(value: object) -> int | None:
    """Accept Discord IDs which fit SQLite's signed INTEGER column."""
    text = str(value)
    if isinstance(value, bool) or len(text) > 19 or not text.isascii() or not text.isdigit():
        return None
    number = int(text)
    return number if 0 < number <= 2**63 - 1 else None


def _nonfinite_constant(_value: str) -> object:
    return _NONFINITE


def _finite_float(value: str) -> float | object:
    # JSON's exponent syntax can overflow float() without using the special
    # NaN/Infinity tokens handled by parse_constant. Preserve a row-local marker
    # so one bad legacy record does not prevent importing its healthy neighbours.
    number = float(value)
    return number if math.isfinite(number) else _NONFINITE


def _finite_values(value: object) -> bool:
    pending = [value]
    visited = set()
    while pending:
        item = pending.pop()
        if item is _NONFINITE or isinstance(item, float) and not math.isfinite(item):
            return False
        if isinstance(item, (dict, list)):
            identity = id(item)
            if identity in visited:
                continue
            visited.add(identity)
            pending.extend(item.values() if isinstance(item, dict) else item)
    return True


def _session_payload(value: object) -> bool:
    if not isinstance(value, dict) or not _finite_values(value):
        return False
    for key in ("current", "currentTrack"):
        if key in value and value[key] is not None and not isinstance(value[key], dict):
            return False
    for key in ("queue", "history", "previousTracks"):
        if key in value and (
            not isinstance(value[key], list)
            or any(not isinstance(track, dict) for track in value[key])
        ):
            return False
    for key in ("pause_reasons", "pauseReasons"):
        if key in value and (
            not isinstance(value[key], list)
            or any(not isinstance(reason, str) for reason in value[key])
        ):
            return False
    return True


class StateStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = asyncio.Lock()

    async def _run[T](self, operation: Callable[[sqlite3.Connection], T]) -> T:
        async with self._lock:
            # Cancelling an asyncio waiter does not stop a SQLite worker thread.
            # Keep ordering ownership until that transaction has really committed,
            # so a cancelled save cannot finish after a subsequent session delete.
            worker = asyncio.get_running_loop().run_in_executor(None, self._transaction, operation)
            try:
                return await asyncio.shield(worker)
            except asyncio.CancelledError:
                while not worker.done():
                    try:
                        await asyncio.shield(worker)
                    except asyncio.CancelledError:
                        continue
                    except Exception:
                        break
                if not worker.cancelled():
                    worker.exception()  # Consume errors; caller cancellation wins.
                raise

    def _transaction[T](self, operation: Callable[[sqlite3.Connection], T]) -> T:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(self.path, timeout=15)) as connection, connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("PRAGMA busy_timeout=15000")
            return operation(connection)

    async def initialize(self) -> None:
        def create(connection: sqlite3.Connection) -> None:
            connection.executescript(
                "CREATE TABLE IF NOT EXISTS settings "
                "(guild_id INTEGER PRIMARY KEY, language TEXT NOT NULL);"
                "CREATE TABLE IF NOT EXISTS sessions "
                "(guild_id INTEGER PRIMARY KEY, payload TEXT NOT NULL, updated_at REAL NOT NULL);"
                "CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);"
            )

        await self._run(create)

    async def language(self, guild_id: int) -> str:
        def read(connection: sqlite3.Connection) -> str:
            row = connection.execute(
                "SELECT language FROM settings WHERE guild_id=?", (guild_id,)
            ).fetchone()
            return row[0] if row else "en"

        return await self._run(read)

    async def set_language(self, guild_id: int, code: str) -> None:
        await self._run(
            lambda connection: connection.execute(
                "INSERT INTO settings VALUES (?, ?) "
                "ON CONFLICT(guild_id) DO UPDATE SET language=excluded.language",
                (guild_id, code),
            )
        )

    async def save(self, guild_id: int, payload: dict[str, Any]) -> None:
        if _guild_id(guild_id) is None or not _session_payload(payload):
            raise ValueError("Invalid saved session shape or server ID.")
        encoded = json.dumps(payload, ensure_ascii=False, allow_nan=False)
        await self._run(
            lambda connection: connection.execute(
                "INSERT INTO sessions VALUES (?, ?, ?) ON CONFLICT(guild_id) "
                "DO UPDATE SET payload=excluded.payload, updated_at=excluded.updated_at",
                (guild_id, encoded, time.time()),
            )
        )

    async def delete(self, guild_id: int) -> None:
        await self._run(
            lambda connection: connection.execute(
                "DELETE FROM sessions WHERE guild_id=?", (guild_id,)
            )
        )

    async def sessions(self) -> dict[int, dict[str, Any]]:
        def read(connection: sqlite3.Connection) -> dict[int, dict[str, Any]]:
            result = {}
            for guild_id, encoded in connection.execute("SELECT guild_id, payload FROM sessions"):
                try:
                    payload = json.loads(
                        encoded, parse_constant=_nonfinite_constant, parse_float=_finite_float
                    )
                except TypeError, ValueError, RecursionError:
                    log.warning("Ignoring malformed saved session for server %s", guild_id)
                    continue
                if _guild_id(guild_id) is None or not _session_payload(payload):
                    log.warning("Ignoring invalid saved session shape for server %s", guild_id)
                    continue
                result[guild_id] = payload
            return result

        return await self._run(read)

    async def import_legacy(self, directory: Path) -> None:
        """Import an existing JS snapshot once; never overwrite Python state."""

        def migrate(connection: sqlite3.Connection) -> None:
            if connection.execute("SELECT 1 FROM metadata WHERE key='legacy_import'").fetchone():
                return
            for filename, kind in (
                ("languages.json", "settings"),
                ("playerState.json", "sessions"),
            ):
                marker = f"legacy_import:{filename}"
                if connection.execute("SELECT 1 FROM metadata WHERE key=?", (marker,)).fetchone():
                    continue
                source = directory / filename
                if not source.exists():
                    continue
                try:
                    data = json.loads(
                        source.read_text(encoding="utf-8-sig"),
                        parse_constant=_nonfinite_constant,
                        parse_float=_finite_float,
                    )
                except OSError, ValueError, RecursionError:
                    log.warning(
                        "Legacy %s could not be read; leaving it available for retry", filename
                    )
                    continue
                entries = (
                    data.get("servers" if kind == "settings" else "players")
                    if isinstance(data, dict)
                    else None
                )
                if not isinstance(entries, dict):
                    log.warning(
                        "Ignoring malformed legacy %s; import can retry after repair", filename
                    )
                    continue
                if kind == "settings":
                    for guild_id, value in entries.items():
                        identifier = _guild_id(guild_id)
                        code = value.get("language", "en") if isinstance(value, dict) else None
                        if identifier is None or not isinstance(code, str) or not code:
                            log.warning("Ignoring malformed legacy language entry")
                            continue
                        try:
                            connection.execute(
                                "INSERT OR IGNORE INTO settings VALUES (?, ?)", (identifier, code)
                            )
                        except ValueError, OverflowError:
                            log.warning("Ignoring unencodable legacy language entry")
                else:
                    for guild_id, value in entries.items():
                        identifier = _guild_id(guild_id)
                        if identifier is None or not _session_payload(value):
                            log.warning("Ignoring malformed legacy playback entry")
                            continue
                        try:
                            encoded = json.dumps(value, allow_nan=False)
                        except TypeError, ValueError, OverflowError, RecursionError:
                            log.warning("Ignoring unencodable legacy playback entry")
                            continue
                        connection.execute(
                            "INSERT OR IGNORE INTO sessions VALUES (?, ?, ?)",
                            (identifier, encoded, time.time()),
                        )
                # Each successfully decoded source is complete independently.
                # Retrying another corrupt file must not resurrect sessions that
                # Python has since intentionally deleted.
                connection.execute("INSERT OR IGNORE INTO metadata VALUES (?, '1')", (marker,))

        await self._run(migrate)
