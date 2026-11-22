from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

import discord
from discord import app_commands

from .activity import added_tracks, user_action
from .models import UserError, duration_text
from .seek import parse_position
from .ui import SearchView, add_youtube_status, short

if TYPE_CHECKING:
    from .bot import DJKhaled

log = logging.getLogger(__name__)
_YOUTUBE_HELP_TIMEOUT = 2


async def dismiss_command_response(interaction: discord.Interaction) -> None:
    """Remove only this slash command's private deferred acknowledgement."""
    try:
        await interaction.delete_original_response()
    except discord.HTTPException:
        log.debug("A temporary command response could not be dismissed.")


def register_commands(bot: DJKhaled) -> None:
    @bot.tree.command(name="play", description="Play a song, playlist, or music link")
    @app_commands.guild_only()
    @app_commands.checks.cooldown(
        2, 10, key=lambda interaction: (interaction.guild_id, interaction.user.id)
    )
    @app_commands.describe(
        query="Song name or YouTube/Spotify/TIDAL/SoundCloud/audio URL",
        playlist="Allow YouTube playlist links",
    )
    async def play(interaction: discord.Interaction, query: str, playlist: bool = False) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        bot.validate_voice(interaction)
        tracks = await bot.media.resolve(query, playlist)
        if not tracks:
            raise UserError("No matching tracks were found.")
        player, count = await bot.enqueue(interaction, tracks)
        player.note(added_tracks(interaction.user, tracks, count))
        if await bot.publish(player, urgent=True):
            await dismiss_command_response(interaction)
        else:
            await interaction.edit_original_response(
                content="Music was queued, but the player card could not update yet. "
                "Check Send Messages and Embed Links permissions if this continues."
            )

    @bot.tree.command(name="search", description="Search YouTube and select a result")
    @app_commands.guild_only()
    @app_commands.checks.cooldown(
        2, 10, key=lambda interaction: (interaction.guild_id, interaction.user.id)
    )
    async def search(interaction: discord.Interaction, query: str) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        bot.validate_voice(interaction)
        query = query.strip()
        if not query:
            raise UserError("Enter a song title to search for.")
        if len(query) > 2000:
            raise UserError("Keep search queries under 2,000 characters.")
        tracks = await bot.media.search(query, 9)
        if not tracks:
            raise UserError("No matching tracks were found.")
        view = SearchView(bot, tracks, interaction.user.id, interaction.guild_id)
        embed = discord.Embed(
            title="Search: " + short(query, 180),
            description="Select one of the results below.",
            color=0xF4B942,
        )
        view.message = await interaction.followup.send(
            embed=embed, view=view, wait=True, ephemeral=True
        )

    @bot.tree.command(name="nowplaying", description="Show the current track and playback position")
    @app_commands.guild_only()
    async def nowplaying(interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        player = bot.players.get(interaction.guild_id)
        if not player or not player.current:
            raise UserError("There is no track playing in this server.")
        if await bot.publish(player, force=True, urgent=True):
            await dismiss_command_response(interaction)
        else:
            await interaction.edit_original_response(
                content="The player card could not update yet. "
                "Check Send Messages and Embed Links permissions if this continues."
            )

    @bot.tree.command(name="seek", description="Jump to a position in the current song")
    @app_commands.guild_only()
    @app_commands.checks.cooldown(
        2, 5, key=lambda interaction: (interaction.guild_id, interaction.user.id)
    )
    @app_commands.describe(position="Seconds, M:SS, or H:MM:SS; for example 90 or 1:30")
    async def seek(interaction: discord.Interaction, position: str) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        seconds = parse_position(position)
        player = bot.players.get(interaction.guild_id)
        if player is None or player.current is None:
            raise UserError("There is no track playing in this server.")
        expected_track = player.current
        bot.authorize(interaction, player, control=True)
        track = await player.seek(seconds, expected_track=expected_track)
        player.note(
            user_action(interaction.user, f"jumped to {duration_text(seconds)} in {track.title}.")
        )
        if await bot.publish(player, urgent=True):
            await dismiss_command_response(interaction)
        else:
            await interaction.edit_original_response(
                content="Playback moved, but the player card could not update yet. "
                "Check Send Messages and Embed Links permissions if this continues."
            )

    @bot.tree.command(name="help", description="Show DJ Khaled commands and playback controls")
    @app_commands.guild_only()
    async def help_command(interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        code = await bot.languages.code(interaction.guild_id)

        def text(key: str) -> str:
            return bot.languages.text(code, key)

        embed = discord.Embed(title=bot.settings.app_name, color=0xF4B942)
        embed.description = "Play music in your voice channel, build a queue, and control it below."
        embed.add_field(
            name=text("commands.help.commands_title"),
            value="/play · /search · /nowplaying · /seek · /help",
            inline=False,
        )
        embed.add_field(
            name=text("commands.help.button_controls_title"),
            value="**Pause / Resume** · **Skip** · **Queue** · **Disconnect**\n"
            "Open **More controls** for volume, seek, repeat, shuffle, autoplay, "
            "previous track, and lyrics.",
            inline=False,
        )
        embed.add_field(
            name="Loop, autoplay and lyrics",
            value="Repeat one track or the queue, select any of 20 autoplay genres, "
            "or open available lyrics. Volume supports 0–100%. "
            "Use /seek or More controls to jump to a position such as 1:30.",
            inline=False,
        )
        embed.add_field(
            name="Control permissions",
            value="Join the bot's voice channel to add songs. The current requester, a DJ, "
            "or someone with Manage Server can change playback. Anyone in that voice "
            "channel can view the queue and available lyrics.",
            inline=False,
        )
        embed.add_field(
            name="YouTube playback",
            value="Regular tracks try guest playback first. Configured YouTube cookies are "
            "retried for bot checks and age-restricted (18+) videos.",
            inline=False,
        )
        embed.add_field(
            name="TIDAL playback",
            value="Use /play with TIDAL or Spotify links. Audio comes from the bot owner's "
            "connected TIDAL account. Spotify tracks need a matching TIDAL recording; "
            "there is no automatic YouTube fallback.",
            inline=False,
        )
        media = getattr(bot, "media", None)
        label = getattr(media, "youtube_status_label", None)
        check = getattr(media, "youtube_status", None)
        if callable(label) and label() == "Checking..." and callable(check):
            try:
                async with asyncio.timeout(_YOUTUBE_HELP_TIMEOUT):
                    await check()
            except TimeoutError:
                pass
            except Exception:
                log.debug("YouTube sign-in status is unavailable.")
        add_youtube_status(
            embed, bot, checking_text="Sign-in check is still running. Open /help again shortly."
        )
        embed.set_footer(text=f"Python 3.14 · discord.py · {len(bot.guilds)} servers")
        await interaction.followup.send(embed=embed, ephemeral=True)
