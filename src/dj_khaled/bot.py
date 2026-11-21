from __future__ import annotations

import asyncio
import logging
import math
from collections.abc import Awaitable, Callable
from typing import Any
from weakref import WeakValueDictionary

import aiohttp
import discord
from discord import app_commands

from .cache import AudioCache
from .commands import register_commands
from .config import Settings
from .i18n import Languages
from .icons import load_control_icons
from .lyrics import LyricsService
from .media import MediaResolver, PublicResolver
from .models import Track, UserError
from .player import GuildPlayer
from .status_api import YouTubeStatusServer
from .storage import StateStore
from .ui import Controls, now_playing

log = logging.getLogger(__name__)
PROGRESS_INTERVAL = 1.0
UI_REFRESH_INTERVAL = 1.0
HTTP_RETRY_INTERVAL = 5.0
UI_SLOW_REQUEST_THRESHOLD = 1.0
UI_ADAPTIVE_DURATION = 30.0
UI_ADAPTIVE_MIN_INTERVAL = 2.0
UI_ADAPTIVE_MAX_INTERVAL = 5.0
UI_REST_PROGRESS_MULTIPLIER = 2.0
COMPONENT_ACK_DEADLINE = 2.0


class DJKhaled(discord.AutoShardedClient):
    def __init__(self, settings: Settings) -> None:
        intents = discord.Intents.none()
        intents.guilds = True
        intents.voice_states = True
        super().__init__(
            intents=intents,
            shard_count=settings.shard_count,
            application_id=settings.client_id,
            activity=discord.Activity(
                type=discord.ActivityType.listening, name=f"{settings.app_name} | /play"
            ),
            allowed_mentions=discord.AllowedMentions.none(),
        )
        self.settings = settings
        self.tree = app_commands.CommandTree(self)
        self.store = StateStore(settings.data_directory / "state.sqlite3")
        self.languages = Languages(self.store)
        self.control_icons: dict[str, discord.PartialEmoji] = {}
        self.players: dict[int, GuildPlayer] = {}
        self._guild_locks: WeakValueDictionary[int, asyncio.Lock] = WeakValueDictionary()
        self._refresh_tasks: dict[str, asyncio.Task[None]] = {}
        self._refresh_pending: set[str] = set()
        self._refresh_after: dict[str, float] = {}
        self._refresh_backoff: dict[str, float] = {}
        self._refresh_urgent: set[str] = set()
        self._refresh_wake: dict[str, asyncio.Event] = {}
        self._refresh_interval: dict[str, float] = {}
        self._refresh_slow_until: dict[str, float] = {}
        self._component_interactions: dict[str, discord.Interaction] = {}
        self._component_tasks: dict[asyncio.Task[bool], str] = {}
        self._refresh_failures: dict[str, int] = {}
        self._control_revision: dict[str, int] = {}
        self._rendered: dict[str, tuple[Any, ...]] = {}
        self._views: dict[str, Controls] = {}
        self._heartbeat: asyncio.Task[None] | None = None
        self._progress_task: asyncio.Task[None] | None = None
        self._restored = False
        self._restore_lock = asyncio.Lock()
        self._closing = False
        self._close_lock = asyncio.Lock()
        self.web: aiohttp.ClientSession | None = None
        self.youtube_status: YouTubeStatusServer | None = None
        self.media: MediaResolver
        self.cache: AudioCache
        self.lyrics: LyricsService
        register_commands(self)
        self.tree.on_error = self._command_error

    async def setup_hook(self) -> None:
        await load_control_icons(self)
        await self.store.initialize()
        await self.store.import_legacy(self.settings.root / "database")
        self.web = aiohttp.ClientSession(
            connector=aiohttp.TCPConnector(resolver=PublicResolver(), ttl_dns_cache=60, limit=16),
            timeout=aiohttp.ClientTimeout(total=120, connect=15, sock_read=30),
            headers={"User-Agent": "DJ-Khaled/1.0 (Discord music bot)"},
        )
        self.media = MediaResolver(
            self.settings, self.web, on_youtube_status=self._youtube_status_updated
        )
        if self.settings.youtube_status_port:
            self.youtube_status = YouTubeStatusServer(self.media)
            await self.youtube_status.start(
                self.settings.youtube_status_host, self.settings.youtube_status_port
            )
            log.info("YouTube status endpoint is available at /youtube/status")
        self.cache = AudioCache(
            self.settings.data_directory / "audio", self.media, self.settings.cache_max_bytes
        )
        await self.cache.cleanup_stale()
        self.lyrics = LyricsService(self.web, self.settings.genius_token)
        if self.settings.guild_ids:
            for guild_id in self.settings.guild_ids:
                guild = discord.Object(id=guild_id)
                self.tree.copy_global_to(guild=guild)
                synced = await self.tree.sync(guild=guild)
                log.info("Registered %s commands in server %s", len(synced), guild_id)
        else:
            synced = await self.tree.sync()
            log.info("Registered %s global commands", len(synced))
        if self.settings.sync_bot_name and self.user and self.user.name != self.settings.app_name:
            try:
                await self.user.edit(username=self.settings.app_name)
            except discord.HTTPException:
                log.warning(
                    "Discord username could not be updated; app branding remains %s",
                    self.settings.app_name,
                )
        self._heartbeat = asyncio.create_task(self._checkpoints(), name="session-checkpoints")
        self._progress_task = asyncio.create_task(self._progress_updates(), name="player-progress")

    def _youtube_status_updated(self) -> None:
        if self._closing:
            return
        for player in self.players.values():
            if (
                not player.closed
                and player.current
                and player.current.platform in {"youtube", "spotify"}
            ):
                self.request_refresh(player, urgent=True)

    async def on_ready(self) -> None:
        log.info("%s is online in %s server(s)", self.settings.app_name, len(self.guilds))
        async with self._restore_lock:
            if not self._restored and not self._closing:
                await self._restore_sessions()
                self._restored = True

    async def _restore_sessions(self) -> None:
        for guild_id, snapshot in (await self.store.sessions()).items():
            if self._closing:
                return
            # A missing local guild can belong to another shard/installation; never delete it.
            guild = self.get_guild(guild_id)
            if guild is None or guild_id in self.players:
                continue
            try:
                if not isinstance(snapshot, dict):
                    raise TypeError("Saved session must be an object.")
                voice_id = self._saved_id(snapshot, "voice_channel_id", "voiceChannelId")
                text_id = self._saved_id(snapshot, "text_channel_id", "textChannelId")
                voice = guild.get_channel(voice_id) if voice_id else None
                text = guild.get_channel(text_id) if text_id else None
                if not isinstance(voice, discord.VoiceChannel) or not hasattr(text, "send"):
                    log.warning(
                        "Saved session has unavailable voice/text channels in server %s", guild_id
                    )
                    continue
                try:
                    message_id = self._saved_id(snapshot, "message_id", "nowPlayingMessageId")
                except TypeError, ValueError:
                    log.warning("Ignoring malformed saved player message ID in server %s", guild_id)
                    message_id = None
                async with self._guild_lock(guild_id):
                    if self._closing:
                        return
                    if guild_id in self.players:
                        continue
                    player = self._new_player(guild, voice, text)
                    if message_id:
                        try:
                            player.message = await text.fetch_message(message_id)
                        except discord.HTTPException:
                            pass
                    if self._closing:
                        return
                    # restore validates before starting its worker. Do not publish an
                    # incomplete player when this particular saved session is invalid.
                    await player.restore(snapshot)
                    self.players[guild_id] = player
            except Exception:
                log.warning(
                    "Saved session could not be restored in server %s", guild_id, exc_info=True
                )

    @staticmethod
    def _saved_id(snapshot: dict[str, Any], *names: str) -> int | None:
        value = next((snapshot[name] for name in names if snapshot.get(name)), None)
        if value is None:
            return None
        if isinstance(value, bool) or not isinstance(value, (int, str)):
            raise ValueError("A saved Discord ID must be a positive integer.")
        result = int(value)
        if result <= 0:
            raise ValueError("A saved Discord ID must be a positive integer.")
        return result

    def _guild_lock(self, guild_id: int) -> asyncio.Lock:
        lock = self._guild_locks.get(guild_id)
        if lock is None:
            lock = asyncio.Lock()
            self._guild_locks[guild_id] = lock
        return lock

    def validate_voice(self, interaction: discord.Interaction) -> discord.Member:
        member = interaction.user
        guild = interaction.guild
        if guild is None or not isinstance(member, discord.Member):
            raise UserError("Use this command inside a server.")
        if not member.voice or not member.voice.channel:
            raise UserError("Join a voice channel first.")
        if isinstance(member.voice.channel, discord.StageChannel):
            raise UserError("Join a regular voice channel. Stage channels are not supported.")
        if guild.me is None:
            raise UserError("The bot is still joining this server. Try again shortly.")
        permissions = member.voice.channel.permissions_for(guild.me)
        if not permissions.connect or not permissions.speak:
            raise UserError("I need Connect and Speak permissions in your voice channel.")
        player = self.players.get(guild.id)
        channel = (
            guild.voice_client.channel
            if guild.voice_client
            else (player.voice_channel if player else None)
        )
        if channel and channel.id != member.voice.channel.id:
            raise UserError("Join the same voice channel as the bot.")
        return member

    def authorize(
        self, interaction: discord.Interaction, player: GuildPlayer, *, control: bool
    ) -> discord.Member:
        if player.closed or self.players.get(player.guild.id) is not player:
            raise UserError("This playback session has ended. Use /play to create a new one.")
        member = self.validate_voice(interaction)
        if interaction.guild_id != player.guild.id:
            raise UserError("This control belongs to another server.")
        if control:
            requester = player.current.requester_id if player.current else player.owner_id
            role_permission = any(
                role.id in self.settings.dj_roles
                if self.settings.dj_roles
                else role.name.casefold() == "dj"
                for role in member.roles
            )
            if not (
                member.guild_permissions.manage_guild or role_permission or member.id == requester
            ):
                raise UserError(
                    "Only the requester, a DJ, or someone with Manage Server can control playback."
                )
        return member

    def _new_player(self, guild: discord.Guild, voice: Any, text: Any) -> GuildPlayer:
        async def report(message: str) -> None:
            player.note(message)

        player = GuildPlayer(
            guild,
            voice,
            text,
            self.settings,
            self.cache,
            self.media,
            self.store,
            self.lyrics,
            lambda current: self.request_refresh(current, urgent=True),
            report,
            self.remove_player,
        )
        return player

    async def enqueue(
        self, interaction: discord.Interaction, tracks: list[Track]
    ) -> tuple[GuildPlayer, int]:
        if self._closing:
            raise UserError("The bot is shutting down. Please try again shortly.")
        member = self.validate_voice(interaction)
        async with self._guild_lock(interaction.guild_id):
            if self._closing:
                raise UserError("The bot is shutting down. Please try again shortly.")
            member = self.validate_voice(interaction)
            player = self.players.get(interaction.guild_id)
            if player is None:
                if not tracks:
                    raise UserError("No playable tracks were found.")
                player = self._new_player(
                    interaction.guild, member.voice.channel, interaction.channel
                )
                self.players[interaction.guild_id] = player
            count = await player.enqueue(tracks, member.id)
            return player, count

    async def remove_player(
        self,
        player: GuildPlayer,
        *,
        guard: Callable[[], Any] | None = None,
        notice: str = "Disconnected. Queue cleared.",
    ) -> None:
        async with self._guild_lock(player.guild.id):
            if guard is not None:
                guard()
            if self.players.get(player.guild.id) is not player:
                return
            try:
                if guard is None:
                    await player.close()
                else:
                    await player.close(guard=guard)
            finally:
                # A late authorization rejection can happen after waiting on the
                # player's save/close locks. Keep that still-open session usable.
                if player.closed:
                    player.note(notice)
                    task = self._refresh_tasks.pop(player.session_id, None)
                    if task and task is not asyncio.current_task():
                        task.cancel()
                        await asyncio.gather(task, return_exceptions=True)
                    await asyncio.gather(
                        *(
                            asyncio.shield(task)
                            for task, session_id in list(self._component_tasks.items())
                            if session_id == player.session_id
                            and task is not asyncio.current_task()
                        ),
                        return_exceptions=True,
                    )
                    self._refresh_pending.discard(player.session_id)
                    if view := self._views.pop(player.session_id, None):
                        view.stop()
                    try:
                        await self.publish(player, urgent=True)
                    finally:
                        self.players.pop(player.guild.id, None)
                        for mapping in (
                            self._refresh_after,
                            self._refresh_backoff,
                            self._refresh_wake,
                            self._refresh_interval,
                            self._refresh_slow_until,
                            self._component_interactions,
                            self._refresh_failures,
                            self._control_revision,
                            self._rendered,
                        ):
                            mapping.pop(player.session_id, None)
                        self._refresh_urgent.discard(player.session_id)

    def _signal_urgent_refresh(self, session_id: str) -> None:
        self._refresh_urgent.add(session_id)
        self._refresh_wake.setdefault(session_id, asyncio.Event()).set()

    def request_refresh(
        self, player: GuildPlayer, *, controls: bool = False, urgent: bool = False
    ) -> None:
        if self._closing or player.closed:
            return
        if controls:
            self._control_revision[player.session_id] = (
                self._control_revision.get(player.session_id, 0) + 1
            )
        if urgent or controls:
            self._signal_urgent_refresh(player.session_id)
        self._refresh_pending.add(player.session_id)
        task = self._refresh_tasks.get(player.session_id)
        if task is None or task.done():

            async def update() -> None:
                try:
                    while player.session_id in self._refresh_pending:
                        self._refresh_pending.discard(player.session_id)
                        await self.publish(player)
                finally:
                    self._refresh_tasks.pop(player.session_id, None)

            self._refresh_tasks[player.session_id] = asyncio.create_task(update())

    async def publish(
        self, player: GuildPlayer, *, force: bool = False, urgent: bool = False
    ) -> bool:
        session_id = player.session_id
        if urgent:
            # Wake a progress refresh already waiting while holding ui_lock.
            self._signal_urgent_refresh(session_id)
        async with player.ui_lock:
            loop = asyncio.get_running_loop()
            wake = self._refresh_wake.setdefault(session_id, asyncio.Event())
            while True:
                hard_delay = self._refresh_backoff.get(session_id, 0) - loop.time()
                if hard_delay > 0:
                    # Neither controls nor progress may bypass an HTTP failure or
                    # Discord retry deadline. Already-issued requests stay owned.
                    await asyncio.sleep(hard_delay)
                    continue
                self._background_refresh_interval(session_id, loop.time())
                delay = self._refresh_after.get(session_id, 0) - loop.time()
                if urgent or session_id in self._refresh_urgent or delay <= 0:
                    break
                wake.clear()
                expires_in = self._refresh_slow_until.get(session_id, 0) - loop.time()
                if expires_in > 0:
                    # Resume the normal clock as soon as the adaptive window ends.
                    delay = min(delay, expires_in)
                try:
                    await asyncio.wait_for(wake.wait(), delay)
                except TimeoutError:
                    pass
            # Consume only work known before rendering. An action arriving while
            # the HTTP request is in flight keeps its flag for the next refresh.
            self._refresh_urgent.discard(session_id)
            wake.clear()
            try:
                return await self._publish(player, force=force)
            finally:
                # Base cadence on completion, including the SDK's own bucket/429
                # waits. Never send a catch-up burst of missed timer updates.
                completed = loop.time()
                self._refresh_after[session_id] = completed + self._background_refresh_interval(
                    session_id, completed
                )

    def _background_refresh_interval(self, session_id: str, now: float) -> float:
        normal = UI_REFRESH_INTERVAL * (
            1 if self._saved_component(session_id) is not None else UI_REST_PROGRESS_MULTIPLIER
        )
        if self._refresh_slow_until.get(session_id, 0) > now:
            return max(normal, self._refresh_interval[session_id])
        interval = self._refresh_interval.pop(session_id, None)
        self._refresh_slow_until.pop(session_id, None)
        if interval is not None and session_id in self._refresh_after:
            # The previous deadline used the longer interval. Restore the same
            # completion time's ordinary cadence when its adaptive window expires.
            self._refresh_after[session_id] -= interval - normal
        return normal

    def _component_context(self, player: GuildPlayer, interaction: discord.Interaction) -> bool:
        return bool(
            interaction.type
            in (discord.InteractionType.component, discord.InteractionType.modal_submit)
            and player.message is not None
            and getattr(interaction, "message", None) is not None
            and interaction.message.id == player.message.id
            and getattr(interaction, "guild_id", player.guild.id) == player.guild.id
        )

    @staticmethod
    def _component_expired(interaction: discord.Interaction) -> bool:
        expired = getattr(interaction, "is_expired", None)
        return bool(expired()) if callable(expired) else False

    def remember_component(self, player: GuildPlayer, interaction: discord.Interaction) -> bool:
        """Remember only a response which edits this canonical player card."""
        if (
            self._closing
            or player.closed
            or self.players.get(player.guild.id) is not player
            or not self._component_context(player, interaction)
            or interaction.response.type
            not in (
                discord.InteractionResponseType.deferred_message_update,
                discord.InteractionResponseType.message_update,
            )
            or self._component_expired(interaction)
        ):
            return False
        previous = self._component_interactions.get(player.session_id)
        if previous is not None and not self._component_expired(previous):
            old_id, new_id = getattr(previous, "id", None), getattr(interaction, "id", None)
            if isinstance(old_id, int) and isinstance(new_id, int) and old_id > new_id:
                return False
        self._component_interactions[player.session_id] = interaction
        return True

    def _saved_component(self, session_id: str) -> discord.Interaction | None:
        interaction = self._component_interactions.get(session_id)
        if interaction is not None:
            player = self.players.get(interaction.guild_id)
            if (
                self._component_expired(interaction)
                or player is None
                or player.session_id != session_id
                or not self._component_context(player, interaction)
            ):
                self._component_interactions.pop(session_id, None)
                return None
        return interaction

    async def _ack_component(
        self, player: GuildPlayer, interaction: discord.Interaction, response_lock: asyncio.Lock
    ) -> None:
        async with response_lock:
            if not interaction.response.is_done():
                await interaction.response.defer(thinking=False)
            self.remember_component(player, interaction)

    async def publish_component(
        self,
        player: GuildPlayer,
        interaction: discord.Interaction,
        *,
        guard: Callable[[], Any] | None = None,
    ) -> bool:
        # The caller may be cancelled while Discord already has the request.
        # Retain the UI transaction and its lock through HTTP and ViewStore commit.
        started = asyncio.get_running_loop().time()
        task = asyncio.create_task(
            self._publish_component(player, interaction, guard=guard),
            name="player-component-publish",
        )
        self._component_tasks[task] = player.session_id
        cancelled = False
        try:
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
        finally:
            self._component_tasks.pop(task, None)
            response_type = getattr(interaction.response, "type", None)
            log.info(
                "Player component update server=%s response_type=%s duration_ms=%.0f",
                player.guild.id,
                response_type.value
                if isinstance(response_type, discord.InteractionResponseType)
                else "none",
                (asyncio.get_running_loop().time() - started) * 1000,
            )

    async def _publish_component(
        self,
        player: GuildPlayer,
        interaction: discord.Interaction,
        *,
        guard: Callable[[], Any] | None = None,
    ) -> bool:
        if (
            not self._component_context(player, interaction)
            or player.closed
            or self.players.get(player.guild.id) is not player
        ):
            raise UserError("This player card is no longer active. Use /nowplaying again.")
        if interaction.response.is_done() and interaction.response.type not in (
            discord.InteractionResponseType.deferred_message_update,
            discord.InteractionResponseType.message_update,
        ):
            raise UserError("This interaction does not update the player card.")
        response_lock = asyncio.Lock()
        ack_in_flight = False
        delay = COMPONENT_ACK_DEADLINE
        created = getattr(interaction, "created_at", None)
        if created is not None:
            try:
                delay = max(0.0, delay - (discord.utils.utcnow() - created).total_seconds())
            except TypeError, AttributeError:
                pass

        async def acknowledge() -> None:
            nonlocal ack_in_flight
            await asyncio.sleep(delay)
            async with response_lock:
                if not interaction.response.is_done():
                    ack_in_flight = True
                    try:
                        await interaction.response.defer(thinking=False)
                        self.remember_component(player, interaction)
                    finally:
                        ack_in_flight = False

        ack_task = asyncio.create_task(acknowledge(), name="player-component-ack")
        try:
            if player.ui_lock.locked():
                # A previous REST write can already be waiting on Discord. ACK
                # independently, then serialize this edit after that write.
                await self._ack_component(player, interaction, response_lock)
            # A soft-waiting progress writer must see the new transport before
            # waking; otherwise it can issue another slow channel REST edit.
            self._signal_urgent_refresh(player.session_id)
            async with player.ui_lock:
                if guard is not None:
                    guard()
                if player.closed or not self._component_context(player, interaction):
                    raise UserError("This player card is no longer active. Use /nowplaying again.")
                loop = asyncio.get_running_loop()
                remaining = self._refresh_backoff.get(player.session_id, 0) - loop.time()
                if remaining > 0:
                    await asyncio.sleep(remaining)
                if guard is not None:
                    guard()
                if player.closed or not self._component_context(player, interaction):
                    raise UserError("This player card is no longer active. Use /nowplaying again.")
                self._refresh_urgent.discard(player.session_id)
                self._refresh_wake[player.session_id].clear()
                try:
                    published = await self._publish(
                        player, interaction=interaction, response_lock=response_lock, guard=guard
                    )
                    if not interaction.response.is_done():
                        # A failed edit still needs a quiet initial ACK. The
                        # shared publisher schedules its normal bounded retry.
                        await self._ack_component(player, interaction, response_lock)
                    return published
                finally:
                    completed = loop.time()
                    self._refresh_after[player.session_id] = (
                        completed + self._background_refresh_interval(player.session_id, completed)
                    )
        finally:
            # Cancelling our deadline must never cancel an ACK HTTP request that
            # already started. Finish it before releasing ownership of its task.
            if not ack_task.done() and not ack_in_flight:
                ack_task.cancel()
            cancelled = False
            while not ack_task.done():
                try:
                    await asyncio.shield(ack_task)
                except asyncio.CancelledError:
                    if ack_task.cancelled():
                        break
                    cancelled = True
                except Exception:
                    break
            if not ack_task.cancelled():
                ack_task.exception()
            if cancelled:
                raise asyncio.CancelledError

    async def _edit_component_card(
        self,
        player: GuildPlayer,
        interaction: discord.Interaction,
        payload: dict[str, Any],
        response_lock: asyncio.Lock | None,
        guard: Callable[[], Any] | None,
    ) -> None:
        async with response_lock if response_lock is not None else asyncio.Lock():
            if guard is not None:
                guard()
            operation = (
                interaction.edit_original_response(**payload)
                if interaction.response.is_done()
                else interaction.response.edit_message(**payload)
            )
            await self._card_request(player, operation)
            self.remember_component(player, interaction)

    async def _edit_player_card(
        self,
        player: GuildPlayer,
        payload: dict[str, Any],
        interaction: discord.Interaction | None,
        response_lock: asyncio.Lock | None,
        guard: Callable[[], Any] | None,
    ) -> None:
        saved = interaction or self._saved_component(player.session_id)
        if (
            saved is not None
            and not self._component_context(player, saved)
            and self._component_interactions.get(player.session_id) is saved
        ):
            self._component_interactions.pop(player.session_id, None)
        if saved is not None and self._component_context(player, saved):
            try:
                await self._edit_component_card(player, saved, payload, response_lock, guard)
                return
            except discord.HTTPException as error:
                if error.status not in (401, 404):
                    raise
                if self._component_interactions.get(player.session_id) is saved:
                    self._component_interactions.pop(player.session_id, None)
                if guard is not None:
                    guard()
        await self._card_request(player, player.message.edit(**payload))

    async def _card_request[T](self, player: GuildPlayer, operation: Awaitable[T]) -> T:
        loop = asyncio.get_running_loop()
        started = loop.time()
        try:
            return await operation
        finally:
            completed = loop.time()
            elapsed = completed - started
            if elapsed > UI_SLOW_REQUEST_THRESHOLD:
                interval = min(
                    UI_ADAPTIVE_MAX_INTERVAL,
                    max(UI_ADAPTIVE_MIN_INTERVAL, elapsed + UI_REFRESH_INTERVAL),
                )
                self._refresh_interval[player.session_id] = interval
                self._refresh_slow_until[player.session_id] = completed + UI_ADAPTIVE_DURATION
                log.warning(
                    "Playback UI request was slow in server %s: duration_ms=%.0f "
                    "background_interval_s=%.2f",
                    player.guild.id,
                    elapsed * 1000,
                    interval,
                )

    async def _publish(
        self,
        player: GuildPlayer,
        *,
        force: bool = False,
        interaction: discord.Interaction | None = None,
        response_lock: asyncio.Lock | None = None,
        guard: Callable[[], Any] | None = None,
    ) -> bool:
        view = None
        published = False
        session_id = player.session_id
        try:
            embed = await now_playing(self, player)
            active = not player.closed and bool(player.current or player.queue)
            previous = self._views.get(session_id)
            if active:
                code = await self.languages.code(player.guild.id)
                view = Controls(self, player, code)
            components = view.to_components() if view else None
            revision = self._control_revision.get(session_id, 0)
            payload = (embed.to_dict(), components, revision)
            rendered = self._rendered.get(session_id)
            if guard is not None:
                guard()
            if not force and player.message and rendered == payload:
                if interaction is not None:
                    await self._ack_component(player, interaction, response_lock)
                return True
            unchanged_controls = bool(
                previous
                and not previous.is_finished()
                and view
                and previous.to_components() == components
                and rendered is not None
                and rendered[2] == revision
            )
            if unchanged_controls:
                view.stop()
                view = previous
            if player.message:
                try:
                    if unchanged_controls:
                        # Timer-only edits keep registered button/menu handlers.
                        await self._edit_player_card(
                            player, {"embed": embed}, interaction, response_lock, guard
                        )
                    else:
                        await self._edit_player_card(
                            player,
                            {"embed": embed, "view": view},
                            interaction,
                            response_lock,
                            guard,
                        )
                    published = True
                except discord.NotFound:
                    self._component_interactions.pop(session_id, None)
                    if previous:
                        previous.stop()
                    self._views.pop(session_id, None)
                    player.message = None
                    if unchanged_controls:
                        view = Controls(self, player, code)
            if not published and active:
                player.message = await self._card_request(
                    player, player.text_channel.send(embed=embed, view=view)
                )
                published = True
            if published or player.closed:
                previous = self._views.pop(session_id, None)
                if previous and previous is not view:
                    previous.stop()
                if view:
                    # Message.edit already registered these same custom IDs.
                    # Stopping the old view removes their dispatch entries, so
                    # restore the new view after the old one has been retired.
                    if not unchanged_controls or previous is not view:
                        self.add_view(view, message_id=player.message.id)
                    self._views[session_id] = view
                if published:
                    self._rendered[session_id] = payload
                    self._refresh_failures.pop(session_id, None)
                    self._refresh_backoff.pop(session_id, None)
            return published
        except discord.RateLimited as error:
            self._refresh_backoff[session_id] = asyncio.get_running_loop().time() + max(
                UI_REFRESH_INTERVAL, error.retry_after
            )
            if not player.closed and not self._closing:
                self.request_refresh(player)
            log.warning(
                "Playback UI is waiting for Discord's rate limit in server %s", player.guild.id
            )
            return False
        except discord.HTTPException:
            failures = min(5, self._refresh_failures.get(session_id, 0) + 1)
            self._refresh_failures[session_id] = failures
            self._refresh_backoff[session_id] = asyncio.get_running_loop().time() + min(
                60, HTTP_RETRY_INTERVAL * 2 ** (failures - 1)
            )
            if not player.closed and not self._closing:
                self.request_refresh(player)
            log.warning("Playback UI could not be refreshed in server %s", player.guild.id)
            return False
        finally:
            if view and self._views.get(player.session_id) is not view:
                view.stop()

    def _progress_tick(self) -> None:
        if self._closing:
            return
        for player in list(self.players.values()):
            if player.closed:
                continue
            advancing = player.current and player.status == "playing" and not player.paused
            if advancing or player.has_notices:
                self.request_refresh(player)

    async def _progress_updates(self) -> None:
        while True:
            await asyncio.sleep(PROGRESS_INTERVAL)
            self._progress_tick()

    async def _checkpoints(self) -> None:
        count = 0
        while True:
            await asyncio.sleep(5)
            count += 1
            for player in list(self.players.values()):
                try:
                    await player.persist()
                    if count % 6 == 0:
                        player.log_health()
                except Exception:
                    log.exception("Could not checkpoint server %s", player.guild.id)

    async def on_voice_state_update(
        self, member: discord.Member, before: discord.VoiceState, after: discord.VoiceState
    ) -> None:
        player = self.players.get(member.guild.id)
        if not player or player.closed:
            return
        if self.user and member.id == self.user.id:
            if before.channel and after.channel is None:
                await player.connection_lost()
                return
            if isinstance(after.channel, discord.StageChannel):
                await player._report_safely(
                    "Stage channels are not supported, so I disconnected. "
                    "Use /play in a regular voice channel to start again."
                )
                await self.remove_player(player)
                return
            if after.channel:
                player.voice_channel = after.channel
            await player.pause("mute", bool(after.mute or after.deaf or after.suppress))
        if before.channel == player.voice_channel or after.channel == player.voice_channel:
            await player.listeners_changed(
                any(not item.bot for item in player.voice_channel.members)
            )

    async def on_guild_remove(self, guild: discord.Guild) -> None:
        if player := self.players.get(guild.id):
            await self.remove_player(player)
        else:
            await self.store.delete(guild.id)

    async def _command_error(
        self, interaction: discord.Interaction, error: app_commands.AppCommandError
    ) -> None:
        await self.interaction_error(interaction, getattr(error, "original", error))

    async def interaction_error(self, interaction: discord.Interaction, error: Exception) -> None:
        if isinstance(error, UserError):
            message = str(error)
        elif isinstance(error, app_commands.CommandOnCooldown):
            seconds = max(1, math.ceil(error.retry_after))
            message = f"Please wait {seconds} seconds before searching or adding more music."
        elif isinstance(error, app_commands.MissingPermissions):
            message = "You do not have permission to use this command."
        else:
            log.error("Interaction failed", exc_info=error)
            message = "That action failed. Please try again."
        try:
            if interaction.response.is_done():
                await interaction.followup.send(message[:1900], ephemeral=True)
            else:
                await interaction.response.send_message(message[:1900], ephemeral=True)
        except discord.HTTPException:
            log.debug("The interaction expired before an error response could be sent.")

    async def close(self) -> None:
        async with self._close_lock:
            await self._close()

    async def _close(self) -> None:
        if self._closing:
            return
        self._closing = True
        if self._progress_task:
            self._progress_task.cancel()
            await asyncio.gather(self._progress_task, return_exceptions=True)
        if self._heartbeat:
            self._heartbeat.cancel()
            await asyncio.gather(self._heartbeat, return_exceptions=True)
        players = list(self.players.values())
        results = await asyncio.gather(
            *(player.close(preserve=True) for player in players),
            return_exceptions=True,
        )
        for player, result in zip(players, results, strict=True):
            if isinstance(result, BaseException):
                log.error("Player shutdown failed for server %s: %s", player.guild.id, result)
        self.players.clear()
        refresh_tasks = list(self._refresh_tasks.values())
        for task in refresh_tasks:
            task.cancel()
        await asyncio.gather(*refresh_tasks, return_exceptions=True)
        await asyncio.gather(
            *(
                asyncio.shield(task)
                for task in list(self._component_tasks)
                if task is not asyncio.current_task()
            ),
            return_exceptions=True,
        )
        self._component_tasks.clear()
        for view in self._views.values():
            view.stop()
        self._views.clear()
        self._refresh_pending.clear()
        self._refresh_after.clear()
        self._refresh_backoff.clear()
        self._refresh_urgent.clear()
        self._refresh_wake.clear()
        self._refresh_interval.clear()
        self._refresh_slow_until.clear()
        self._component_interactions.clear()
        self._refresh_failures.clear()
        self._control_revision.clear()
        self._rendered.clear()
        for name in ("youtube_status", "lyrics", "cache", "media", "web"):
            service = getattr(self, name, None)
            if service is not None:
                try:
                    await service.close()
                except Exception:
                    log.exception("Could not close %s during shutdown", name)
        await super().close()
