import asyncio
import contextlib
import traceback
from os import getenv

import discord
from discord import app_commands
from discord.ext import commands
from discord.utils import MISSING
from dotenv import load_dotenv

from player import GuildPlayer, PlayerManager, now_playing_embed, queue_lines
from ytdl_source import TrackError, format_duration, search

load_dotenv()

EMPTY_CHANNEL_GRACE = 30
QUEUE_PAGE_SIZE = 10

intents = discord.Intents.default()
intents.guilds = True
intents.voice_states = True


class MusicBot(commands.Bot):
    def __init__(self):
        super().__init__(command_prefix=commands.when_mentioned, intents=intents)
        self.players = PlayerManager(self)

    async def setup_hook(self):
        await self.tree.sync()
        print("[+] Slash commands successfully synchronized with Discord!")

    async def close(self):
        await self.players.shutdown()
        await super().close()


bot = MusicBot()


@bot.event
async def on_ready():
    print(f"[+] {bot.user.name} successfully started.")


@bot.event
async def on_voice_state_update(member, before, after):
    if member.id == bot.user.id:
        await _on_own_voice_update(member, before, after)
        return

    if member.bot or before.channel is None or before.channel == after.channel:
        return

    guild = member.guild
    voice_client = guild.voice_client
    if voice_client is None or voice_client.channel != before.channel:
        return
    if any(not m.bot for m in before.channel.members):
        return

    await asyncio.sleep(EMPTY_CHANNEL_GRACE)

    voice_client = guild.voice_client
    if voice_client is None or voice_client.channel != before.channel:
        return
    if any(not m.bot for m in voice_client.channel.members):
        return

    player = bot.players.get(guild)
    await player.announce(
        "👋 Everyone left the voice channel — disconnecting, queue cleared."
    )
    await player.leave()


async def _on_own_voice_update(member, before, after):
    player = bot.players.peek(member.guild.id)
    if player is None:
        return

    if after.channel is None:
        if player.handle_forced_disconnect():
            await player.announce(
                "🔌 I was disconnected from the voice channel — queue cleared."
            )
        return

    player.channel = after.channel


# --------------------------------------------------------------------- helpers


async def _reply(interaction, content=MISSING, *, embed=MISSING, ephemeral=False):
    if interaction.response.is_done():
        await interaction.followup.send(content, embed=embed, ephemeral=ephemeral)
    else:
        await interaction.response.send_message(
            content, embed=embed, ephemeral=ephemeral
        )


async def _prepare(interaction, *, connect: bool = True) -> GuildPlayer | None:
    """Return the guild player, connecting to the caller's voice channel first."""
    player = bot.players.get(interaction.guild)
    player.text_channel = interaction.channel

    if not connect:
        return player

    user_channel = interaction.user.voice.channel if interaction.user.voice else None
    if not player.connected:
        if user_channel is None:
            await _reply(
                interaction,
                "You must be in a voice channel to use that command.",
                ephemeral=True,
            )
            return None
        try:
            await player.connect(user_channel)
        except (asyncio.TimeoutError, discord.ClientException) as exc:
            await _reply(interaction, f"❌ Could not join the voice channel: {exc}")
            return None

    return player


async def _enqueue(interaction, query: str, *, front: bool = False, now: bool = False):
    await interaction.response.defer()

    player = await _prepare(interaction)
    if player is None:
        return

    try:
        tracks, playlist_title = await search(query)
    except TrackError as exc:
        await _reply(interaction, f"❌ Could not load that: {exc}.")
        return

    for track in tracks:
        track.requester_id = interaction.user.id

    playing = player.current
    was_idle = playing is None
    player.add(tracks, front=front or now)

    if len(tracks) == 1:
        track = tracks[0]
        if now and not was_idle:
            prefix = "⏭️ Playing now"
        elif front and not was_idle:
            prefix = "⏫ Playing next"
        else:
            prefix = "➕ Added to queue"
        await _reply(
            interaction,
            f"{prefix}: **{track.title}** (`{format_duration(track.duration)}`)",
        )
    else:
        title = playlist_title or "Playlist"
        await _reply(
            interaction,
            f"➕ Added **{title}** — {len(tracks)} tracks "
            f"(`{format_duration(sum(t.duration or 0 for t in tracks))}`).",
        )

    # Only cut the track that was playing when the command was issued — it may
    # have ended on its own while the query was being resolved.
    if now and player.current is playing is not None:
        player.skip()


# -------------------------------------------------------------------- commands


@bot.tree.command(name="join", description="Connect the bot to a voice channel.")
@app_commands.guild_only()
async def join(interaction: discord.Interaction):
    if not interaction.user.voice:
        await _reply(
            interaction,
            "You must be in a voice channel to use that command.",
            ephemeral=True,
        )
        return

    await interaction.response.defer()
    player = bot.players.get(interaction.guild)
    player.text_channel = interaction.channel
    channel = interaction.user.voice.channel
    try:
        await player.connect(channel)
    except (asyncio.TimeoutError, discord.ClientException) as exc:
        await _reply(interaction, f"❌ Could not join the voice channel: {exc}")
        return

    await _reply(interaction, f"✅ Connected to: **{channel.name}**")


@bot.tree.command(name="leave", description="Disconnect the bot from the voice channel")
@app_commands.guild_only()
async def leave(interaction: discord.Interaction):
    await interaction.response.defer()
    player = bot.players.get(interaction.guild)
    player.text_channel = interaction.channel

    if await player.leave():
        await _reply(interaction, "👋 Left the voice channel, queue cleared.")
    else:
        await _reply(interaction, "I am not in a voice channel.", ephemeral=True)


@bot.tree.command(name="play", description="Play a track or playlist, or search")
@app_commands.describe(query="Link to a video, a playlist, or a search query")
@app_commands.guild_only()
async def play(interaction: discord.Interaction, query: str):
    await _enqueue(interaction, query)


@bot.tree.command(name="playnext", description="Add a track to the front of the queue")
@app_commands.describe(query="Link to a video, a playlist, or a search query")
@app_commands.guild_only()
async def playnext(interaction: discord.Interaction, query: str):
    await _enqueue(interaction, query, front=True)


@bot.tree.command(
    name="playnow", description="Play a track immediately, skipping the current one"
)
@app_commands.describe(query="Link to a video, a playlist, or a search query")
@app_commands.guild_only()
async def playnow(interaction: discord.Interaction, query: str):
    await _enqueue(interaction, query, now=True)


@bot.tree.command(name="skip", description="Skip the current song")
@app_commands.guild_only()
async def skip(interaction: discord.Interaction):
    player = bot.players.get(interaction.guild)
    player.text_channel = interaction.channel

    title = player.current.title if player.current else None
    if player.skip():
        await _reply(interaction, f"⏭️ Skipped **{title}**.")
    else:
        await _reply(interaction, "Nothing is playing.", ephemeral=True)


@bot.tree.command(name="stop", description="Stop playback and clear the queue")
@app_commands.guild_only()
async def stop(interaction: discord.Interaction):
    player = bot.players.get(interaction.guild)
    player.text_channel = interaction.channel

    removed = len(player.queue)
    await player.stop()
    await _reply(interaction, f"⏹️ Playback stopped, {removed} queued track(s) cleared.")


@bot.tree.command(name="queue", description="Show the song queue")
@app_commands.describe(page="Page of the queue to show (10 tracks per page)")
@app_commands.guild_only()
async def queue(interaction: discord.Interaction, page: int = 1):
    player = bot.players.get(interaction.guild)
    player.text_channel = interaction.channel

    if player.current is None and not player.queue:
        await _reply(interaction, "The queue is empty.", ephemeral=True)
        return

    pages = max(1, -(-len(player.queue) // QUEUE_PAGE_SIZE))
    page = min(max(page, 1), pages)
    start = (page - 1) * QUEUE_PAGE_SIZE
    chunk = player.queue[start : start + QUEUE_PAGE_SIZE]

    embed = discord.Embed(title="🎶 Queue", colour=discord.Colour.blurple())
    if player.current is not None:
        embed.add_field(
            name="Now playing",
            value=(
                f"{player.current.display}\n"
                f"`{format_duration(player.elapsed)} / "
                f"{format_duration(player.current.duration)}`"
            ),
            inline=False,
        )
    if chunk:
        embed.add_field(
            name="Up next",
            value="\n".join(queue_lines(chunk, start + 1)),
            inline=False,
        )
        embed.set_footer(
            text=(
                f"Page {page}/{pages} · {len(player.queue)} track(s) · "
                f"{format_duration(player.total_duration)} total"
            )
        )
    else:
        embed.set_footer(text="Nothing else is queued")

    await _reply(interaction, embed=embed)


@bot.tree.command(name="move", description="Move a queued track to another position")
@app_commands.describe(
    position="Current position of the track in the queue",
    destination="Position to move it to",
)
@app_commands.guild_only()
async def move(interaction: discord.Interaction, position: int, destination: int):
    player = bot.players.get(interaction.guild)
    player.text_channel = interaction.channel

    size = len(player.queue)
    if size == 0:
        await _reply(interaction, "The queue is empty.", ephemeral=True)
        return
    if not 1 <= position <= size or not 1 <= destination <= size:
        await _reply(
            interaction,
            f"Positions must be between 1 and {size}.",
            ephemeral=True,
        )
        return

    track = player.move(position - 1, destination - 1)
    await _reply(
        interaction, f"↕️ Moved **{track.title}** to position **{destination}**."
    )


@bot.tree.command(name="nowplaying", description="Show the currently playing track")
@app_commands.guild_only()
async def nowplaying(interaction: discord.Interaction):
    player = bot.players.get(interaction.guild)
    player.text_channel = interaction.channel

    embed = now_playing_embed(player, detailed=True)
    if embed is None:
        await _reply(interaction, "Nothing is playing.", ephemeral=True)
        return
    await _reply(interaction, embed=embed)


@bot.tree.error
async def on_app_command_error(
    interaction: discord.Interaction, error: app_commands.AppCommandError
):
    original = getattr(error, "original", error)
    traceback.print_exception(type(original), original, original.__traceback__)

    message = (
        f"❌ {original}"
        if isinstance(original, TrackError)
        else "❌ Something went wrong while running that command."
    )
    with contextlib.suppress(discord.HTTPException, discord.InteractionResponded):
        await _reply(interaction, message, ephemeral=True)


def main():
    token = getenv("TOKEN")
    if not token:
        raise SystemExit("[!] TOKEN is not set — copy .env.example to .env first.")
    bot.run(token)


if __name__ == "__main__":
    main()
