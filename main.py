import asyncio
import contextlib
import traceback
from os import getenv

import discord
from discord import app_commands
from discord.ext import commands
from discord.utils import MISSING
from dotenv import load_dotenv

from player import (
    GuildPlayer,
    PlayerManager,
    now_playing_embed,
    queue_embed,
    queue_pages,
)
from ytdl_source import TrackError, format_duration, search

load_dotenv()

EMPTY_CHANNEL_GRACE = 30
# How long the /queue and /nowplaying message keep their buttons live.
VIEW_TIMEOUT = 300

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


@bot.event
async def on_guild_remove(guild):
    player = bot.players.remove(guild.id)
    if player is not None:
        await player.destroy()


# --------------------------------------------------------------------- helpers


async def _reply(interaction, content=MISSING, *, embed=MISSING, view=MISSING, ephemeral=False):
    if interaction.response.is_done():
        await interaction.followup.send(content, embed=embed, view=view, ephemeral=ephemeral)
    else:
        await interaction.response.send_message(
            content, embed=embed, view=view, ephemeral=ephemeral
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


async def _redraw_controls(interaction: discord.Interaction, view: "NowPlayingView") -> None:
    """Redraw a control message in place, keeping the same view instance."""
    embed = now_playing_embed(view.player, detailed=True)
    if embed is None:
        with contextlib.suppress(discord.HTTPException):
            await interaction.response.edit_message(
                content="Nothing is playing.", embed=None, view=None
            )
        return
    view.sync()
    with contextlib.suppress(discord.HTTPException):
        await interaction.response.edit_message(embed=embed, view=view)


async def _refresh_panel(
    interaction: discord.Interaction, view: "NowPlayingView", *, delay: float = 0.0
) -> None:
    """Edit the panel in place *after* the interaction was answered elsewhere."""
    if delay:
        await asyncio.sleep(delay)
    message = interaction.message or view.message
    if message is None:
        return
    view.message = message
    embed = now_playing_embed(view.player, detailed=True)
    view.sync()
    with contextlib.suppress(discord.HTTPException):
        if embed is None:
            await message.edit(content="Nothing is playing.", embed=None, view=None)
        else:
            await message.edit(content=None, embed=embed, view=view)


async def _grey_out(message: discord.Message | None, view: discord.ui.View) -> None:
    """Leave the buttons visible but inert once a view timed out."""
    if message is None:
        return
    for child in view.walk_children():
        if isinstance(child, discord.ui.Button):
            child.disabled = True
    # The view is already stopped, so re-registering it is not required for the
    # edit to land; only the component payload has to be sent again.
    with contextlib.suppress(discord.HTTPException):
        await message.edit(view=view)


def _parse_timestamp(raw: str) -> float | None:
    """Parse ``90``, ``1:30`` or ``1:02:03`` into seconds."""
    raw = raw.strip().replace(",", ".")
    if not raw:
        return None
    parts = raw.split(":")
    if len(parts) > 3:
        return None
    total = 0.0
    for part in parts:
        try:
            value = float(part)
        except ValueError:
            return None
        if value < 0:
            return None
        total = total * 60 + value
    return total


# ------------------------------------------------------------------------- UI


class SeekModal(discord.ui.Modal):
    position = discord.ui.Label(
        text="Position",
        description="Seconds, or mm:ss / hh:mm:ss",
        component=discord.ui.TextInput(placeholder="1:30", max_length=9),
    )

    def __init__(self, player: GuildPlayer, view: "NowPlayingView") -> None:
        super().__init__(title="Rewind / seek", timeout=VIEW_TIMEOUT)
        self.player = player
        self.view = view
        # Start from where the track is, so a small correction is one edit away.
        current = player.elapsed
        if current >= 1:
            self.position.component.default = format_duration(current)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        raw = self.position.component.value
        target = _parse_timestamp(raw)
        track = self.player.current
        duration = track.duration if track else None

        if target is None:
            await interaction.response.send_message(
                "❌ That is not a timestamp — try `90`, `1:30` or `1:02:03`.",
                ephemeral=True,
            )
            return
        if duration and target >= duration:
            await interaction.response.send_message(
                f"❌ The track is only `{format_duration(duration)}` long.",
                ephemeral=True,
            )
            return
        if not self.player.seek(target):
            await interaction.response.send_message(
                "Nothing is playing.", ephemeral=True
            )
            return

        await interaction.response.send_message(
            f"⏩ Jumped to `{format_duration(target)}`.", ephemeral=True
        )
        # Refresh the panel once the new source is actually on air.
        await _refresh_panel(interaction, self.view, delay=1.0)


class NowPlayingView(discord.ui.View):
    """Pause, rewind and repeat controls for the current track."""

    def __init__(self, player: GuildPlayer) -> None:
        super().__init__(timeout=VIEW_TIMEOUT)
        self.player = player
        self.message: discord.Message | None = None
        self.sync()

    def sync(self) -> None:
        """Reflect the player state on the buttons."""
        self.pause.label = "Resume" if self.player.paused else "Pause"
        self.pause.emoji = "▶️" if self.player.paused else "⏸️"
        self.loop.style = (
            discord.ButtonStyle.success if self.player.loop else discord.ButtonStyle.secondary
        )
        self.seek.disabled = self.player.current is None

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.guild_id != self.player.guild.id:
            return False
        # Remember the panel this view is attached to so the seek modal can
        # refresh it afterwards.
        self.message = interaction.message
        return True

    async def on_timeout(self) -> None:
        await _grey_out(self.message, self)

    @discord.ui.button(label="Pause", emoji="⏸️", style=discord.ButtonStyle.secondary)
    async def pause(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.player.toggle_pause() is None:
            await interaction.response.send_message(
                "Nothing is playing.", ephemeral=True
            )
            return
        await _redraw_controls(interaction, self)

    @discord.ui.button(label="Seek", emoji="⏩", style=discord.ButtonStyle.secondary)
    async def seek(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.player.current is None:
            await interaction.response.send_message(
                "Nothing is playing.", ephemeral=True
            )
            return
        await interaction.response.send_modal(SeekModal(self.player, self))

    @discord.ui.button(label="Repeat", emoji="🔁", style=discord.ButtonStyle.secondary)
    async def loop(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.player.loop = not self.player.loop
        await _redraw_controls(interaction, self)

    @discord.ui.button(label="Skip", emoji="⏭️", style=discord.ButtonStyle.primary)
    async def skip(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not self.player.skip():
            await interaction.response.send_message(
                "Nothing is playing.", ephemeral=True
            )
            return
        await interaction.response.defer()
        # Keep the panel alive and let it pick up the next track instead of
        # deleting it — otherwise every skip costs the user a fresh /nowplaying.
        await _refresh_panel(interaction, self, delay=1.0)


class QueueView(discord.ui.View):
    """Prev/Next pagination for /queue, bound to the player it was opened on."""

    def __init__(self, player: GuildPlayer, page: int) -> None:
        super().__init__(timeout=VIEW_TIMEOUT)
        self.player = player
        self.page = page
        self.pages = 1
        self.message: discord.Message | None = None
        self.refresh()

    def refresh(self) -> None:
        self.pages = queue_pages(self.player)
        self.page = min(max(self.page, 1), self.pages)
        self.previous.disabled = self.page <= 1
        self.next.disabled = self.page >= self.pages

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.guild_id != self.player.guild.id:
            return False
        self.message = interaction.message
        return True

    async def on_timeout(self) -> None:
        await _grey_out(self.message, self)

    async def _show(self, interaction: discord.Interaction) -> None:
        self.refresh()
        embed, page, pages = queue_embed(self.player, self.page)
        self.page = page
        await interaction.response.edit_message(embed=embed, view=self)

    @discord.ui.button(label="Previous", emoji="◀️", style=discord.ButtonStyle.secondary)
    async def previous(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.page -= 1
        await self._show(interaction)

    @discord.ui.button(label="Next", emoji="▶️", style=discord.ButtonStyle.secondary)
    async def next(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.page += 1
        await self._show(interaction)

    @discord.ui.button(
        label="Refresh", emoji="🔄", style=discord.ButtonStyle.primary
    )
    async def refresh_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._show(interaction)


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


@bot.tree.command(name="pause", description="Pause or resume the current song")
@app_commands.guild_only()
async def pause(interaction: discord.Interaction):
    player = bot.players.get(interaction.guild)
    player.text_channel = interaction.channel

    state = player.toggle_pause()
    if state is None:
        await _reply(interaction, "Nothing is playing.", ephemeral=True)
    elif state:
        await _reply(interaction, "⏸️ Paused.")
    else:
        await _reply(interaction, "▶️ Resumed.")


@bot.tree.command(name="repeat", description="Toggle repeating the current song")
@app_commands.guild_only()
async def repeat(interaction: discord.Interaction):
    player = bot.players.get(interaction.guild)
    player.text_channel = interaction.channel

    player.loop = not player.loop
    await _reply(interaction, f"🔁 Repeat is now **{'on' if player.loop else 'off'}**.")


@bot.tree.command(name="seek", description="Jump to a position in the current song")
@app_commands.describe(position="Seconds, or mm:ss / hh:mm:ss")
@app_commands.guild_only()
async def seek(interaction: discord.Interaction, position: str):
    player = bot.players.get(interaction.guild)
    player.text_channel = interaction.channel

    target = _parse_timestamp(position)
    if target is None:
        await _reply(
            interaction,
            "❌ That is not a timestamp — try `90`, `1:30` or `1:02:03`.",
            ephemeral=True,
        )
        return

    duration = player.current.duration if player.current else None
    if duration and target >= duration:
        await _reply(
            interaction,
            f"❌ The track is only `{format_duration(duration)}` long.",
            ephemeral=True,
        )
        return

    if not player.seek(target):
        await _reply(interaction, "Nothing is playing.", ephemeral=True)
        return

    await _reply(interaction, f"⏩ Jumped to `{format_duration(target)}`.")


@bot.tree.command(name="queue", description="Show the song queue")
@app_commands.describe(page="Page of the queue to show (10 tracks per page)")
@app_commands.guild_only()
async def queue(interaction: discord.Interaction, page: int = 1):
    player = bot.players.get(interaction.guild)
    player.text_channel = interaction.channel

    if player.current is None and not player.queue:
        await _reply(interaction, "The queue is empty.", ephemeral=True)
        return

    embed, current, pages = queue_embed(player, page)
    if pages > 1:
        await _reply(interaction, embed=embed, view=QueueView(player, current))
    else:
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
    await _reply(interaction, embed=embed, view=NowPlayingView(player))


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
