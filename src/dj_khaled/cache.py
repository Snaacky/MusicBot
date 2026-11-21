"""Shared, bounded cache with atomic publication and reference-counted leases."""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
import tempfile
import threading
import time
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from uuid import uuid4

from .media import MediaResolver
from .models import MediaError, Track

log = logging.getLogger(__name__)


@dataclass(slots=True)
class CacheJob:
    task: asyncio.Task[Path]
    cancel: threading.Event
    waiters: int = 0


@dataclass(frozen=True, slots=True)
class AudioLease:
    key: str
    path: Path
    identity: str = field(default_factory=lambda: uuid4().hex)


class AudioCache:
    def __init__(self, directory: Path, media: MediaResolver, max_bytes: int) -> None:
        self.directory = directory
        self.media = media
        self.max_bytes = max_bytes
        self._jobs: dict[str, CacheJob] = {}
        self._references: Counter[str] = Counter()
        self._leases: set[AudioLease] = set()
        self._lock = asyncio.Lock()
        self._closed = False

    async def acquire(self, track: Track) -> AudioLease:
        key = track.cache_key
        lease: AudioLease | None = None
        async with self._lock:
            if self._closed:
                raise MediaError("The audio cache is shutting down.")
            target = self.directory / (key + ".audio")
            try:
                complete = target.is_file() and target.stat().st_size > 0
            except OSError:
                complete = False
            if complete:
                lease = self._pin(key, target)
                try:
                    os.utime(target, None)
                except OSError:
                    log.debug("Could not update cache access time", exc_info=True)
            job = self._jobs.get(key)
            if job and job.task.done() and not job.waiters:
                self._jobs.pop(key, None)
                job = None
            if lease is None and job and job.cancel.is_set() and not job.waiters:
                # Wait outside the lock before retrying. Replacing a still-running job
                # loses ownership and can race two publishers for the same cache key.
                retiring = job.task
            else:
                retiring = None
            if lease is None and job is None:
                cancel = threading.Event()
                job = CacheJob(asyncio.create_task(self._download(track, target, cancel)), cancel)
                # Consume a background failure even if all waiting users cancelled.
                job.task.add_done_callback(lambda task: self._completed(key, job, task))
                self._jobs[key] = job
            if lease is None and retiring is None:
                job.waiters += 1
        if retiring is not None:
            try:
                await asyncio.shield(retiring)
            except Exception:
                pass
            return await self.acquire(track)
        try:
            if lease is None:
                result = await asyncio.shield(job.task)
                async with self._lock:
                    if self._closed:
                        raise MediaError("The audio cache is shutting down.")
                    lease = self._pin(key, result)
            if track.duration <= 0:
                await self.media.update_metadata(track, lease.path)
            await self.prune(protect={key})
            return lease
        except BaseException:
            if lease is not None:
                self._unpin(lease)
            raise
        finally:
            if not complete:
                # These event-loop-only bookkeeping operations must not suspend:
                # cancellation during a return handoff must not leak a pinned file.
                job.waiters -= 1
                if job.waiters == 0:
                    if not job.task.done():
                        job.cancel.set()
                    elif self._jobs.get(key) is job:
                        self._jobs.pop(key, None)

    def _pin(self, key: str, path: Path) -> AudioLease:
        lease = AudioLease(key, path)
        self._references[key] += 1
        self._leases.add(lease)
        return lease

    def _unpin(self, lease: AudioLease) -> None:
        if lease not in self._leases:
            return
        self._leases.remove(lease)
        self._references[lease.key] -= 1
        if not self._references[lease.key]:
            self._references.pop(lease.key, None)

    def _completed(self, key: str, job: CacheJob, task: asyncio.Task[Path]) -> None:
        if not task.cancelled():
            task.exception()
        if not job.waiters and self._jobs.get(key) is job:
            self._jobs.pop(key, None)

    async def _download(self, track: Track, target: Path, cancel: threading.Event) -> Path:
        self.directory.mkdir(parents=True, exist_ok=True)
        temporary = Path(tempfile.mkdtemp(prefix="download-", dir=self.directory))
        try:
            result = await self.media.download(track, temporary, cancel)
            if cancel.is_set():
                raise MediaError("Playback request cancelled.")
            size = result.stat().st_size
            if not size or size > self.media.settings.max_download_bytes:
                raise MediaError("The provider returned an empty or oversized audio file.")
            os.replace(result, target)
            return target
        finally:
            await asyncio.to_thread(shutil.rmtree, temporary, True)

    async def release(self, lease: AudioLease) -> None:
        # Unpin before the first suspension. Prune's locked protection snapshot
        # can retain an extra file for one pass, never evict a still-owned lease.
        self._unpin(lease)
        await self.prune()

    async def prefetch(self, track: Track) -> None:
        lease = await self.acquire(track)
        await self.release(lease)

    async def prune(self, protect: set[str] | None = None) -> None:
        async with self._lock:
            protected = set(self._references) | set(self._jobs) | (protect or set())
            task = asyncio.create_task(asyncio.to_thread(self._prune_files, protected))
            cancelled = False
            while True:
                try:
                    await asyncio.shield(task)
                    break
                except asyncio.CancelledError:
                    if task.cancelled():
                        raise
                    # Keep ownership while filesystem work finishes, just as a DB
                    # transaction must retain its lock after cancellation.
                    cancelled = True
            if cancelled:
                raise asyncio.CancelledError

    def _prune_files(self, protected: set[str]) -> None:
        entries = []
        try:
            for path in self.directory.glob("*.audio"):
                try:
                    info = path.stat()
                    entries.append((info.st_mtime, info.st_size, path))
                except OSError:
                    continue
        except OSError:
            log.warning("The audio cache could not be scanned", exc_info=True)
            return
        entries.sort(key=lambda entry: entry[0])
        total = sum(size for _, size, _ in entries)
        for _, size, entry in entries:
            if total <= self.max_bytes:
                break
            if entry.stem in protected:
                continue
            try:
                entry.unlink()
                total -= size
            except OSError:
                log.debug("Could not evict an audio cache file", exc_info=True)

    async def close(self) -> None:
        async with self._lock:
            self._closed = True
            jobs = list(self._jobs.values())
            for job in jobs:
                job.cancel.set()
        if jobs:
            await asyncio.gather(*(job.task for job in jobs), return_exceptions=True)
        await self.prune()

    async def cleanup_stale(self) -> None:
        self.directory.mkdir(parents=True, exist_ok=True)
        for path in self.directory.glob("download-*"):
            # A second process can own a fresh download; do not remove its working files.
            if path.is_dir() and time.time() - path.stat().st_mtime > 86400:
                await asyncio.to_thread(shutil.rmtree, path, True)
        await self.prune()
