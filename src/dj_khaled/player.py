"""One asynchronous playback worker owns every transition within a server."""

from __future__ import annotations

import asyncio
import logging
import math
import random
import threading
import time
import uuid
from collections import deque
from collections.abc import Awaitable, Callable
from contextlib import nullcontext
from dataclasses import dataclass, replace
from typing import Any

import discord

from .audio import BufferedPCM, ContinuousPCM
from .cache import AudioCache, AudioLease
from .config import Settings
from .lyrics import Lyrics, LyricsService
from .media import MediaResolver, is_music
from .models import GENRES, LoopMode, MediaError, Track, UserError
from .pcm import create_decoder
from .storage import StateStore
from .voice import PacedVoiceClient

log = logging.getLogger(__name__)
NOTIFICATION_QUEUE_LIMIT = 8
NOTIFICATION_INTERVAL = 1.0
NOTIFICATION_DEDUP_SECONDS = 15.0
NOTIFICATION_TIMEOUT = 10.0


class TimedAudio(discord.PCMVolumeTransformer):
    def __init__(self, original: discord.AudioSource, volume: float) -> None:
        super().__init__(original, volume=volume)
        self.frames = 0
        self._last_read: float | None = None
        self.late_frames = 0
        self.max_interval_ms = 0.0
        self._cleanup_done = threading.Event()

    def read(self) -> bytes:
        now = time.perf_counter()
        if self._last_read is not None:
            interval = now - self._last_read
            self.max_interval_ms = max(self.max_interval_ms, interval * 1000)
            if interval > 0.04:
                self.late_frames += 1
        self._last_read = now
        data = super().read()
        if data and not getattr(self.original, "last_read_underrun", False):
            self.frames += len(data) / 3840
        return data

    def reset_clock(self) -> None:
        self._last_read = None

    def cleanup(self) -> None:
        try:
            super().cleanup()
        finally:
            if hasattr(self, "_cleanup_done"):
                self._cleanup_done.set()

    def wait_cleanup(self, timeout: float = 5) -> bool:
        return self._cleanup_done.wait(timeout)


@dataclass(eq=False, slots=True)
class PlaybackSlot:
    track: Track
    source: TimedAudio
    lease: AudioLease
    ended: asyncio.Future[bool]
    retirement: asyncio.Task[None] | None = None


class GuildPlayer:
    def __init__(
        self,
        guild: discord.Guild,
        voice_channel: discord.VoiceChannel | discord.StageChannel,
        text_channel: Any,
        settings: Settings,
        cache: AudioCache,
        media: MediaResolver,
        store: StateStore,
        lyrics: LyricsService,
        refresh: Callable[[GuildPlayer], None],
        report: Callable[[str], Awaitable[None]],
        expired: Callable[[GuildPlayer], Awaitable[None]],
    ) -> None:
        self.guild = guild
        self.voice_channel = voice_channel
        self.text_channel = text_channel
        self.settings = settings
        self.cache = cache
        self.media = media
        self.store = store
        self.lyrics_service = lyrics
        self.refresh = refresh
        self.report = report
        self.expired = expired
        self.session_id = uuid.uuid4().hex
        self.owner_id: int | None = None
        self.current: Track | None = None
        self.queue: deque[Track] = deque()
        self.history: deque[Track] = deque(maxlen=10)
        self.volume = settings.default_volume
        self.loop_mode = LoopMode.OFF
        self.shuffle = False
        self.autoplay: str | None = None
        self.pause_reasons: set[str] = set()
        self.current_lyrics: Lyrics | None = None
        self.status = "idle"
        self.closed = False
        self.message: discord.Message | None = None
        self.queue_page: int | None = None
        self._notices: deque[tuple[str, float]] = deque(maxlen=3)
        self._lock = asyncio.Lock()
        self.ui_lock = asyncio.Lock()
        self._save_lock = asyncio.Lock()
        self._close_lock = asyncio.Lock()
        self._wake = asyncio.Event()
        self._worker: asyncio.Task[None] | None = None
        self._preparing: asyncio.Task[Any] | None = None
        self._connecting: asyncio.Task[None] | None = None
        self._checkpoint_task: asyncio.Task[None] | None = None
        self._checkpoint_dirty = False
        self._autoplay_search: asyncio.Task[list[Track]] | None = None
        self._prefetch_tasks: dict[str, asyncio.Task[None]] = {}
        self._prefetch_pending: set[asyncio.Task[None]] = set()
        self._lyrics_task: asyncio.Task[None] | None = None
        self._alone: asyncio.Task[None] | None = None
        self._idle: asyncio.Task[None] | None = None
        self._reconnect: asyncio.Task[None] | None = None
        self._notification_task: asyncio.Task[None] | None = None
        self._notifications: deque[str] = deque()
        self._notification_recent: dict[str, float] = {}
        self._notification_active: str | None = None
        self._notification_overflow = 0
        self._notification_last_sent = 0.0
        self._finished: asyncio.Future[Exception | None] | None = None
        self._source: TimedAudio | None = None
        self._stream: ContinuousPCM | None = None
        self._stream_finished: asyncio.Future[Exception | None] | None = None
        self._active_slot: PlaybackSlot | None = None
        self._next_slot: PlaybackSlot | None = None
        self._next_decode: asyncio.Task[None] | None = None
        self._next_attempt: tuple[PlaybackSlot, Track] | None = None
        self._slots: set[PlaybackSlot] = set()
        self._retiring: set[asyncio.Task[None]] = set()
        self._syncing_stream = False
        self._offset = 0.0
        self._generation = 0
        self._reason = ""
        self._retries = 0
        self._transition_started: float | None = None
        self._transition_reason = ""

    @property
    def current(self) -> Track | None:
        self._sync_handoffs()
        return self._current

    @current.setter
    def current(self, track: Track | None) -> None:
        self._current = track

    @property
    def voice(self) -> discord.VoiceClient | None:
        return self.guild.voice_client

    @property
    def position(self) -> float:
        self._sync_handoffs()
        return self._offset + (self._source.frames * 0.02 if self._source else 0)

    @property
    def paused(self) -> bool:
        return bool(self.pause_reasons)

    def note(self, message: str) -> None:
        message = " ".join(message.split())[:240]
        if not message:
            return
        now = time.monotonic()
        while self._notices and self._notices[0][1] <= now:
            self._notices.popleft()
        entry = (message, now + 15)
        if self._notices and self._notices[-1][0] == message:
            self._notices[-1] = entry
        else:
            self._notices.append(entry)
        self.refresh(self)

    @property
    def notices(self) -> list[str]:
        now = time.monotonic()
        while self._notices and self._notices[0][1] <= now:
            self._notices.popleft()
        return [message for message, _expires in self._notices]

    @property
    def has_notices(self) -> bool:
        # Keep expired entries visible to the ticker until a render removes the
        # old notice field, including while playback is paused or idle.
        return bool(self._notices)

    def start(self) -> None:
        if self.closed:
            return
        if self._worker is None or self._worker.done():
            self._worker = asyncio.create_task(self._run(), name=f"player-{self.guild.id}")
        self._wake.set()

    async def enqueue(self, tracks: list[Track], requester_id: int) -> int:
        async with self._lock:
            self._sync_handoffs()
            if self.closed:
                raise UserError("This playback session has ended. Use /play again.")
            capacity = self.settings.max_queue - len(self.queue) - bool(self.current)
            if capacity <= 0:
                raise UserError("The music queue is full.")
            accepted = [replace(track, requester_id=requester_id) for track in tracks[:capacity]]
            if not accepted:
                raise UserError("No playable tracks were found.")
            self.owner_id = self.owner_id or requester_id
            self.queue.extend(accepted)
        self._cancel_autoplay_search()
        self._cancel_idle()
        self.start()
        self._schedule_prefetch()
        self._schedule_next_decode()
        await self._persist_safely()
        self.refresh(self)
        return len(accepted)

    async def _connect(self) -> None:
        started = time.perf_counter()
        existing = self.voice
        if existing and not existing.is_connected():
            await self.connection_lost()
        if self._reconnect and not self._reconnect.done():
            await asyncio.shield(self._reconnect)
            if self.closed:
                raise UserError("The voice connection could not be restored.")
        voice = self.voice
        if voice and voice.is_connected():
            self.voice_channel = voice.channel
            return
        await self.voice_channel.connect(
            cls=PacedVoiceClient, timeout=30, reconnect=True, self_deaf=True
        )
        humans = [member for member in self.voice_channel.members if not member.bot]
        await self.listeners_changed(bool(humans))
        log.info(
            "Playback phase server=%s phase=connect elapsed_ms=%.1f",
            self.guild.id,
            (time.perf_counter() - started) * 1000,
        )

    def _connection_task(self) -> asyncio.Task[None]:
        if self._connecting is None or self._connecting.done():
            self._connecting = asyncio.create_task(
                self._connect(), name=f"voice-connect-{self.guild.id}"
            )
            # A skipped track can stop waiting while the session's handshake
            # finishes. Keep that task owned and observe failures without an
            # abandoned-task warning; the next preparation can try again.
            self._connecting.add_done_callback(
                lambda task: task.exception() if not task.cancelled() else None
            )
        return self._connecting

    def _retire_acquisition(self, task: asyncio.Task[AudioLease]) -> None:
        if not task.done():
            task.cancel()

        async def release() -> None:
            result = (await asyncio.gather(task, return_exceptions=True))[0]
            if isinstance(result, AudioLease):
                try:
                    await self.cache.release(result)
                except Exception:
                    log.exception("Audio cache release failed for server %s", self.guild.id)

        retirement = asyncio.create_task(release(), name=f"audio-lease-{self.guild.id}")
        self._retiring.add(retirement)
        retirement.add_done_callback(self._retiring.discard)

    async def _wait_for_connection(self) -> None:
        await asyncio.shield(self._connection_task())

    async def _prepare(self, track: Track) -> AudioLease:
        started = time.perf_counter()
        connection = self._connection_task()
        acquisition = asyncio.create_task(self.cache.acquire(track))
        try:
            # A connection belongs to the session, rather than an individual
            # track. Cancelling Skip's acquisition must not abandon Discord's
            # already registered, partly established voice client.
            await asyncio.gather(asyncio.shield(connection), acquisition)
            lease = acquisition.result()
        except BaseException:
            self._retire_acquisition(acquisition)
            raise
        log.info(
            "Playback phase server=%s phase=prepare elapsed_ms=%.1f",
            self.guild.id,
            (time.perf_counter() - started) * 1000,
        )
        return lease

    async def _run(self) -> None:
        try:
            while not self.closed:
                await self._wake.wait()
                self._wake.clear()
                while not self.closed:
                    async with self._lock:
                        if self.current is None and self.queue:
                            if self.shuffle and self._reason != "previous":
                                index = random.randrange(len(self.queue))
                                self.queue.rotate(-index)
                                self.current = self.queue.popleft()
                                self.queue.rotate(index)
                            else:
                                self.current = self.queue.popleft()
                            self._retries = 0
                            self._offset = self.current.start_seconds
                        track = self.current
                        self._generation += 1
                        generation = self._generation
                        self._reason = ""
                    if track is None:
                        if self._stream is not None:
                            await self._end_stream(self._stream)
                        if self.autoplay and await self._autoplay():
                            continue
                        self.status = "idle"
                        self.current_lyrics = None
                        self._schedule_prefetch()
                        self.refresh(self)
                        self._request_checkpoint()
                        self._schedule_idle()
                        break
                    await self._play(track, generation)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("Player worker failed for server %s", self.guild.id)
            await self._report_safely("Playback stopped unexpectedly. Use /play to try again.")
            try:
                await self.expired(self)
            except Exception:
                log.exception("Could not remove failed player for server %s", self.guild.id)
            if not self.closed:
                await self.close(preserve=True)

    def _sync_handoffs(self) -> None:
        stream = getattr(self, "_stream", None)
        if stream is None or self._syncing_stream:
            return
        self._syncing_stream = True
        changed = False
        try:
            with stream.lock:
                for previous, following in stream.take_boundaries():
                    if not isinstance(previous, PlaybackSlot) or not isinstance(
                        following, PlaybackSlot
                    ):
                        continue
                    if self.queue and self.queue[0] is following.track:
                        self.queue.popleft()
                    else:
                        for index, queued in enumerate(self.queue):
                            if queued is following.track:
                                del self.queue[index]
                                break
                    self.history.append(previous.track)
                    if self.loop_mode == LoopMode.QUEUE:
                        self.queue.append(previous.track)
                    self._current = following.track
                    self._source = following.source
                    self._active_slot = following
                    self._offset = following.track.start_seconds
                    self._retries = 0
                    self._generation += 1
                    self.current_lyrics = None
                    if self._next_slot is following:
                        self._next_slot = None
                    self._next_attempt = None
                    if not previous.ended.done():
                        previous.ended.set_result(True)
                    self._start_retirement(previous)
                    changed = True
                for ended in stream.take_finished():
                    if not isinstance(ended, PlaybackSlot):
                        continue
                    if self._current is ended.track and self._reason != "seek":
                        if self.loop_mode == LoopMode.TRACK:
                            self._offset = ended.track.start_seconds
                        else:
                            self._current = None
                            self._offset = 0
                            self.history.append(ended.track)
                            if self.loop_mode == LoopMode.QUEUE:
                                self.queue.append(ended.track)
                        self._source = None
                        self._active_slot = None
                        self._retries = 0
                        self._generation += 1
                        changed = True
                    if not ended.ended.done():
                        ended.ended.set_result(False)
        finally:
            self._syncing_stream = False
        if changed and not self.closed:
            self.refresh(self)
            self._schedule_next_decode()

    def _invalidate_next(self, *, keep: PlaybackSlot | None = None) -> None:
        stream = self._stream
        if stream is not None:
            with stream.lock:
                self._sync_handoffs()
                stream.remove_next()
        if self._next_decode and not self._next_decode.done():
            self._next_decode.cancel()
        self._next_attempt = None
        slot, self._next_slot = self._next_slot, keep
        if slot is not None and slot is not keep and slot is not self._active_slot:
            self._start_retirement(slot)

    def _start_retirement(self, slot: PlaybackSlot) -> asyncio.Task[None]:
        if slot.retirement is None:

            async def retire() -> None:
                try:
                    await asyncio.to_thread(slot.source.cleanup)
                except Exception:
                    log.exception("Decoder cleanup failed for server %s", self.guild.id)
                finally:
                    try:
                        await self.cache.release(slot.lease)
                    except Exception:
                        log.exception("Audio cache release failed for server %s", self.guild.id)
                    self._slots.discard(slot)

            slot.retirement = asyncio.create_task(retire())
            self._retiring.add(slot.retirement)
            slot.retirement.add_done_callback(self._retiring.discard)
        return slot.retirement

    async def _build_slot(self, track: Track, *, connect: bool, offset: float) -> PlaybackSlot:
        lease_task = asyncio.create_task(
            self._prepare(track) if connect else self.cache.acquire(track)
        )
        lease = None
        slot = None
        try:
            lease = await lease_task
            started = time.perf_counter()
            decoder = create_decoder(
                str(lease.path),
                executable=self.settings.ffmpeg,
                before_options=f"-nostdin -protocol_whitelist file,pipe -ss {max(0, offset):.3f}",
                options="-vn -loglevel error",
            )
            buffered = BufferedPCM(decoder, self.settings.audio_buffer_ms)
            source = TimedAudio(buffered, self.volume / 100)
            slot = PlaybackSlot(track, source, lease, asyncio.get_running_loop().create_future())
            self._slots.add(slot)
            await asyncio.to_thread(buffered.wait_ready)
            log.info(
                "Playback phase server=%s phase=decode elapsed_ms=%.1f standby=%s",
                self.guild.id,
                (time.perf_counter() - started) * 1000,
                not connect,
            )
            return slot
        except BaseException:
            if lease is None and lease_task.done() and not lease_task.cancelled():
                if lease_task.exception() is None:
                    lease = lease_task.result()
            if slot is not None:
                self._start_retirement(slot)
            elif lease is not None:
                self._retire_acquisition(lease_task)
            raise

    def _schedule_next_decode(self) -> None:
        if (
            self.closed
            or self._reason
            or self.shuffle
            or self.loop_mode == LoopMode.TRACK
            or not self.settings.prefetch_tracks
        ):
            return
        stream, active = self._stream, self._active_slot
        if stream is None or active is None or not self.queue:
            return
        target = self.queue[0]
        if self._next_slot is not None and self._next_slot.track is target:
            stream.set_next(self._next_slot.source, self._next_slot, expected=active)
            return
        if self._next_attempt is not None:
            owner, candidate = self._next_attempt
            if owner is active and candidate is target:
                return
        self._invalidate_next()
        self._next_attempt = (active, target)

        async def prepare() -> None:
            slot = None
            installed = False
            try:
                slot = await self._build_slot(target, connect=False, offset=target.start_seconds)
                self._sync_handoffs()
                if (
                    not self.closed
                    and self._stream is stream
                    and self._active_slot is active
                    and not self.shuffle
                    and self.loop_mode != LoopMode.TRACK
                    and self.queue
                    and self.queue[0] is target
                ):
                    slot.source.volume = self.volume / 100
                    installed = stream.set_next(slot.source, slot, expected=active)
                    if installed:
                        self._next_slot = slot
            except asyncio.CancelledError:
                raise
            except Exception:
                log.debug("Next-track decoding was unavailable for server %s", self.guild.id)
            finally:
                if slot is not None and not installed:
                    await asyncio.shield(self._start_retirement(slot))

        self._next_decode = asyncio.create_task(prepare(), name=f"next-audio-{self.guild.id}")

    async def _end_stream(self, stream: ContinuousPCM, *, preserve_next: bool = False) -> None:
        self._sync_handoffs()
        saved = self._next_slot if preserve_next else None
        if saved is not None:
            with stream.lock:
                stream.remove_next()
            if self._next_decode and not self._next_decode.done():
                self._next_decode.cancel()
            self._next_attempt = None
        else:
            self._invalidate_next()
        finished = self._stream_finished
        if self.voice and finished is not None and not finished.done():
            self.voice.stop()
        if not await asyncio.to_thread(stream.wait_cleanup):
            stream.cleanup()
        # The sender has relinquished these decoders. Retain their leases until
        # owned cleanup actually finishes, without delaying the next sender on
        # FFmpeg shutdown or cache maintenance.
        for slot in list(self._slots):
            if slot is not saved:
                self._start_retirement(slot)
        if self._stream is stream:
            self._stream = None
            self._stream_finished = None
            self._active_slot = None
            self._source = None
            self._finished = None

    async def _play(self, track: Track, generation: int) -> None:
        preparing_started = time.perf_counter()
        self._sync_handoffs()
        slot = self._active_slot
        stream = self._stream
        continued = bool(
            stream
            and slot
            and slot.track is track
            and self._stream_finished is not None
            and not self._stream_finished.done()
            and self.voice
            and (self.voice.is_playing() or self.voice.is_paused())
        )
        started = continued
        failure = None
        try:
            if not continued:
                if stream is not None:
                    await self._end_stream(stream)
                self.status = "loading"
                self.current_lyrics = None
                self.refresh(self)
                if self._next_slot is not None and self._next_slot.track is track:
                    slot, self._next_slot = self._next_slot, None
                    slot.source.volume = self.volume / 100
                    if not self.voice or not self.voice.is_connected():
                        self._preparing = asyncio.create_task(self._wait_for_connection())
                        await self._preparing
                        self._preparing = None
                else:
                    self._preparing = asyncio.create_task(
                        self._build_slot(track, connect=True, offset=self._offset)
                    )
                    slot = await self._preparing
                    self._preparing = None
                # Volume may have changed while this slot warmed up, before it
                # became the displayed/active source used by set_volume().
                slot.source.volume = self.volume / 100
                if self.closed or self._reason:
                    return
                voice = self.voice
                if voice is None or not voice.is_connected():
                    raise UserError("The voice connection was lost. Please try again.")
                loop = asyncio.get_running_loop()
                finished = loop.create_future()
                stream = ContinuousPCM(
                    slot.source, slot, lambda: loop.call_soon_threadsafe(self._sync_handoffs)
                )
                self._stream = stream
                self._stream_finished = finished
                self._active_slot = slot
                self._source = slot.source

                def complete(error: Exception | None) -> None:
                    def resolve() -> None:
                        if not finished.done():
                            finished.set_result(error)

                    loop.call_soon_threadsafe(resolve)

                bitrate = min(512, max(16, getattr(self.voice_channel, "bitrate", 96000) // 1000))
                voice.play(
                    stream,
                    after=complete,
                    application="audio",
                    bitrate=bitrate,
                    signal_type="music",
                    fec=True,
                    expected_packet_loss=0.15,
                )
                started = True
                if self.pause_reasons:
                    voice.pause()
                log.info(
                    "Playback phase server=%s phase=audio_started elapsed_ms=%.1f paused=%s "
                    "encoder_bitrate_kbps=%s",
                    self.guild.id,
                    (time.perf_counter() - preparing_started) * 1000,
                    self.paused,
                    bitrate,
                )
                if self._transition_started is not None:
                    log.info(
                        "Playback transition server=%s action=%s elapsed_ms=%.1f",
                        self.guild.id,
                        self._transition_reason,
                        (time.perf_counter() - self._transition_started) * 1000,
                    )
                    self._transition_started = None
                    self._transition_reason = ""
            self._finished = self._stream_finished
            self.status = "playing"
            self._schedule_lyrics(track, generation)
            self._schedule_prefetch()
            self._schedule_next_decode()
            self.refresh(self)
            self._request_checkpoint()
            await asyncio.wait(
                (slot.ended, self._stream_finished), return_when=asyncio.FIRST_COMPLETED
            )
            self._sync_handoffs()
            if self._stream_finished.done():
                failure = self._stream_finished.result()
                disconnected = self.voice is None or not self.voice.is_connected()
                if (
                    not failure
                    and not self._reason
                    and ("connection" in self.pause_reasons or disconnected)
                ):
                    await self.connection_lost()
                    failure = MediaError("Voice connection interrupted. Resuming the track.")
                if failure:
                    raise failure
        except asyncio.CancelledError:
            if self.closed or not self._reason:
                raise
        except Exception as error:
            failure = error
            log.warning(
                "Track failed for server %s: %s", self.guild.id, type(error).__name__, exc_info=True
            )
            if self._reason != "seek":
                self._queue_report(
                    str(error) if isinstance(error, UserError) else "That track could not play."
                )
        finally:
            self._sync_handoffs()
            position = self.position
            active = self._active_slot
            active_generation = self._generation
            still_streaming = bool(
                stream
                and slot
                and slot.ended.done()
                and slot.ended.result()
                and self._stream_finished is not None
                and not self._stream_finished.done()
                and not self.closed
                and not self._reason
            )
            self._preparing = None
            if still_streaming:
                self._start_retirement(slot)
            elif stream is not None:
                if not started:
                    stream.cleanup()
                await self._end_stream(
                    stream,
                    preserve_next=not failure
                    and not self.closed
                    and (
                        (not self._reason and slot is not None and not slot.ended.done())
                        or (
                            self._reason == "skip"
                            and self._next_slot is not None
                            and bool(self.queue)
                            and self.queue[0] is self._next_slot.track
                            and not self.shuffle
                        )
                    ),
                )
            elif slot is not None:
                self._start_retirement(slot)
            if not self.closed:
                if failure and active is not None and active is not slot:
                    await self._finish(active.track, active_generation, failure, position)
                elif slot is None or not slot.ended.done():
                    await self._finish(track, generation, failure, position)

    async def _finish(
        self, track: Track, generation: int, failure: Exception | None, position: float
    ) -> None:
        async with self._lock:
            if self.current is not track or generation != self._generation:
                return
            reason = self._reason or ("error" if failure else "finished")
            if reason == "seek":
                # seek() already committed the replacement position. Retain the
                # same track while the worker disposes the old sender/decoder.
                self._retries = 0
                return
            if reason == "error" and "connection" in self.pause_reasons:
                self._offset = position
                return
            if reason == "error" and position > self._offset and self._retries < 2:
                self._offset = position
                self._retries += 1
                return
            self._retries = 0
            if reason == "finished" and self.loop_mode == LoopMode.TRACK:
                # Repeating the same current track bypasses shuffle and preserves
                # a faithful checkpoint if shutdown occurs before playback restarts.
                self.current = track
                self._offset = track.start_seconds
            else:
                self.current = None
                self._offset = 0
                if reason != "previous" and (reason != "error" or position > track.start_seconds):
                    self.history.append(track)
                if reason in {"finished", "skip"} and self.loop_mode == LoopMode.QUEUE:
                    self.queue.append(track)
        self.refresh(self)
        self._request_checkpoint()

    async def _autoplay(self) -> bool:
        genre = self.autoplay
        if not genre or genre not in GENRES:
            return False
        search = asyncio.create_task(self.media.search(GENRES[genre], 15))
        self._autoplay_search = search
        try:
            results = await search
            recent = {track.url for track in self.history}
            candidates = [track for track in results if is_music(track) and track.url not in recent]
            async with self._lock:
                if self.closed:
                    return False
                if self.queue:
                    return True
                if self.autoplay != genre:
                    return False
                if candidates:
                    self.queue.append(
                        replace(random.choice(candidates), requester_id=self.owner_id)
                    )
                    return True
        except asyncio.CancelledError:
            task = asyncio.current_task()
            if self.closed or (task and task.cancelling()):
                raise
            # Manual queue/navigation or a genre change superseded this lookup.
            # Native provider work remains owned by MediaResolver until it settles.
            return bool(self.queue)
        except Exception:
            log.warning("Autoplay search failed for server %s", self.guild.id)
        finally:
            if self._autoplay_search is search:
                self._autoplay_search = None
        return False

    def _cancel_autoplay_search(self) -> None:
        if self._autoplay_search and not self._autoplay_search.done():
            self._autoplay_search.cancel()

    def _check_open(self) -> None:
        self._sync_handoffs()
        if self.closed:
            raise UserError("This playback session has ended. Use /play again.")

    def _check_navigation(self) -> None:
        self._check_open()
        if self._reason:
            raise UserError(
                "Wait for the current track to change before skipping or going back again."
            )

    def _interrupt(self, reason: str, *, keep: PlaybackSlot | None = None) -> None:
        self._invalidate_next(keep=keep)
        self._reason = reason
        self._transition_started = time.perf_counter()
        self._transition_reason = reason
        self._cancel_autoplay_search()
        if self._preparing and not self._preparing.done():
            self._preparing.cancel()
        if self.voice:
            self.voice.stop()
        self._wake.set()

    async def skip(self) -> Track:
        async with self._lock:
            with self._stream.lock if self._stream else nullcontext():
                self._check_navigation()
                if self.current is None:
                    raise UserError("There is no current track to skip.")
                track = self.current
                ready = self._next_slot
                if (
                    self.shuffle
                    or not self.queue
                    or ready is None
                    or ready.track is not self.queue[0]
                ):
                    ready = None
                self._interrupt("skip", keep=ready)
                # Commit accepted navigation before cleanup/checkpoint awaits.
                self.history.append(track)
                if self.loop_mode == LoopMode.QUEUE:
                    self.queue.append(track)
                self.current = None
                self._offset = 0
                self._retries = 0
                return track

    async def previous(self) -> Track:
        async with self._lock:
            with self._stream.lock if self._stream else nullcontext():
                self._check_navigation()
                if not self.history:
                    raise UserError("There is no previous track.")
                if len(self.queue) + bool(self.current) >= self.settings.max_queue:
                    raise UserError("The music queue is full. Skip a track before using Previous.")
                # Validation and source invalidation share the audio boundary lock.
                self._invalidate_next()
                if self.current:
                    self.queue.appendleft(self.current)
                selected = self.history.pop()
                self.queue.appendleft(selected)
                self._interrupt("previous")
                self.current = None
        self.start()
        self._schedule_prefetch()
        return selected

    async def seek(self, position: float, *, expected_track: Track | None = None) -> Track:
        async with self._lock:
            with self._stream.lock if self._stream else nullcontext():
                self._check_navigation()
                track = self.current
                if track is None:
                    raise UserError("There is no current track to seek.")
                if expected_track is not None and track is not expected_track:
                    raise UserError("The track changed. Open Seek again for the current song.")
                if (
                    self.status != "playing"
                    or not self.voice
                    or not (self.voice.is_playing() or self.voice.is_paused())
                ):
                    raise UserError("Wait for the current track to start before seeking.")
                if track.duration <= 0 or not math.isfinite(track.duration):
                    raise UserError("Seeking requires a track with a known duration.")
                try:
                    target = float(position)
                except TypeError, ValueError, OverflowError:
                    raise UserError("Enter a valid playback position.") from None
                if isinstance(position, bool) or not math.isfinite(target) or target < 0:
                    raise UserError("Enter a finite playback position of zero or greater.")
                if target >= track.duration:
                    raise UserError("Choose a position before the end of this track.")
                self._offset = target
                self._source = None
                self._retries = 0
                self._interrupt("seek")
                self.status = "loading"
        self.start()
        self.refresh(self)
        await self._persist_safely()
        return track

    async def _apply_pause(self, reason: str, enabled: bool) -> Track | None:
        async with self._lock:
            with self._stream.lock if self._stream else nullcontext():
                self._check_open()
                accepted_track = self.current
                if enabled:
                    self.pause_reasons.add(reason)
                else:
                    self.pause_reasons.discard(reason)
                voice = self.voice
                if voice:
                    if self.pause_reasons and voice.is_playing():
                        voice.pause()
                    elif not self.pause_reasons and voice.is_paused():
                        if self._source:
                            self._source.reset_clock()
                        voice.resume()
        return accepted_track

    async def pause(self, reason: str, enabled: bool) -> Track | None:
        accepted_track = await self._apply_pause(reason, enabled)
        self.refresh(self)
        await self._persist_safely()
        return accepted_track

    async def set_volume(self, value: int) -> None:
        if not 0 <= value <= 100:
            raise UserError("Volume must be an integer between 0 and 100.")
        async with self._lock:
            self._check_open()
            self.volume = value
            if self._source:
                self._source.volume = value / 100
            if self._next_slot:
                self._next_slot.source.volume = value / 100
        self.refresh(self)
        await self._persist_safely()

    async def set_loop(self, mode: LoopMode) -> None:
        async with self._lock:
            self._invalidate_next()
            self._check_open()
            self.loop_mode = mode
        self._schedule_next_decode()
        self.refresh(self)
        await self._persist_safely()

    async def set_autoplay(self, genre: str | None) -> None:
        if genre is not None and genre not in GENRES:
            raise UserError("Choose a supported autoplay genre.")
        async with self._lock:
            self._check_open()
            changed = self.autoplay != genre
            self.autoplay = genre
        if changed:
            self._cancel_autoplay_search()
        if genre:
            self._cancel_idle()
            self.start()
        self.refresh(self)
        await self._persist_safely()

    async def toggle_shuffle(self) -> None:
        async with self._lock:
            self._invalidate_next()
            self._check_open()
            self.shuffle = not self.shuffle
            if self.shuffle:
                shuffled = list(self.queue)
                random.shuffle(shuffled)
                self.queue = deque(shuffled)
        self._schedule_prefetch()
        self._schedule_next_decode()
        self.refresh(self)
        await self._persist_safely()

    def _schedule_lyrics(self, track: Track, generation: int) -> None:
        if self._lyrics_task:
            self._lyrics_task.cancel()

        async def load() -> None:
            try:
                lyrics = await self.lyrics_service.fetch(track.title, track.artist)
                if not self.closed and generation == self._generation and self.current is track:
                    self.current_lyrics = lyrics
                    self.refresh(self)
            except asyncio.CancelledError:
                pass
            except Exception:
                log.debug("Lyrics lookup failed for server %s", self.guild.id)

        self._lyrics_task = asyncio.create_task(load())

    def _schedule_prefetch(self) -> None:
        desired = {
            track.cache_key: track
            for track in list(self.queue)[: self.settings.prefetch_tracks]
            if not self.closed
        }
        for key, task in list(self._prefetch_tasks.items()):
            if key not in desired:
                self._prefetch_tasks.pop(key)
                task.cancel()

        async def preload(key: str, track: Track) -> None:
            try:
                await self.cache.prefetch(track)
            except asyncio.CancelledError:
                pass
            except Exception:
                log.debug("Prefetch failed for server %s", self.guild.id)
            finally:
                if self._prefetch_tasks.get(key) is asyncio.current_task():
                    self._prefetch_tasks.pop(key, None)

        for key, track in desired.items():
            if key not in self._prefetch_tasks:
                task = asyncio.create_task(preload(key, track))
                self._prefetch_tasks[key] = task
                self._prefetch_pending.add(task)
                task.add_done_callback(self._prefetch_pending.discard)

    async def listeners_changed(self, has_listeners: bool) -> None:
        if self.closed:
            return
        await self._apply_pause("alone", not has_listeners)
        self.refresh(self)
        # Listener-only pause reasons are not restored from checkpoints. Do not
        # make the first voice handshake wait on a durable database round trip.
        self._request_checkpoint()
        if has_listeners:
            if self._alone:
                self._alone.cancel()
                self._alone = None
        elif self._alone is None or self._alone.done():

            async def leave() -> None:
                await asyncio.sleep(self.settings.alone_timeout)
                if not any(not member.bot for member in self.voice_channel.members):
                    await self.expired(self)

            self._alone = asyncio.create_task(leave())

    async def connection_lost(self, grace_seconds: float = 45) -> None:
        """Let discord.py finish its reconnect without deleting the session underneath it."""
        if self.closed or (self._reconnect and not self._reconnect.done()):
            return

        async def recover() -> None:
            try:
                await self.pause("connection", True)
                async with asyncio.timeout(grace_seconds):
                    while not self.closed:
                        voice = self.voice
                        me = self.guild.me
                        if voice and voice.is_connected() and me and me.voice and me.voice.channel:
                            self.voice_channel = voice.channel
                            await self.pause("connection", False)
                            await self.listeners_changed(
                                any(not member.bot for member in self.voice_channel.members)
                            )
                            log.info("Voice session recovered in server %s", self.guild.id)
                            return
                        # The library owns the reconnect; observe its public state without
                        # starting a competing connection or requesting new gateway data.
                        await asyncio.sleep(0.25)  # noqa: ASYNC110
            except TimeoutError:
                log.warning("Voice recovery timed out in server %s", self.guild.id)
                await self.expired(self)

        self._reconnect = asyncio.create_task(recover(), name=f"voice-recovery-{self.guild.id}")

    def _cancel_idle(self) -> None:
        if self._idle:
            self._idle.cancel()
            self._idle = None

    def _schedule_idle(self) -> None:
        self._cancel_idle()

        async def leave() -> None:
            await asyncio.sleep(self.settings.idle_timeout)
            if self.current is None and not self.queue:
                await self.expired(self)

        self._idle = asyncio.create_task(leave())

    async def snapshot(self) -> dict[str, Any]:
        async with self._lock:
            with self._stream.lock if self._stream else nullcontext():
                self._sync_handoffs()
                return {
                    "guild_id": self.guild.id,
                    "voice_channel_id": self.voice_channel.id,
                    "text_channel_id": self.text_channel.id,
                    "message_id": self.message.id if self.message else None,
                    "current": self.current.to_dict() if self.current else None,
                    "queue": [track.to_dict() for track in self.queue],
                    "history": [track.to_dict() for track in self.history],
                    "position": self.position,
                    "volume": self.volume,
                    "loop": self.loop_mode.value,
                    "shuffle": self.shuffle,
                    "autoplay": self.autoplay,
                    "manual_pause": "manual" in self.pause_reasons,
                    "owner_id": self.owner_id,
                }

    def log_health(self) -> None:
        source = self._source
        if source is None or self.paused:
            return
        buffer = source.original
        latency = getattr(self.voice, "latency", 0) * 1000
        log.info(
            "Voice health server=%s websocket_ms=%.0f late_frames=%s max_gap_ms=%.1f "
            "decoder_underruns=%s buffered_frames=%s",
            self.guild.id,
            latency,
            source.late_frames,
            source.max_interval_ms,
            buffer.underruns,
            buffer.buffered_frames,
        )
        timing_stats = getattr(self.voice, "timing_stats", None)
        if callable(timing_stats):
            log.info("Voice transport server=%s %s", self.guild.id, timing_stats())

    async def _report_safely(self, message: str) -> None:
        try:
            async with asyncio.timeout(NOTIFICATION_TIMEOUT):
                await self.report(message)
        except Exception:
            log.exception("Playback notification failed for server %s", self.guild.id)

    def _queue_report(self, message: str) -> None:
        """Keep Discord text delivery out of the audio transition path."""
        if self.closed:
            return
        now = asyncio.get_running_loop().time()
        if message == self._notification_active or message in self._notifications:
            return
        if now - self._notification_recent.get(message, float("-inf")) < NOTIFICATION_DEDUP_SECONDS:
            return
        if len(self._notifications) >= NOTIFICATION_QUEUE_LIMIT:
            self._notification_overflow += 1
            return
        self._notification_recent[message] = now
        while len(self._notification_recent) > 32:
            self._notification_recent.pop(next(iter(self._notification_recent)))
        self._notifications.append(message)
        if self._notification_task is None or self._notification_task.done():
            self._notification_task = asyncio.create_task(
                self._deliver_notifications(), name=f"playback-notices-{self.guild.id}"
            )

    async def _deliver_notifications(self) -> None:
        try:
            while not self.closed and (self._notifications or self._notification_overflow):
                if self._notifications:
                    message = self._notifications.popleft()
                else:
                    count, self._notification_overflow = self._notification_overflow, 0
                    message = (
                        f"Grouped {count} additional playback errors "
                        "to avoid flooding this channel. "
                        "Check the bot logs for details."
                    )
                self._notification_active = message
                delay = (
                    self._notification_last_sent
                    + NOTIFICATION_INTERVAL
                    - asyncio.get_running_loop().time()
                )
                if delay > 0:
                    await asyncio.sleep(delay)
                await self._report_safely(message)
                self._notification_last_sent = asyncio.get_running_loop().time()
        finally:
            self._notification_active = None

    async def _persist_safely(self) -> None:
        try:
            await self.persist()
        except Exception:
            log.exception("Could not save playback state for server %s", self.guild.id)

    def _request_checkpoint(self) -> None:
        if self.closed:
            return
        self._checkpoint_dirty = True
        if self._checkpoint_task is None or self._checkpoint_task.done():
            self._checkpoint_task = asyncio.create_task(
                self._flush_checkpoints(), name=f"playback-checkpoint-{self.guild.id}"
            )

    async def _flush_checkpoints(self) -> None:
        # At most one writer plus one coalesced request. persist() takes the
        # same ordering lock used by durable controls and shutdown snapshots.
        while self._checkpoint_dirty and not self.closed:
            self._checkpoint_dirty = False
            await self._persist_safely()

    async def persist(self) -> None:
        async with self._save_lock:
            if self.closed:
                return
            snapshot = await self.snapshot()
            if snapshot["current"] or snapshot["queue"]:
                await self.store.save(self.guild.id, snapshot)
            else:
                await self.store.delete(self.guild.id)

    async def restore(self, data: dict[str, Any]) -> None:
        # Parse the full snapshot first: a malformed later field must not leave
        # half-restored state behind or start a worker for an invalid session.
        current_data = data.get("current") or data.get("currentTrack")
        current = Track.from_dict(current_data) if current_data else None
        queue = deque(Track.from_dict(track) for track in data.get("queue", []))
        history = deque(
            (
                Track.from_dict(track)
                for track in data.get("history", data.get("previousTracks", []))
            ),
            maxlen=10,
        )
        volume = max(0, min(100, int(data.get("volume", self.volume))))
        mode = data.get("loop") or "off"
        loop_mode = LoopMode(mode) if mode in LoopMode else LoopMode.OFF
        autoplay = data.get("autoplay") if data.get("autoplay") in GENRES else None
        owner = data.get("owner_id") or data.get("requesterId")
        owner_id = int(owner) if owner is not None else None
        manual_pause = bool(data.get("manual_pause") or "manual" in data.get("pauseReasons", []))
        position = (
            float(data["position"])
            if "position" in data
            else float(data.get("playbackPositionMs", 0)) / 1000
        )
        if not math.isfinite(position):
            raise ValueError("Saved playback position must be finite.")
        offset = max(0, position)
        if current and current.duration and offset >= current.duration - 1:
            offset = current.start_seconds
        capacity = self.settings.max_queue - bool(current)
        if len(queue) > capacity:
            log.warning(
                "Saved queue exceeds configured limit in server %s; keeping %s tracks",
                self.guild.id,
                self.settings.max_queue,
            )
            queue = deque(list(queue)[:capacity])
        async with self._lock:
            if self.closed:
                raise UserError("This playback session has ended.")
            self.current = current
            self.queue = queue
            self.history = history
            self.volume = volume
            self.loop_mode = loop_mode
            self.shuffle = bool(data.get("shuffle"))
            self.autoplay = autoplay
            self.owner_id = owner_id
            if manual_pause:
                self.pause_reasons.add("manual")
            self._offset = offset
        self.start()

    async def close(
        self, preserve: bool = False, *, guard: Callable[[], Any] | None = None
    ) -> None:
        async with self._close_lock:
            await self._close(preserve, guard=guard)

    async def _close(self, preserve: bool, *, guard: Callable[[], Any] | None = None) -> None:
        if guard is not None:
            guard()
        if self.closed:
            return
        snapshot_failed = False
        async with self._save_lock:
            if guard is not None:
                guard()
            try:
                snapshot = await self.snapshot() if preserve else None
            except Exception:
                # Keep the previous checkpoint when a fresh snapshot cannot be
                # built, but always stop owned tasks and leave voice.
                log.exception("Could not snapshot closing player for server %s", self.guild.id)
                snapshot = None
                snapshot_failed = True
            # Snapshot acquisition can itself wait for the player state lock.
            # Revalidate once more immediately before the irreversible state change.
            if guard is not None:
                guard()
            self.closed = True
            self.status = "ended"
            self._reason = "stop"
            self._generation += 1
            self._checkpoint_dirty = False
        self._invalidate_next()
        tasks = [
            self._worker,
            self._preparing,
            self._connecting,
            self._checkpoint_task,
            self._next_decode,
            self._autoplay_search,
            *self._prefetch_pending,
            self._lyrics_task,
            self._alone,
            self._idle,
            self._reconnect,
            self._notification_task,
        ]
        current_task = asyncio.current_task()
        for task in tasks:
            if task and task is not current_task:
                task.cancel()
        if self.voice:
            try:
                self.voice.stop()
            except Exception:
                log.exception("Could not stop audio for server %s", self.guild.id)
        await asyncio.gather(
            *(task for task in tasks if task and task is not current_task), return_exceptions=True
        )
        for slot in list(self._slots):
            self._start_retirement(slot)
        while self._retiring:
            await asyncio.gather(*list(self._retiring), return_exceptions=True)
        self._notifications.clear()
        self._notification_recent.clear()
        self._notification_overflow = 0
        try:
            if snapshot_failed:
                pass
            elif snapshot and (snapshot["current"] or snapshot["queue"]):
                await self.store.save(self.guild.id, snapshot)
            else:
                await self.store.delete(self.guild.id)
        finally:
            try:
                if self.voice:
                    await self.voice.disconnect(force=True)
            finally:
                self.current = None
                self.queue.clear()
                self.current_lyrics = None
                self.refresh(self)
