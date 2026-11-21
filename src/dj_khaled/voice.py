"""Pace music packets at the transport boundary, including after reconnects."""

from __future__ import annotations

import threading
import time
from typing import Any

import discord

from .audio import FramePacer

OPUS_SILENCE = b"\xf8\xff\xfe"


class PacedVoiceClient(discord.VoiceClient):
    def __init__(self, client: discord.Client, channel: discord.abc.Connectable) -> None:
        super().__init__(client, channel)
        self._audio_pacer = FramePacer()
        self._timing_lock = threading.Lock()
        self._last_sent: float | None = None
        self._packets = 0
        self._late_packets = 0
        self._max_gap = 0.0
        self._max_send_duration = 0.0

    def play(self, source: discord.AudioSource, **options: Any) -> None:
        self._audio_pacer.reset()
        with self._timing_lock:
            self._last_sent = None
        super().play(source, **options)

    def resume(self) -> None:
        self._audio_pacer.reset()
        with self._timing_lock:
            self._last_sent = None
        super().resume()

    def timing_stats(self) -> dict[str, float | int]:
        with self._timing_lock:
            return {
                "packets": self._packets,
                "late_packets": self._late_packets,
                "max_gap_ms": round(self._max_gap * 1000, 2),
                "max_send_ms": round(self._max_send_duration * 1000, 2),
            }

    def send_audio_packet(self, data: bytes, *, encode: bool = True) -> None:
        # Preserve Discord's own short terminal silence sequence.
        music = encode or data != OPUS_SILENCE
        if music:
            self._audio_pacer.wait()
        started = time.perf_counter()
        super().send_audio_packet(data, encode=encode)
        if music:
            finished = time.perf_counter()
            if hasattr(self, "_timing_lock"):
                with self._timing_lock:
                    if self._last_sent is not None:
                        gap = finished - self._last_sent
                        self._max_gap = max(self._max_gap, gap)
                        self._late_packets += gap > 0.03
                    self._last_sent = finished
                    self._packets += 1
                    self._max_send_duration = max(self._max_send_duration, finished - started)
            self._audio_pacer.sent()
