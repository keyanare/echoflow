import asyncio
from collections import defaultdict
from os import getenv

import discord
import yt_dlp
from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv

load_dotenv()

intents = discord.Intents.default()
intents.guilds = True
intents.voice_states = True

YTDL_OPTIONS = {
    "format": "bestaudio/best",
    "noplaylist": False,
    "nocheckcertificate": True,
    "ignoreerrors": False,
    "logtostderr": False,
    "quiet": True,
    "no_warnings": True,
    "default_search": "auto",
    "source_address": "0.0.0.0",
    "extract_flat": "in_playlist",
    "playlistend": 20,
}

FFMPEG_OPTIONS = {
    "before_options": "-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5",
    "options": "-vn",
}

ytdl = yt_dlp.YoutubeDL(YTDL_OPTIONS)


class MusicBot(commands.Bot):
    def __init__(self):
        super().__init__(command_prefix="!", intents=intents)

    async def setup_hook(self):
        await self.tree.sync()
        print("[+] Slash commands successfully synchronized with Discord!")


class YTDLSource(discord.PCMVolumeTransformer):
    def __init__(self, source, *, data, volume=0.5):
        super().__init__(source, volume)
        self.data = data
        self.title = data.get("title")
        self.url = data.get("url")

    @classmethod
    async def from_url(cls, url, *, loop=None):
        loop = loop or asyncio.get_event_loop()
        with yt_dlp.YoutubeDL(
            {"format": "bestaudio/best", "quiet": True}
        ) as ydl_single:
            data = await loop.run_in_executor(
                None, lambda: ydl_single.extract_info(url, download=False)
            )
        filename = data["url"]
        return cls(discord.FFmpegPCMAudio(filename, **FFMPEG_OPTIONS), data=data)


bot = MusicBot()
song_queues = defaultdict(list)


@bot.event
async def on_ready():
    print(self_name := f"[+] {bot.user.name} successfully started.")


async def _play_next_async(interaction: discord.Interaction):
    guild_id = interaction.guild_id
    queue = song_queues[guild_id]
    voice_client = interaction.guild.voice_client

    if not voice_client or not voice_client.is_connected():
        queue.clear()
        return

    if len(queue) > 0:
        next_track = queue.pop(0)
        url = next_track["url"]
        title = next_track["title"]

        try:
            player = await YTDLSource.from_url(url, loop=bot.loop)

            if not voice_client or not voice_client.is_connected():
                queue.clear()
                return

            voice_client.play(player, after=lambda e: play_next(interaction))
            await interaction.channel.send(f"**Now playing:** {title}")
        except Exception as e:
            print(f"Playback error: {e}")
            await interaction.channel.send(f"Can't play: {title}")

            if voice_client and voice_client.is_connected():
                await _play_next_async(interaction)
            else:
                queue.clear()


def play_next(interaction: discord.Interaction):
    asyncio.run_coroutine_threadsafe(_play_next_async(interaction), bot.loop)


@bot.tree.command(name="join", description="Connect the bot to a voice channel.")
async def join(interaction: discord.Interaction):
    if not interaction.author.voice:
        await interaction.response.send_message(
            "You must be in voice channel to use that command."
        )
        return
    channel = interaction.user.voice.channel
    if interaction.guild.voice_client is not None:
        return await interaction.guild.voice_client.move_to(channel)
    else:
        await channel.connect()

    await interaction.response.send_message(f"Connected to: **{channel.name}**")


@bot.tree.command(name="leave", description="Disconnect bot from voice channel")
async def leave(interaction: discord.Interaction):
    guild_id = interaction.guild_id
    song_queues[guild_id] = []
    voice_client = interaction.guild.voice_client
    if voice_client:
        voice_client.stop()
        await voice_client.disconnect()
        await interaction.response.send_message("The bot left the channel.")
    else:
        await interaction.response.send_message("I am not in the voice channel")


@bot.tree.command(name="play", description="Play the track or playlist from link")
@app_commands.describe(url="Link to a video, playlist or search query")
async def play(interaction: discord.Interaction, url: str):
    await interaction.response.defer()

    voice_client = interaction.guild.voice_client
    if not voice_client:
        if not interaction.user.voice:
            await interaction.followup.send("You must be in voice channel")
            return
        channel = interaction.user.voice.channel
        voice_client = await channel.connect()

    guild_id = interaction.guild_id
    loop = bot.loop

    if guild_id not in song_queues:
        song_queues[guild_id] = []

    is_search = False
    if not url.startswith(("http://", "https://")):
        search_query = f"ytsearch1:{url}"
        is_search = True
        data = await loop.run_in_executor(
            None, lambda: ytdl.extract_info(search_query, download=False)
        )
    else:
        data = await loop.run_in_executor(
            None, lambda: ytdl.extract_info(url, download=False)
        )

    if data is None:
        await interaction.followup.send("Cannot load information, please try again.")
        return

    tracks_to_add = []

    if "entries" in data:
        entries = list(data["entries"])

        if is_search and len(entries) == 1:
            entry = entries[0]
            if entry:
                tracks_to_add.append(
                    {
                        "url": entry.get("url")
                        or f"https://www.youtube.com/watch?v={entry.get('id')}",
                        "title": entry.get("title", "Untitled"),
                    }
                )
        else:
            for entry in entries:
                if entry:
                    video_url = (
                        entry.get("url")
                        or f"https://www.youtube.com/watch?v={entry.get('id')}"
                    )
                    tracks_to_add.append(
                        {"url": video_url, "title": entry.get("title", "Untitled")}
                    )
    else:
        video_url = data.get("webpage_url") or data.get("url")
        tracks_to_add.append({"url": video_url, "title": data.get("title", "Untitled")})

    if not tracks_to_add:
        await interaction.followup.send("No results found for your query.")
        return

    song_queues[guild_id].extend(tracks_to_add)

    if len(tracks_to_add) == 1:
        await interaction.followup.send(
            f"Added to queue: **{tracks_to_add[0]['title']}**"
        )
    else:
        playlist_title = data.get("title", "Playlist")
        await interaction.followup.send(
            f"Found and added to queue: **{playlist_title}** (Added {len(tracks_to_add)} songs)."
        )

    if not voice_client.is_playing() and not voice_client.is_paused():
        await _play_next_async(interaction)


@bot.tree.command(name="skip", description="Skip current song")
async def skip(interaction: discord.Interaction):
    voice_client = interaction.guild.voice_client
    if voice_client and voice_client.is_playing():
        voice_client.stop()
        await interaction.response.send_message("Track skipped.")
    else:
        await interaction.response.send_message("Nothing is playing", ephemeral=True)


@bot.tree.command(name="stop", description="Stop music and clear queue")
async def stop(interaction: discord.Interaction):
    guild_id = interaction.guild_id
    song_queues[guild_id] = []
    voice_client = interaction.guild.voice_client
    if voice_client:
        voice_client.stop()
    await interaction.response.send_message("Playback is stopped, queue cleared.")


TOKEN = getenv("TOKEN")
bot.run(TOKEN)
