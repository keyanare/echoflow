"""Per-guild playback: queue, playback loop and voice connection supervision."""

from __future__ import annotations

import asyncio
import contextlib
import time
import traceback
from typing import Iterable, Sequence

import discord

from ytdl_source import Track, TrackError, create_source, format_duration

WATCHDOG_INTERVAL = 5.0
# How long the voice connection may stay down before we rebuild it ourselves.
RECONNECT_GRACE = 15.0
CONNECT_TIMEOUT = 20.0
DISCONNECT_TIMEOUT = 10.0
# Consecutive health checks with a live connection but no audio before we advance.
STALL_LIMIT = 2
# Voice state updates arriving this soon after our own disconnect are our doing.
SELF_DISCONNECT_WINDOW = 5.0

FINISHED = "finished"
RESUME = "resume"
STOPPED = "stopped"
SKIPPED = "skipped"

# A track that dies this soon after starting almost certainly hit a dead stream
# link rather than finishing; the stream is re-resolved and playback resumes
# from where it stopped instead of silently moving on to the next track.
EARLY_DEATH_WINDOW = 15.0
MAX_STREAM_RETRIES = 2


class GuildPlayer:
    """Owns the queue and the playback loop of a single guild."""

    def __init__(self, bot: discord.Client, guild: discord.Guild) -> None:
        self.bot = bot
        self.guild = guild
        self.queue: list[Track] = []
        self.current: Track | None = None
        self.text_channel: discord.abc.Messageable | None = None
        self.channel: discord.abc.Connectable | None = None
        self.volume = 0.5

        self._wake = asyncio.Event()
        self._next = asyncio.Event()
        self._finish_reason: str | None = None
        self._lock = asyncio.Lock()

        self._started_at = 0.0
        self._offset = 0.0
        self._generation = 0
        self._stall_generation = -1
        self._stalled_checks = 0
        self._down_since: float | None = None
        self._awaiting_end = False
        self._recovering = False
        self._self_disconnect_at = 0.0

        self._tasks = [
            bot.loop.create_task(self._run(), name=f"player-{guild.id}"),
            bot.loop.create_task(self._watch(), name=f"watchdog-{guild.id}"),
        ]

    # ------------------------------------------------------------------ state

    @property
    def voice_client(self) -> discord.VoiceClient | None:
        return self.guild.voice_client  # type: ignore[return-value]

    @property
    def connected(self) -> bool:
        voice_client = self.voice_client
        return voice_client is not None and voice_client.is_connected()

    @property
    def elapsed(self) -> float:
        if self.current is None or not self._awaiting_end:
            return 0.0
        return self._offset + max(0.0, time.monotonic() - self._started_at)

    @property
    def total_duration(self) -> float:
        return sum(track.duration or 0 for track in self.queue)

    # ------------------------------------------------------------------ queue

    def add(self, tracks: Iterable[Track], *, front: bool = False) -> None:
        items = list(tracks)
        if not items:
            return
        if front:
            self.queue[:0] = items
        else:
            self.queue.extend(items)
        self._wake.set()

    def move(self, source: int, target: int) -> Track:
        """Move a queued track between 0-based positions. Caller validates bounds."""
        track = self.queue.pop(source)
        self.queue.insert(target, track)
        return track

    def clear(self) -> int:
        removed = len(self.queue)
        self.queue.clear()
        return removed

    def skip(self) -> bool:
        if self.current is None:
            return False
        voice_client = self.voice_client
        if voice_client is not None:
            with contextlib.suppress(Exception):
                voice_client.stop()
        # Advance even if the audio thread is wedged and never fires its callback.
        self._finish(SKIPPED)
        return True

    async def stop(self) -> None:
        self.clear()
        self.skip()

    # ------------------------------------------------------------- connection

    async def connect(self, channel: discord.abc.Connectable) -> discord.VoiceClient:
        async with self._lock:
            voice_client = self.voice_client
            if voice_client is not None and voice_client.is_connected():
                if voice_client.channel != channel:
                    await voice_client.move_to(channel)
            else:
                if voice_client is not None:
                    await self._tear_down(voice_client)
                voice_client = await channel.connect(
                    timeout=CONNECT_TIMEOUT, reconnect=True, self_deaf=True
                )
            self.channel = channel
            return voice_client  # type: ignore[return-value]

    async def leave(self) -> bool:
        """Disconnect for real and report whether the bot actually left."""
        self.clear()
        self._self_disconnect_at = time.monotonic()
        self._finish_reason = STOPPED

        left = False
        async with self._lock:
            voice_client = self.voice_client
            if voice_client is not None:
                await self._tear_down(voice_client)
                left = True

            # A half-dead session can leave the bot sitting in the channel with no
            # usable voice client; tell the gateway to drop it explicitly.
            me = self.guild.me
            if me is not None and me.voice is not None:
                with contextlib.suppress(Exception):
                    await self.guild.change_voice_state(channel=None)
                left = True

        self.channel = None
        self._finish(STOPPED)
        return left

    async def _tear_down(self, voice_client: discord.VoiceClient) -> None:
        # Marks the disconnect as self-inflicted so the voice state update it
        # triggers is not mistaken for the bot being kicked out.
        self._self_disconnect_at = time.monotonic()
        with contextlib.suppress(Exception):
            voice_client.stop()
        try:
            await asyncio.wait_for(
                voice_client.disconnect(force=True), timeout=DISCONNECT_TIMEOUT
            )
        except Exception:
            with contextlib.suppress(Exception):
                voice_client.cleanup()

    async def _reconnect(self) -> discord.VoiceClient | None:
        """Rebuild the voice connection from scratch. Caller must hold the lock."""
        channel = self.channel
        if channel is None:
            return None

        voice_client = self.voice_client
        if voice_client is not None:
            await self._tear_down(voice_client)

        try:
            return await channel.connect(
                timeout=CONNECT_TIMEOUT, reconnect=True, self_deaf=True
            )
        except Exception as exc:
            print(f"[!] Reconnect to {channel} failed: {exc}")
            return None

    # -------------------------------------------------------------- playback

    async def _run(self) -> None:
        while True:
            try:
                while not self.queue:
                    self._wake.clear()
                    await self._wake.wait()

                self.current = self.queue.pop(0)
                await self._play(self.current)
            except asyncio.CancelledError:
                raise
            except Exception:
                # Never let one bad track kill the loop — that is what leaves the
                # bot answering commands while no music ever plays again.
                traceback.print_exc()
                if self.current is not None:
                    title = self.current.title
                    await self.announce(f"⚠️ Skipping **{title}** — playback error.")
                await asyncio.sleep(1)
            finally:
                self.current = None
                self._awaiting_end = False

    async def _play(self, track: Track) -> None:
        seek = 0.0
        retries = 0

        while True:
            self._next.clear()
            self._finish_reason = None

            async with self._lock:
                voice_client = self.voice_client
                if voice_client is None or not voice_client.is_connected():
                    voice_client = await self._reconnect()
                if voice_client is None:
                    self.clear()
                    await self.announce(
                        "🔌 Lost the voice connection and could not restore it — "
                        "the queue has been cleared."
                    )
                    return

                try:
                    source = await create_source(track, seek=seek, volume=self.volume)
                except TrackError as exc:
                    await self.announce(f"⚠️ Skipping **{track.title}** — {exc}.")
                    return

                await self._await_idle(voice_client)
                try:
                    voice_client.play(source, after=self._after)
                except Exception as exc:
                    source.cleanup()
                    await self.announce(f"⚠️ Could not start **{track.title}** — {exc}.")
                    return

                self._offset = seek
                self._started_at = time.monotonic()
                self._generation += 1
                self._awaiting_end = True

            if seek == 0.0:
                await self.announce(embed=now_playing_embed(self))

            await self._next.wait()
            played_for = max(0.0, time.monotonic() - self._started_at)
            self._awaiting_end = False
            position = seek + played_for

            if self._finish_reason == RESUME:
                seek = position
                continue

            died_young = played_for < EARLY_DEATH_WINDOW and (
                track.duration is None or position < track.duration * 0.9
            )
            if self._finish_reason == FINISHED and died_young:
                if retries < MAX_STREAM_RETRIES:
                    retries += 1
                    seek = position
                    await self.announce(
                        "🔄 The stream cut out — picking up where it left off."
                    )
                    continue
                await self.announce(f"⚠️ **{track.title}** keeps cutting out — skipping.")
            return

    async def _await_idle(self, voice_client: discord.VoiceClient) -> None:
        """Give a previous source a moment to release the voice client."""
        for _ in range(20):
            if not (voice_client.is_playing() or voice_client.is_paused()):
                return
            with contextlib.suppress(Exception):
                voice_client.stop()
            await asyncio.sleep(0.1)

    def _after(self, error: Exception | None) -> None:
        if error is not None:
            print(f"[!] Playback error in guild {self.guild.id}: {error}")
        self.bot.loop.call_soon_threadsafe(self._finish, FINISHED)

    def _finish(self, reason: str) -> None:
        if self._finish_reason is None:
            self._finish_reason = reason
        self._next.set()

    # -------------------------------------------------------------- watchdog

    async def _watch(self) -> None:
        while True:
            await asyncio.sleep(WATCHDOG_INTERVAL)
            try:
                await self._check_health()
            except asyncio.CancelledError:
                raise
            except Exception:
                traceback.print_exc()

    async def _check_health(self) -> None:
        if self._recovering:
            return

        if not self._awaiting_end:
            self._down_since = None
            self._stalled_checks = 0
            if self.queue and self.current is None:
                self._wake.set()
            return

        voice_client = self.voice_client
        if voice_client is not None and voice_client.is_connected():
            self._down_since = None
            if voice_client.is_playing() or voice_client.is_paused():
                self._stalled_checks = 0
                self._stall_generation = self._generation
                return
            # Connected, a track is on air, yet no audio is being pushed: the
            # player thread died without ever firing its completion callback.
            if self._stall_generation != self._generation:
                self._stall_generation = self._generation
                self._stalled_checks = 0
                return
            self._stalled_checks += 1
            if self._stalled_checks >= STALL_LIMIT:
                self._stalled_checks = 0
                self._finish(FINISHED)
            return

        now = time.monotonic()
        if self._down_since is None:
            self._down_since = now
            return
        if now - self._down_since >= RECONNECT_GRACE:
            self._down_since = None
            await self._recover()

    async def _recover(self) -> None:
        """Rebuild a dropped connection and resume the track where it stopped."""
        self._recovering = True
        # Claim the finish reason before tearing anything down, so the callback
        # fired by the disconnect cannot turn this into a "track finished".
        self._finish_reason = RESUME
        try:
            async with self._lock:
                voice_client = await self._reconnect()
            if voice_client is None:
                self._finish_reason = STOPPED
                self.clear()
                await self.announce(
                    "🔌 Lost the voice connection and could not restore it — "
                    "the queue has been cleared."
                )
                self._finish(STOPPED)
                return
            await self.announce("🔄 Voice connection dropped — reconnected, resuming.")
            self._finish(RESUME)
        finally:
            self._recovering = False

    def handle_forced_disconnect(self) -> bool:
        """The bot was removed from voice by someone else; True if we acted on it."""
        if self._recovering:
            return False
        if time.monotonic() - self._self_disconnect_at < SELF_DISCONNECT_WINDOW:
            return False
        self.channel = None
        self._finish_reason = STOPPED
        was_playing = self.current is not None or bool(self.queue)
        self.clear()
        self._finish(STOPPED)
        return was_playing

    # ------------------------------------------------------------------- misc

    async def announce(self, content: str | None = None, *, embed=None) -> None:
        channel = self.text_channel
        if channel is None:
            return
        with contextlib.suppress(discord.HTTPException, discord.Forbidden):
            await channel.send(content, embed=embed)

    async def destroy(self) -> None:
        for task in self._tasks:
            task.cancel()
        self._tasks.clear()
        with contextlib.suppress(Exception):
            await self.leave()


class PlayerManager:
    def __init__(self, bot: discord.Client) -> None:
        self.bot = bot
        self._players: dict[int, GuildPlayer] = {}

    def get(self, guild: discord.Guild) -> GuildPlayer:
        player = self._players.get(guild.id)
        if player is None:
            player = GuildPlayer(self.bot, guild)
            self._players[guild.id] = player
        return player

    def peek(self, guild_id: int | None) -> GuildPlayer | None:
        if guild_id is None:
            return None
        return self._players.get(guild_id)

    def remove(self, guild_id: int) -> GuildPlayer | None:
        return self._players.pop(guild_id, None)

    async def shutdown(self) -> None:
        players = list(self._players.values())
        self._players.clear()
        for player in players:
            await player.destroy()


def progress_bar(elapsed: float, total: float | None, length: int = 18) -> str:
    if not total:
        return ""
    ratio = min(max(elapsed / total, 0.0), 1.0)
    filled = int(length * ratio)
    return "".join(("▬" * filled, "🔘", "▬" * max(0, length - filled - 1)))


def now_playing_embed(player: GuildPlayer, *, detailed: bool = False):
    track = player.current
    if track is None:
        return None

    embed = discord.Embed(
        title="Now playing",
        description=track.display,
        colour=discord.Colour.blurple(),
    )
    if detailed:
        elapsed = player.elapsed
        bar = progress_bar(elapsed, track.duration)
        position = f"{format_duration(elapsed)} / {format_duration(track.duration)}"
        embed.add_field(
            name="​", value=f"{bar}\n`{position}`" if bar else f"`{position}`"
        )
    else:
        embed.add_field(name="Duration", value=format_duration(track.duration))

    if track.requester_id:
        embed.add_field(name="Requested by", value=f"<@{track.requester_id}>")
    if player.queue:
        embed.set_footer(text=f"{len(player.queue)} track(s) up next")
    return embed


def queue_lines(tracks: Sequence[Track], start: int) -> list[str]:
    return [
        f"`{start + offset}.` {track.display} — `{format_duration(track.duration)}`"
        for offset, track in enumerate(tracks)
    ]
