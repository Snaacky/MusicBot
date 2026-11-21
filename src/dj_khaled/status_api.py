"""Optional local HTTP diagnostics for the bot's YouTube session."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from aiohttp import web

from .models import UserError

if TYPE_CHECKING:
    from .media import MediaResolver

_CHECK_TIMEOUT = 20


class YouTubeStatusServer:
    def __init__(self, media: MediaResolver) -> None:
        self.media = media
        self._runner: web.AppRunner | None = None

    def application(self) -> web.Application:
        application = web.Application(client_max_size=1024)
        application.router.add_get("/youtube/status", self._status, allow_head=False)
        return application

    async def _status(self, _request: web.Request) -> web.Response:
        try:
            async with asyncio.timeout(_CHECK_TIMEOUT):
                result = await self.media.youtube_status()
        except TimeoutError:
            result = {
                "configured": self.media.youtube_auth_configured,
                "logged_in": None,
                "status": "unavailable",
                "message": "YouTube login status could not be checked in time.",
            }
        except Exception:
            result = {
                "configured": self.media.youtube_auth_configured,
                "logged_in": None,
                "status": "unavailable",
                "message": "YouTube login status is temporarily unavailable. Please try again.",
            }
        return web.json_response(
            result,
            status=503 if result["logged_in"] is None else 200,
            headers={"Cache-Control": "no-store"},
        )

    async def start(self, host: str, port: int) -> None:
        runner = web.AppRunner(self.application(), access_log=None, shutdown_timeout=5)
        self._runner = runner
        try:
            await runner.setup()
            await web.TCPSite(runner, host, port).start()
        except OSError:
            await self.close()
            raise UserError(
                "The YouTube status endpoint could not start. Check YOUTUBE_STATUS_HOST "
                "and choose an available YOUTUBE_STATUS_PORT."
            ) from None
        except BaseException:
            await self.close()
            raise

    async def close(self) -> None:
        runner, self._runner = self._runner, None
        if runner is not None:
            await runner.cleanup()
