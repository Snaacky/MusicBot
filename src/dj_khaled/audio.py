"""Decode ahead so the voice sender never waits on FFmpeg's pipe."""

from __future__ import annotations

import queue
import threading
import time
from collections.abc import Callable

import discord

from .models import MediaError

PCM_FRAME_BYTES = 3840  # 20 ms, 48 kHz, stereo, signed 16-bit PCM.
SILENCE = bytes(PCM_FRAME_BYTES)


class FramePacer:
    """Resume the audio clock after a stall instead of sending a catch-up burst."""

    def __init__(
        self,
        clock: Callable[[], float] = time.perf_counter,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._clock = clock
        self._sleep = sleep
        self._deadline: float | None = None
        self._lock = threading.Lock()
        self._generation = 0

    def reset(self) -> None:
        with self._lock:
            self._generation += 1
            self._deadline = None

    def wait(self) -> float:
        with self._lock:
            now = self._clock()
            # Discard timing debt after a stall without accumulating ordinary jitter.
            deadline = self._deadline
            if deadline is None or now - deadline > 0.005:
                deadline = now
            generation = self._generation
        delay = deadline - now
        if delay > 0:
            self._sleep(delay)
        sent_at = self._clock()
        with self._lock:
            if generation == self._generation:
                self._deadline = deadline + 0.02
            elif self._deadline is None:
                # A resume/reset may arrive while this audio thread is sleeping.
                self._deadline = sent_at + 0.02
        return sent_at

    def sent(self) -> None:
        """Also account for stalls inside encoding or the socket send itself."""
        with self._lock:
            now = self._clock()
            if self._deadline is not None and now - self._deadline > 0.005:
                self._deadline = now + 0.02


class BufferedPCM(discord.AudioSource):
    def __init__(self, source: discord.AudioSource, buffer_ms: int = 1000) -> None:
        self.original = source
        self.capacity = max(1, buffer_ms // 20)
        self._frames: queue.Queue[bytes] = queue.Queue(self.capacity)
        self._stop = threading.Event()
        self._done = threading.Event()
        self._ready = threading.Event()
        self._cleanup_lock = threading.Lock()
        self._frame_lock = threading.Lock()
        self._cleaned = False
        self._error: Exception | None = None
        self._starved_since: float | None = None
        self.last_read_underrun = False
        self.underruns = 0
        self._reader = threading.Thread(target=self._decode, name="dj-audio-buffer", daemon=True)
        self._reader.start()

    @property
    def buffered_frames(self) -> int:
        return self._frames.qsize()

    def _decode(self) -> None:
        try:
            while not self._stop.is_set():
                frame = self.original.read()
                if not frame:
                    error = getattr(self.original, "_current_error", None)
                    if error:
                        raise error
                    return
                if len(frame) > PCM_FRAME_BYTES or len(frame) % 4:
                    raise MediaError("The decoder returned an incomplete audio frame.")
                if len(frame) < PCM_FRAME_BYTES:
                    # Buffered pipe reads are short only at EOF. Publish this
                    # terminal tail and EOF together so a consumer cannot insert
                    # underrun silence between the tail and a prepared next song.
                    terminal_error = None
                    try:
                        extra = self.original.read()
                        if extra:
                            terminal_error = MediaError(
                                "The decoder returned a non-terminal short frame."
                            )
                        else:
                            terminal_error = getattr(self.original, "_current_error", None)
                    except Exception as error:
                        terminal_error = error
                    while not self._stop.is_set():
                        with self._frame_lock:
                            try:
                                self._frames.put_nowait(frame)
                            except queue.Full:
                                pass
                            else:
                                self._error = terminal_error
                                self._done.set()
                                self._ready.set()
                                return
                        self._stop.wait(0.01)
                    return
                while not self._stop.is_set():
                    try:
                        self._frames.put(frame, timeout=0.05)
                        if self._frames.full():
                            self._ready.set()
                        break
                    except queue.Full:
                        continue
        except Exception as error:
            if not self._stop.is_set():
                self._error = error
        finally:
            self._done.set()
            self._ready.set()

    def wait_ready(self, timeout: float = 10) -> None:
        if not self._ready.wait(timeout):
            raise MediaError("Audio decoding took too long to start.")
        if self._stop.is_set():
            raise MediaError("Playback request cancelled.")
        if self._error and self._frames.empty():
            raise self._error

    def read(self) -> bytes:
        with self._frame_lock:
            return self._read_frame()

    def _read_frame(self) -> bytes:
        self.last_read_underrun = False
        if self._stop.is_set():
            return b""
        try:
            frame = self._frames.get_nowait()
        except queue.Empty:
            if self._done.is_set():
                # The producer may have filled the queue just before signalling EOF.
                try:
                    return self._frames.get_nowait()
                except queue.Empty:
                    if self._error:
                        raise self._error from None
                    return b""
            now = time.monotonic()
            if self._starved_since is None:
                self._starved_since = now
            if now - self._starved_since > 5:
                raise MediaError("Audio decoding stalled. Retrying the track.") from None
            self.underruns += 1
            self.last_read_underrun = True
            # Keep the RTP cadence instead of blocking then sending a burst of old frames.
            return SILENCE
        self._starved_since = None
        return frame

    def cleanup(self) -> None:
        if not hasattr(self, "_cleanup_lock"):
            return
        with self._cleanup_lock:
            if self._cleaned:
                return
            self._cleaned = True
            self._stop.set()
            self._ready.set()
            try:
                self.original.cleanup()
            finally:
                if threading.current_thread() is not self._reader:
                    self._reader.join(timeout=1)


class ContinuousPCM(discord.AudioSource):
    """Frame buffered PCM across ready track boundaries without ending the sender.

    Sources and their file leases remain caller-owned. This class only switches
    ready sources and records boundaries; cleanup never blocks the audio thread.
    """

    def __init__(
        self,
        source: discord.AudioSource,
        token: object = None,
        notify: Callable[[], None] | None = None,
    ) -> None:
        self.lock = threading.RLock()
        self._current = source
        self._token = token
        self._next: tuple[discord.AudioSource, object] | None = None
        self._boundaries: list[tuple[object, object]] = []
        self._finished: list[object] = []
        self._notify = notify
        self._tail = bytearray()
        self._terminal = False
        self._finished_notified = False
        self._pending_error: Exception | None = None
        self._closed = threading.Event()

    @property
    def current_token(self) -> object:
        with self.lock:
            return self._token

    def set_next(
        self, source: discord.AudioSource, token: object, *, expected: object = None
    ) -> bool:
        with self.lock:
            if self._terminal or self._closed.is_set():
                return False
            if expected is not None and self._token is not expected:
                return False
            self._next = (source, token)
            return True

    def remove_next(self) -> object:
        with self.lock:
            if self._next is None:
                return None
            _source, token = self._next
            self._next = None
            return token

    def take_boundaries(self) -> list[tuple[object, object]]:
        with self.lock:
            boundaries, self._boundaries = self._boundaries, []
            return boundaries

    def take_finished(self) -> list[object]:
        with self.lock:
            finished, self._finished = self._finished, []
            return finished

    def read(self) -> bytes:
        with self.lock:
            if self._closed.is_set():
                return b""
            if self._pending_error is not None and not self._tail:
                raise self._pending_error
            while len(self._tail) < PCM_FRAME_BYTES and not self._terminal:
                try:
                    frame = self._current.read()
                except Exception as error:
                    if not self._tail:
                        raise
                    self._pending_error = error
                    self._terminal = True
                    break
                if frame:
                    if len(frame) > PCM_FRAME_BYTES or len(frame) % 4:
                        raise MediaError("The decoder returned an incomplete audio frame.")
                    self._tail.extend(frame)
                    continue
                if self._next is None:
                    self._terminal = True
                    break
                previous = self._token
                self._current, self._token = self._next
                self._next = None
                self._boundaries.append((previous, self._token))
                if self._notify is not None:
                    self._notify()
            if not self._tail:
                if self._terminal and self._pending_error is None and not self._finished_notified:
                    self._finished_notified = True
                    self._finished.append(self._token)
                    if self._notify is not None:
                        self._notify()
                return b""
            frame = bytes(self._tail[:PCM_FRAME_BYTES])
            del self._tail[:PCM_FRAME_BYTES]
            return frame.ljust(PCM_FRAME_BYTES, b"\0")

    @property
    def frames(self) -> float:
        return getattr(self._current, "frames", 0)

    @frames.setter
    def frames(self, value: float) -> None:
        self._current.frames = value

    @property
    def volume(self) -> float:
        return getattr(self._current, "volume", 1.0)

    @volume.setter
    def volume(self, value: float) -> None:
        with self.lock:
            self._current.volume = value
            if self._next:
                self._next[0].volume = value

    def wait_cleanup(self, timeout: float = 5) -> bool:
        return self._closed.wait(timeout)

    def cleanup(self) -> None:
        if hasattr(self, "_closed"):
            self._closed.set()
