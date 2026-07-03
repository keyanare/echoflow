# EchoFlow

A lightweight Discord music bot that plays audio from YouTube (single tracks, playlists, or search queries) directly in a voice channel.

## Features

- `/join` — connects the bot to your current voice channel
- `/leave` — disconnects the bot and clears the queue
- `/play <url>` — plays a track/playlist from a URL, or searches YouTube if a plain query is given
- `/skip` — skips the currently playing track
- `/stop` — stops playback and clears the queue

Tracks are queued per-server, and playback automatically advances to the next track when one finishes.

## Requirements

- Python 3.14+
- [uv](https://docs.astral.sh/uv/) for dependency management
- [FFmpeg](https://ffmpeg.org/) installed and available on your `PATH` (required for audio playback)
- A Discord bot application/token with the following:
  - **Privileged Gateway Intents:** none required beyond defaults (Guilds, Voice States)
  - **Scopes:** `bot`, `applications.commands`
  - **Bot Permissions:** Connect, Speak, Send Messages

## Setup

1. Clone the repository and install dependencies:

   ```bash
   uv sync
   ```

2. Copy the example environment file and add your bot token:

   ```bash
   cp .env.example .env
   ```

   ```
   TOKEN="your-discord-bot-token"
   ```

3. Run the bot:

   ```bash
   uv run main.py
   ```

On startup, the bot syncs its slash commands with Discord and logs a confirmation message once ready.

## Notes

- `yt-dlp` handles metadata extraction and streaming for both direct links and search queries (`ytsearch1:<query>`).
- Playlists are limited to the first 20 entries (`playlistend` in `YTDL_OPTIONS`).
