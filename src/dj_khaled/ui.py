from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any
from urllib.parse import quote, urlsplit
from uuid import uuid4

import discord

from .activity import added_tracks, track_action, user_action
from .icons import get_icon
from .lyrics import Lyrics, paginate
from .models import GENRES, LoopMode, Track, UserError, duration_text, progress_text
from .seek import parse_position

if TYPE_CHECKING:
    from .bot import DJKhaled
    from .player import GuildPlayer

GOLD = 0xFBBF24
AMBER = 0xF59E0B
BLURPLE = 0x5865F2
SLATE = 0x64748B
QUEUE_PAGE_SIZE = 5


def clean_label(value: str) -> str:
    """Keep English labels while removing the old decorative emoji prefixes."""
    return re.sub(r"^[^\w]+", "", value).strip()


def track_link(track: Track, limit: int = 150, budget: int = 1000) -> str:
    title = short(track.title, min(limit, budget))
    try:
        parsed = urlsplit(track.url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            return title
    except ValueError:
        return title
    url = quote(track.url, safe=":/?#[]@!$&'*+,;=%-._~")
    linked = f"[{title}](<{url}>)"
    return linked if len(linked) <= budget else title


def short(value: str, limit: int = 200) -> str:
    value = str(value).replace("\x00", "")
    # Link labels need individual bracket escaping; a general Markdown helper
    # can leave a closing bracket capable of terminating our generated link.
    escaped = re.sub(r"([\\`*_~|\[\]()<>#+\-])", r"\\\1", value)
    if len(escaped) <= limit:
        return escaped
    prefix = escaped[: max(0, limit - 1)]
    trailing_slashes = len(prefix) - len(prefix.rstrip("\\"))
    if trailing_slashes % 2:
        prefix = prefix[:-1]
    return prefix + "…"


def selection_value(interaction: discord.Interaction, menu: discord.ui.Select) -> str:
    """Read the dispatched payload, which is stable across concurrent callbacks."""
    data = getattr(interaction, "data", None)
    values = data.get("values", []) if isinstance(data, dict) else menu.values
    if len(values) != 1 or not isinstance(values[0], str):
        raise UserError("This selection is no longer valid. Open a new selector.")
    return values[0]


def require_dashboard(interaction: discord.Interaction, player: GuildPlayer) -> None:
    message = getattr(interaction, "message", None)
    dashboard = getattr(player, "message", None)
    if dashboard is None or getattr(message, "id", None) != dashboard.id:
        raise UserError("Use the controls on the current Now Playing card.")


def remember_control(bot: DJKhaled, player: GuildPlayer, interaction: discord.Interaction) -> None:
    remember = getattr(bot, "remember_component", None)
    if callable(remember):
        remember(player, interaction)


def add_activity(embed: discord.Embed, player: GuildPlayer) -> discord.Embed:
    notices = getattr(player, "notices", [])
    if notices:
        embed.add_field(
            name="Recent actions",
            value="\n".join("• " + short(message, 240) for message in notices[-3:]),
            inline=False,
        )
    return embed


def add_youtube_status(
    embed: discord.Embed, bot: DJKhaled, *, checking_text: str | None = None
) -> discord.Embed:
    """Render the cached session label without waiting for a provider request."""
    label = getattr(getattr(bot, "media", None), "youtube_status_label", None)
    if callable(label) and isinstance(value := label(), str) and value:
        if value == "Checking..." and checking_text:
            value = checking_text
        embed.add_field(name="YouTube sign-in", value=value, inline=False)
    return embed


def clamp_queue_page(player: GuildPlayer, count: int) -> int | None:
    page = getattr(player, "queue_page", None)
    if page is None:
        return None
    if not isinstance(page, int) or isinstance(page, bool):
        page = 0
    player.queue_page = min(max(0, page), max(0, (count - 1) // QUEUE_PAGE_SIZE))
    return player.queue_page


def add_queue(embed: discord.Embed, player: GuildPlayer, state: dict[str, Any]) -> discord.Embed:
    if getattr(player, "closed", False):
        return embed
    queued = state["queue"]
    page = clamp_queue_page(player, len(queued))
    if page is None:
        return embed
    pages = max(1, (len(queued) + QUEUE_PAGE_SIZE - 1) // QUEUE_PAGE_SIZE)
    start = page * QUEUE_PAGE_SIZE
    lines = []
    for index, data in enumerate(queued[start : start + QUEUE_PAGE_SIZE], start + 1):
        track = Track.from_dict(data)
        lines.append(
            f"`{index:02}` {track_link(track, 80, budget=160)} · `{duration_text(track.duration)}`"
        )
    embed.add_field(
        name=f"Queue · Page {page + 1}/{pages}",
        value="\n".join(lines)[:1000] if lines else "Nothing queued. Add a song with **/play**.",
        inline=False,
    )
    return embed


async def dismiss_selector(interaction: discord.Interaction) -> None:
    """Only Search/Genre messages use this; modal origins can be the player card."""
    try:
        await interaction.delete_original_response()
    except discord.HTTPException:
        pass


async def now_playing(bot: DJKhaled, player: GuildPlayer) -> discord.Embed:
    code = await bot.languages.code(player.guild.id)

    def text(key: str, default: str | None = None, **values: object) -> str:
        return bot.languages.text(code, key, default, **values)

    state = await player.snapshot()
    embed = discord.Embed(color=GOLD)
    if player.closed or not state["current"]:
        if not player.closed and state["queue"]:
            embed.color = BLURPLE
            embed.set_author(name=bot.settings.app_name)
            embed.title = "Getting the next track ready"
            embed.description = "Playback will continue shortly."
            embed.set_footer(text=f"{len(state['queue'])} queued")
            return add_activity(add_queue(embed, player, state), player)
        embed.color = SLATE
        embed.set_author(name=bot.settings.app_name)
        embed.title = (
            text("player.ended", "Session ended")
            if player.closed
            else text("player.queue_finished", "You're all caught up")
        )
        embed.description = text(
            "player.add_music", "Join a voice channel and use **/play** to add a song or playlist."
        )
        embed.set_footer(text=text("player.ready", "Ready for your next track"))
        return add_activity(add_queue(embed, player, state), player)
    track = Track.from_dict(state["current"])
    embed.title = track.title[:256]
    embed.url = track.url
    source = text("commands.nowplaying.platform_name_" + track.platform, track.platform.title())
    if track.platform == "spotify":
        source += " · TIDAL audio"
    detail = [short(track.artist, 160), source]
    embed.description = " · ".join(item for item in detail if item)
    if track.requester_id:
        requested = clean_label(text("commands.nowplaying.requested_by", "Requested by"))
        embed.description += f"\n{requested} <@{track.requester_id}>"
    if player.status == "loading":
        status = text("player.loading", "Getting your track ready")
        embed.color = BLURPLE
    elif "connection" in player.pause_reasons:
        status = text("player.reconnecting", "Reconnecting")
        embed.color = BLURPLE
    elif player.paused:
        status = clean_label(text("commands.nowplaying.status_paused", "Paused"))
        embed.color = AMBER
    else:
        status = clean_label(text("commands.play.now_playing", "Now playing"))
    embed.set_author(name=f"{bot.settings.app_name} · {status}")
    playback = progress_text(state["position"], track.duration)
    if player.status == "loading":
        playback = text("player.preparing", "Preparing audio. Playback will start shortly.")
    elif "connection" in player.pause_reasons:
        playback += "\n" + text(
            "player.connection_lost", "Reconnecting to voice. Your queue is saved."
        )
    elif "mute" in player.pause_reasons:
        playback += "\n" + text(
            "player.muted", "Paused because the bot is muted or cannot speak in this channel."
        )
    elif "alone" in player.pause_reasons:
        playback += "\n" + text("player.alone", "Paused until someone joins the voice channel.")
    embed.add_field(
        name=text("player.playback", "Playback"),
        value=playback,
        inline=False,
    )
    if track.platform == "youtube" and track.age_restricted:
        add_youtube_status(embed, bot)
    if state["loop"] == LoopMode.TRACK:
        next_name = text("player.on_repeat", "On repeat")
        next_value = track_link(track)
    elif state["queue"]:
        next_track = Track.from_dict(state["queue"][0])
        next_name = (
            text("player.from_queue", "From your queue")
            if state["shuffle"]
            else text("player.up_next", "Up next")
        )
        next_value = track_link(next_track)
        if next_track.duration:
            next_value += f" · `{duration_text(next_track.duration)}`"
    elif state["loop"] == LoopMode.QUEUE:
        next_name = text("player.up_next", "Up next")
        next_value = track_link(track)
    elif state["autoplay"]:
        next_name = text("player.up_next", "Up next")
        next_value = text(
            "player.autoplay_next",
            "Autoplay will find another {genre} track.",
            genre=text("genres." + state["autoplay"]),
        )
    else:
        next_name = text("player.up_next", "Up next")
        next_value = text(
            "player.queue_empty", "Nothing queued. Add your next track with **/play**."
        )
    embed.add_field(name=next_name, value=next_value[:1024], inline=False)
    if track.thumbnail and track.thumbnail.startswith(("http://", "https://")):
        embed.set_thumbnail(url=track.thumbnail)
    footer = [text("player.volume", "Volume {volume}%", volume=state["volume"])]
    footer.append(text("player.queued", "{count} queued", count=len(state["queue"])))
    if state["loop"] != LoopMode.OFF:
        footer.append(clean_label(text("buttons.loop_" + state["loop"])))
    if state["shuffle"]:
        footer.append(clean_label(text("buttons.shuffle_on")))
    if state["autoplay"]:
        footer.append(
            clean_label(text("buttons.autoplay_on")) + ": " + text("genres." + state["autoplay"])
        )
    if channel_name := getattr(player.voice_channel, "name", None):
        footer.append(channel_name[:80])
    embed.set_footer(text=" · ".join(footer))
    return add_activity(add_queue(embed, player, state), player)


class SafeView(discord.ui.View):
    def __init__(self, bot: DJKhaled, *, timeout: float | None = 300) -> None:
        super().__init__(timeout=timeout)
        self.bot = bot
        self.message: discord.Message | None = None
        self._claimed = False

    async def claim(self, interaction: discord.Interaction) -> bool:
        if self._claimed or self.is_finished():
            await interaction.response.send_message(
                "This selection has already been handled.", ephemeral=True
            )
            return False
        # No await before claiming: two already-dispatched callbacks cannot both win.
        self._claimed = True
        for item in self.children:
            if isinstance(item, (discord.ui.Button, discord.ui.Select)):
                item.disabled = True
        self.stop()
        return True

    async def on_error(
        self, interaction: discord.Interaction, error: Exception, item: discord.ui.Item[Any]
    ) -> None:
        await self.bot.interaction_error(interaction, error)

    async def on_timeout(self) -> None:
        for item in self.children:
            if isinstance(item, (discord.ui.Button, discord.ui.Select)):
                item.disabled = True
        if self.message:
            try:
                await self.message.edit(view=self)
            except discord.HTTPException:
                pass
        self.stop()


class Controls(SafeView):
    def __init__(self, bot: DJKhaled, player: GuildPlayer, language: str) -> None:
        super().__init__(bot, timeout=None)
        self.player = player
        queue_count = len(player.queue)
        page = clamp_queue_page(player, queue_count)

        def label(key: str) -> str:
            return clean_label(bot.languages.text(language, "buttons." + key))

        buttons = (
            (
                "pause",
                label("resume" if "manual" in player.pause_reasons else "pause"),
                "play" if "manual" in player.pause_reasons else "pause",
                True,
            ),
            ("skip", label("skip"), "skip", False),
            (
                "queue",
                f"{'Hide queue' if page is not None else label('queue')} ({queue_count})",
                "queue",
                False,
            ),
            ("disconnect", label("disconnect"), "disconnect", False),
        )
        for index, (action, title, icon, active) in enumerate(buttons):
            button = discord.ui.Button(
                label=title[:80],
                emoji=get_icon(bot, icon),
                custom_id=f"dj:{player.session_id}:{action}",
                style=discord.ButtonStyle.primary if active else discord.ButtonStyle.secondary,
                row=index // 5,
                disabled=action == "skip" and player.current is None,
            )
            button.callback = self.handler(action)
            self.add_item(button)

        options = [
            discord.SelectOption(
                label=f"Volume · {player.volume}%",
                value="volume",
                emoji=get_icon(bot, "volume"),
                description="Adjust playback volume.",
            ),
            discord.SelectOption(
                label=label("loop_" + player.loop_mode.value)[:100],
                value="loop",
                description="Cycle between repeat off, one track, and the whole queue.",
            ),
            discord.SelectOption(
                label=label("shuffle_on" if player.shuffle else "shuffle")[:100],
                value="shuffle",
                description="Turn shuffle off."
                if player.shuffle
                else "Play the queue in a random order.",
            ),
            discord.SelectOption(
                label=label("autoplay_on" if player.autoplay else "autoplay_off")[:100],
                value="autoplay",
                description="Turn autoplay off."
                if player.autoplay
                else "Choose a genre and keep the music going.",
            ),
        ]
        if player.current and player.current.duration > 0:
            options.insert(
                0,
                discord.SelectOption(
                    label="Seek to time",
                    value="seek",
                    emoji=get_icon(bot, "seek"),
                    description="Jump to a position such as 1:30 or 90 seconds.",
                ),
            )
        if player.history:
            options.insert(
                1 if options[0].value == "seek" else 0,
                discord.SelectOption(
                    label=label("previous")[:100],
                    value="previous",
                    emoji=get_icon(bot, "previous"),
                    description="Go back to the previous track.",
                ),
            )
        if player.current and player.current_lyrics:
            options.append(
                discord.SelectOption(
                    label=label("lyrics")[:100],
                    value="lyrics",
                    description="Open lyrics for the current track.",
                )
            )
        menu = discord.ui.Select(
            placeholder=label("more")[:150],
            custom_id=f"dj:{player.session_id}:more",
            options=options,
            row=1,
        )

        async def selected(interaction: discord.Interaction) -> None:
            action = selection_value(interaction, menu)
            try:
                await self.handler(action)(interaction)
            finally:
                # Restore the action menu's placeholder after a selection.
                self.bot.request_refresh(self.player, controls=True)

        menu.callback = selected
        self.add_item(menu)
        pages = max(1, (queue_count + QUEUE_PAGE_SIZE - 1) // QUEUE_PAGE_SIZE)
        if page is not None and pages > 1:
            for action, title, disabled in (
                ("queue_previous", "Previous page", page == 0),
                ("queue_page", f"Page {page + 1}/{pages}", True),
                ("queue_next", "Next page", page == pages - 1),
            ):
                button = discord.ui.Button(
                    label=title,
                    custom_id=f"dj:{player.session_id}:{action}",
                    style=discord.ButtonStyle.secondary,
                    disabled=disabled,
                    row=2,
                )
                button.callback = self.handler(action)
                self.add_item(button)

    def handler(self, action: str):
        async def callback(interaction: discord.Interaction) -> None:
            if action not in {
                "volume",
                "seek",
                "autoplay",
                "queue",
                "queue_previous",
                "queue_next",
                "queue_page",
                "lyrics",
                "previous",
                "pause",
                "skip",
                "disconnect",
                "stop",
                "shuffle",
                "loop",
            }:
                raise UserError("This playback control is no longer available.")
            queue_action = action in {"queue", "queue_previous", "queue_next", "queue_page"}
            self.bot.authorize(
                interaction, self.player, control=not queue_action and action != "lyrics"
            )
            if action == "volume":
                await interaction.response.send_modal(VolumeModal(self.bot, self.player))
                return
            if action == "seek":
                track = self.player.current
                if track is None:
                    raise UserError("There is no track playing to seek.")
                if track.duration <= 0:
                    raise UserError(
                        "Seeking needs a known song length. Try again once audio is ready."
                    )
                await interaction.response.send_modal(SeekModal(self.bot, self.player, track))
                return
            if action == "autoplay" and not self.player.autoplay:
                view = GenreView(self.bot, self.player, interaction.user.id)
                await interaction.response.send_message(
                    "Choose an autoplay genre.", view=view, ephemeral=True
                )
                view.message = await interaction.original_response()
                return
            if queue_action:
                require_dashboard(interaction, self.player)
                state = await self.player.snapshot()
                self.bot.authorize(interaction, self.player, control=False)
                require_dashboard(interaction, self.player)
                page = clamp_queue_page(self.player, len(state["queue"]))
                if action == "queue":
                    self.player.queue_page = 0 if page is None else None
                elif page is not None:
                    step = -1 if action == "queue_previous" else 1 if action == "queue_next" else 0
                    self.player.queue_page = page + step
                    clamp_queue_page(self.player, len(state["queue"]))

                def guard() -> None:
                    self.bot.authorize(interaction, self.player, control=False)
                    require_dashboard(interaction, self.player)

                await self.bot.publish_component(self.player, interaction, guard=guard)
                return
            if action == "lyrics":
                lyrics = self.player.current_lyrics
                track = self.player.current
                if not lyrics or not track:
                    raise UserError("Lyrics are not available for this track yet.")
                view = LyricsView(self.bot, track, lyrics, interaction.user.id)
                await interaction.response.send_message(
                    embed=view.embed(), view=view, ephemeral=True
                )
                view.message = await interaction.original_response()
                return
            await interaction.response.defer(thinking=False)
            self.bot.authorize(interaction, self.player, control=True)
            remember_control(self.bot, self.player, interaction)
            if action == "previous":
                track = await self.player.previous()
                message = track_action(interaction.user, "went back to", track)
            elif action == "pause":
                enabled = "manual" not in self.player.pause_reasons
                track = await self.player.pause("manual", enabled)
                message = (
                    track_action(interaction.user, "paused", track)
                    if enabled
                    else (
                        user_action(
                            interaction.user, "cleared the manual pause; waiting for voice."
                        )
                        if self.player.paused
                        else track_action(interaction.user, "resumed", track)
                    )
                )
            elif action == "skip":
                track = await self.player.skip()
                message = track_action(interaction.user, "skipped", track)
            elif action in {"disconnect", "stop"}:
                await self.bot.remove_player(
                    self.player,
                    guard=lambda: self.bot.authorize(interaction, self.player, control=True),
                    notice=user_action(interaction.user, "disconnected the bot. Queue cleared."),
                )
                # Removal records its notice before rendering the ended card.
                return
            elif action == "shuffle":
                await self.player.toggle_shuffle()
                message = user_action(
                    interaction.user,
                    "enabled shuffle." if self.player.shuffle else "disabled shuffle.",
                )
            elif action == "loop":
                modes = list(LoopMode)
                await self.player.set_loop(
                    modes[(modes.index(self.player.loop_mode) + 1) % len(modes)]
                )
                message = user_action(
                    interaction.user, "set repeat to " + self.player.loop_mode.value + "."
                )
            else:
                await self.player.set_autoplay(None)
                message = user_action(interaction.user, "disabled autoplay.")
            self.player.note(message)

        return callback


class SeekModal(discord.ui.Modal):
    value = discord.ui.TextInput(
        label="Jump to (M:SS or seconds)",
        placeholder="For example: 1:30, 90, or 0:01:30",
        min_length=1,
        max_length=32,
    )

    def __init__(self, bot: DJKhaled, player: GuildPlayer, track: Track) -> None:
        super().__init__(
            title="Seek to time",
            timeout=300,
            custom_id=f"seek:{player.session_id}:{uuid4().hex}",
        )
        self.bot = bot
        self.player = player
        self.track = track
        self.value.default = duration_text(player.position)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        self.bot.authorize(interaction, self.player, control=True)
        position = parse_position(self.value.value)
        await interaction.response.defer(thinking=False)
        self.bot.authorize(interaction, self.player, control=True)
        remember_control(self.bot, self.player, interaction)
        track = await self.player.seek(position, expected_track=self.track)
        self.player.note(
            user_action(
                interaction.user,
                f"jumped to {duration_text(position)} in {track.title}.",
            )
        )

    async def on_error(self, interaction: discord.Interaction, error: Exception) -> None:
        await self.bot.interaction_error(interaction, error)


class VolumeModal(discord.ui.Modal):
    value = discord.ui.TextInput(label="Volume (0–100)", min_length=1, max_length=3)

    def __init__(self, bot: DJKhaled, player: GuildPlayer) -> None:
        super().__init__(
            title="Set volume",
            timeout=300,
            custom_id=f"volume:{player.session_id}:{uuid4().hex}",
        )
        self.bot = bot
        self.player = player
        self.value.default = str(player.volume)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        self.bot.authorize(interaction, self.player, control=True)
        if not re.fullmatch(r"[0-9]{1,3}", self.value.value) or int(self.value.value) > 100:
            raise UserError("Enter a whole number between 0 and 100.")
        volume = int(self.value.value)
        await interaction.response.defer(thinking=False)
        self.bot.authorize(interaction, self.player, control=True)
        remember_control(self.bot, self.player, interaction)
        await self.player.set_volume(volume)
        self.player.note(user_action(interaction.user, f"set volume to {volume}%."))

    async def on_error(self, interaction: discord.Interaction, error: Exception) -> None:
        await self.bot.interaction_error(interaction, error)


class GenreView(SafeView):
    def __init__(self, bot: DJKhaled, player: GuildPlayer, owner: int) -> None:
        super().__init__(bot)
        self.player = player
        self.owner = owner
        menu = discord.ui.Select(
            placeholder="Autoplay genre",
            options=[
                discord.SelectOption(label=genre.replace("hiphop", "Hip-Hop").title(), value=genre)
                for genre in GENRES
            ],
        )

        async def selected(interaction: discord.Interaction) -> None:
            self.bot.authorize(interaction, player, control=True)
            genre = selection_value(interaction, menu)
            if genre not in GENRES:
                raise UserError("Choose one of the available autoplay genres.")
            if not await self.claim(interaction):
                return
            await interaction.response.defer(thinking=False)
            try:
                self.bot.authorize(interaction, player, control=True)
                await player.set_autoplay(genre)
            except Exception:
                try:
                    await interaction.edit_original_response(view=None)
                except discord.HTTPException:
                    pass
                raise
            player.note(user_action(interaction.user, f"enabled {genre.title()} autoplay."))
            if await self.bot.publish(player, urgent=True):
                await dismiss_selector(interaction)
            else:
                await interaction.edit_original_response(
                    content="Autoplay was updated, but the player card could not update yet.",
                    view=None,
                )

        menu.callback = selected
        self.add_item(menu)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.owner:
            await interaction.response.send_message(
                "Open your own autoplay selector.", ephemeral=True
            )
            return False
        return True


class SearchView(SafeView):
    def __init__(self, bot: DJKhaled, tracks: list[Track], owner: int, guild_id: int) -> None:
        super().__init__(bot)
        self.owner = owner
        self.guild_id = guild_id
        self.tracks = tuple(tracks[:9])
        menu = discord.ui.Select(
            placeholder="Select a track",
            options=[
                discord.SelectOption(
                    label=f"{index + 1}. {track.title}"[:100],
                    description=f"{track.artist} · {duration_text(track.duration)}"[:100],
                    value=str(index),
                )
                for index, track in enumerate(self.tracks)
            ],
        )

        async def selected(interaction: discord.Interaction) -> None:
            value = selection_value(interaction, menu)
            if not value.isdecimal() or not 0 <= int(value) < len(self.tracks):
                raise UserError("This search result is no longer available. Use /search again.")
            track = self.tracks[int(value)]
            if not await self.claim(interaction):
                return
            await interaction.response.defer(thinking=False)
            try:
                player, count = await bot.enqueue(interaction, [track])
            except Exception:
                try:
                    await interaction.edit_original_response(view=None)
                except discord.HTTPException:
                    pass
                raise
            player.note(added_tracks(interaction.user, [track], count))
            if await bot.publish(player, urgent=True):
                await dismiss_selector(interaction)
            else:
                await interaction.edit_original_response(
                    content="Track queued, but the player card could not update yet. "
                    "Check Send Messages and Embed Links permissions if this continues.",
                    embed=None,
                    view=None,
                )

        menu.callback = selected
        self.add_item(menu)
        cancel = discord.ui.Button(label="Cancel", style=discord.ButtonStyle.danger)

        async def cancelled(interaction: discord.Interaction) -> None:
            if not await self.claim(interaction):
                return
            await interaction.response.defer(thinking=False)
            await dismiss_selector(interaction)

        cancel.callback = cancelled
        self.add_item(cancel)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.owner or interaction.guild_id != self.guild_id:
            await interaction.response.send_message(
                "Use /search to open your own results.", ephemeral=True
            )
            return False
        return True


class LyricsView(SafeView):
    def __init__(self, bot: DJKhaled, track: Track, lyrics: Lyrics, owner: int) -> None:
        super().__init__(bot)
        self.title = short(track.title, 180)
        self.artist = short(track.artist, 140)
        self.source = lyrics.source
        self.owner = owner
        self.pages = paginate(lyrics.text)
        self.index = 0
        self.previous = discord.ui.Button(label="Previous", emoji="◀️", disabled=True)
        self.next = discord.ui.Button(label="Next", emoji="▶️", disabled=len(self.pages) < 2)
        self.previous.callback = self.navigate(-1)
        self.next.callback = self.navigate(1)
        self.add_item(self.previous)
        self.add_item(self.next)

    def embed(self) -> discord.Embed:
        embed = discord.Embed(
            title="Song lyrics",
            description=(
                f"**{self.title}**\n{self.artist}\n\n"
                + (self.pages[self.index] if self.pages else "No lyrics found.")
            ),
            color=0xF4B942,
        )
        embed.set_footer(text=f"{self.source} · {self.index + 1}/{max(1, len(self.pages))}")
        return embed

    def navigate(self, step: int):
        async def callback(interaction: discord.Interaction) -> None:
            self.index = max(0, min(len(self.pages) - 1, self.index + step))
            self.previous.disabled = self.index == 0
            self.next.disabled = self.index >= len(self.pages) - 1
            await interaction.response.edit_message(embed=self.embed(), view=self)

        return callback

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.owner:
            await interaction.response.send_message("Open your own lyrics view.", ephemeral=True)
            return False
        return True
