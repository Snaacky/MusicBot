"""Keep the final PCM samples which discord.py's fixed-frame decoder drops."""

from __future__ import annotations

import subprocess
from typing import Any

import discord

from .models import MediaError

_SDK_FFMPEG_PCM = discord.FFmpegPCMAudio
_FRAME_BYTES = 3840


class TailPreservingPCM(discord.AudioSource):
    """Leave frame assembly to the continuous stream, including partial tails."""

    def __init__(self, original: discord.FFmpegPCMAudio) -> None:
        self.original = original
        self._error: Exception | None = None

    def read(self) -> bytes:
        # discord.py 2.x's read() discards anything shorter than a 20 ms frame.
        # Its stdout pipe contains those decoded samples, so read it directly.
        # ContinuousPCM joins this tail to the following track before encoding.
        data = self.original._stdout.read(_FRAME_BYTES)
        if len(data) < _FRAME_BYTES:
            process = getattr(self.original, "_process", None)
            if process and not getattr(self.original, "_stopped", False):
                # EOF can precede process exit. This runs on the decoder thread,
                # so await its exit briefly before trusting the SDK's error poll.
                try:
                    process.wait(timeout=1)
                except subprocess.TimeoutExpired:
                    self._error = MediaError(
                        "The audio decoder did not finish. Retrying the track."
                    )
            self.original._check_process_returncode()
        return data

    @property
    def _current_error(self) -> Exception | None:
        return self._error or self.original._current_error

    def cleanup(self) -> None:
        self.original.cleanup()


def create_decoder(*args: Any, **kwargs: Any) -> discord.AudioSource:
    source = discord.FFmpegPCMAudio(*args, **kwargs)
    # Keep the runtime factory replaceable by simple test/dummy AudioSources.
    if isinstance(source, _SDK_FFMPEG_PCM):
        return TailPreservingPCM(source)
    return source
