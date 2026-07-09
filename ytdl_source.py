import asyncio

import discord
import yt_dlp

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
