# EchoFlow

A lightweight Discord music bot that plays audio from YouTube (single tracks, playlists, or search queries) directly in a voice channel.

## Features

**Playback**

- `/join` — connects the bot to your current voice channel
- `/leave` — disconnects the bot and clears the queue
- `/play <query>` — plays a track/playlist from a URL, or searches YouTube if a plain query is given
- `/skip` — skips the currently playing track
- `/stop` — stops playback and clears the queue

**Queue**

- `/queue [page]` — shows the current track and what is coming next, 10 per page
- `/playnext <query>` — puts a track at the front of the queue, right after the current one
- `/playnow <query>` — plays a track immediately, dropping the one that was playing
- `/move <position> <destination>` — reorders a queued track (positions as shown by `/queue`)
- `/nowplaying` — shows the current track with a progress bar

Tracks are queued per-server, and playback automatically advances to the next track when one finishes.

## Reliability

- **Dropped voice connections are repaired automatically.** A watchdog supervises every active
  player; if the voice connection stays down for 15 seconds it is rebuilt from scratch and the
  current track resumes from the position it stopped at. A playback loop that dies for any other
  reason is caught and reported instead of leaving the bot silently unresponsive.
- **Stream links are resolved at playback time, never when queueing.** The queue only stores page
  URLs, so links cannot expire while they wait. Every resolved stream is verified with a real
  download before ffmpeg touches it — YouTube answers small range requests with 200 even for links
  it refuses to serve (SABR), so only reading the body reveals a dead one. Rejected links (the
  usual `403 Forbidden`) are retried against other YouTube player clients.
- **Streams that die mid-track are recovered automatically.** A track that cuts out seconds after
  starting is re-resolved against another player client and resumes from where it stopped, instead
  of being silently skipped.
- **Failures always answer.** Extraction is bounded by a timeout and every command reports what
  went wrong, so a track that cannot be fetched produces an error message rather than a command
  that never finishes.

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

## Running with Docker

Alternatively, you can run the bot in a container — FFmpeg and Opus are already included in the image, so no local dependencies are needed beyond Docker itself.

With Compose (recommended — build, run and restart policy in one command):

```bash
cp .env.example .env   # then put your token in it
docker compose up -d --build
```

`docker compose logs -f` follows the logs, `docker compose down` stops the bot, and
`docker compose up -d --build` again picks up code changes.

Plain Docker works too:

```bash
docker build -t echoflow .
docker run -d --name echoflow --restart unless-stopped --env-file .env echoflow
```

## Notes

- `yt-dlp` handles metadata extraction and streaming for both direct links and search queries (`ytsearch1:<query>`).
- Playlists are limited to the first 100 entries (`MAX_PLAYLIST_ITEMS` in `ytdl_source.py`).
- The code is split into `main.py` (slash commands), `player.py` (per-guild queue, playback loop
  and connection watchdog) and `ytdl_source.py` (yt-dlp extraction and stream resolution).
