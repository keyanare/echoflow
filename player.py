"""Per-guild playback: queue, playback loop and voice connection supervision."""

from __future__ import annotations

import asyncio
import contextlib
import time
import traceback
from functools import partial
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
# How many times in a row a track may be resumed after a reconnect before we give up.
MAX_RESUME_ATTEMPTS = 5

FINISHED = "finished"
RESUME = "resume"
STOPPED = "stopped"
SKIPPED = "skipped"
SEEK = "seek"

# A track that dies this soon after starting almost certainly hit a dead stream
# link rather than finishing; the stream is re-resolved and playback resumes
# from where it stopped instead of silently moving on to the next track.
EARLY_DEATH_WINDOW = 15.0
MAX_STREAM_RETRIES = 2

# Tracks per /queue page and the character budget of a single embed field.
QUEUE_PAGE_SIZE = 10
QUEUE_FIELD_BUDGET = 1000


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
        # Repeat the current track instead of advancing when it ends.
        self.loop = False

        self._wake = asyncio.Event()
        self._next = asyncio.Event()
        self._finish_reason: str | None = None
        self._lock = asyncio.Lock()

        self._started_at = 0.0
        self._offset = 0.0
        self._paused_at = 0.0
        self._paused = False

        # Every `voice_client.play` gets its own token. The completion callback
        # carries the token it was started with, so a callback fired by a source
        # that has already been replaced can never finish the *next* track.
        self._play_token = 0
        self._active_token: int | None = None
        # Number of forced-order requests (skip / stop / leave) that have not
        # been acted on yet, and a counter used to abandon a track that is still
        # being resolved when the queue is wiped.
        self._skips = 0
        self._abort_seq = 0
        self._seek_to = 0.0

        self._stall_token = -1
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
    def paused(self) -> bool:
        return self._paused

    @property
    def elapsed(self) -> float:
        if self.current is None or not self._awaiting_end:
            return 0.0
        if self._paused:
            return self._offset + max(0.0, self._paused_at - self._started_at)
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
        """Advance past the current track, even if it is still being resolved."""
        if self.current is None:
            return False
        if self._awaiting_end and self._finish_reason == FINISHED:
            # The track already ended on its own and the loop is about to move
            # on, so that advance satisfies this request; counting it again
            # would swallow the next track.
            return True

        # Each request is consumed exactly once: either when a track that was
        # still being resolved is dropped before it starts, or when an on-air
        # track ends with SKIPPED. That is what keeps /skip from eating two
        # tracks while still honouring a double press.
        self._skips += 1
        voice_client = self.voice_client
        if voice_client is not None:
            with contextlib.suppress(Exception):
                voice_client.stop()
        # A no-op while nothing is on air; the counter above covers that case.
        self._finish(SKIPPED)
        return True

    async def stop(self) -> None:
        self.clear()
        self._abort(STOPPED)

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
        # Abort first: the watchdog must never see a "dropped" connection for a
        # disconnect we asked for ourselves.
        self._abort(STOPPED)
        self._self_disconnect_at = time.monotonic()

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
                    # Nothing is on air, so a pending skip request is stale: it
                    # must not swallow the next track somebody queues.
                    self._skips = 0
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
                self._active_token = None
                self._paused = False
                # Releases a task cancelled while waiting for the next track.
                self._wake.set()

    async def _play(self, track: Track) -> None:
        seek = 0.0
        retries = 0
        resumptions = 0
        replaying = False

        while True:
            self._next.clear()
            self._finish_reason = None
            # Snapshot the abort counter: if the queue is wiped while we resolve
            # this track, its result is thrown away instead of started.
            abort_seq = self._abort_seq
            announce_track = not replaying

            # Resolve the stream outside the lock: yt-dlp can take tens of
            # seconds, and holding the lock that long would stall /leave,
            # /stop and the watchdog's reconnect.
            try:
                source = await create_source(track, seek=seek, volume=self.volume)
            except TrackError as exc:
                await self.announce(f"⚠️ Skipping **{track.title}** — {exc}.")
                return

            # A forced order arrived while the track was resolving: drop it and
            # let the loop pick up whatever the command left behind instead of
            # starting this track (or reconnecting for it).
            if self._skips > 0 or self._abort_seq != abort_seq:
                source.cleanup()
                if self._skips > 0:
                    self._skips -= 1
                return

            async with self._lock:
                voice_client = self.voice_client
                if voice_client is None or not voice_client.is_connected():
                    voice_client = await self._reconnect()
                if voice_client is None:
                    source.cleanup()
                    self.clear()
                    await self.announce(
                        "🔌 Lost the voice connection and could not restore it — "
                        "the queue has been cleared."
                    )
                    return

                await self._await_idle(voice_client)

                if self._skips > 0 or self._abort_seq != abort_seq:
                    source.cleanup()
                    if self._skips > 0:
                        self._skips -= 1
                    return

                self._play_token += 1
                token = self._play_token
                self._active_token = token
                self._offset = seek
                self._started_at = time.monotonic()
                self._paused = False
                self._awaiting_end = True
                try:
                    voice_client.play(source, after=partial(self._after, token))
                except Exception as exc:
                    self._awaiting_end = False
                    self._active_token = None
                    source.cleanup()
                    await self.announce(f"⚠️ Could not start **{track.title}** — {exc}.")
                    return

            if announce_track and seek == 0.0:
                await self.announce(embed=now_playing_embed(self))

            await self._next.wait()
            played = self.elapsed
            self._awaiting_end = False
            self._active_token = None
            self._paused = False
            position = played
            played_for = max(0.0, position - seek)
            reason = self._finish_reason
            replaying = True

            if reason == SKIPPED and self._skips > 0:
                # Skip requested while this track was still starting up.
                self._skips -= 1
                return

            if reason == RESUME:
                resumptions += 1
                if resumptions > MAX_RESUME_ATTEMPTS:
                    await self.announce(
                        "🔌 The connection keeps dropping — giving up on this track."
                    )
                    return
                seek = position
                continue

            resumptions = 0

            if reason == SEEK:
                seek = self._seek_target()
                continue

            if reason in (SKIPPED, STOPPED):
                return

            died_young = played_for < EARLY_DEATH_WINDOW and (
                track.duration is None or position < track.duration * 0.9
            )
            if reason == FINISHED and died_young:
                if retries < MAX_STREAM_RETRIES:
                    retries += 1
                    seek = position
                    await self.announce(
                        "🔄 The stream cut out — picking up where it left off."
                    )
                    continue
                await self.announce(f"⚠️ **{track.title}** keeps cutting out — skipping.")
                return

            if self.loop:
                seek = 0.0
                continue

            return

    def _seek_target(self) -> float:
        """Where a seek requested mid-track should resume, clamped to the track."""
        target = max(0.0, self._seek_to)
        duration = self.current.duration if self.current is not None else None
        if duration:
            target = min(target, max(0.0, duration - 1.0))
        return target

    async def _await_idle(self, voice_client: discord.VoiceClient) -> None:
        """Give a previous source a moment to release the voice client."""
        for _ in range(20):
            if not (voice_client.is_playing() or voice_client.is_paused()):
                return
            with contextlib.suppress(Exception):
                voice_client.stop()
            await asyncio.sleep(0.1)

    def _after(self, token: int, error: Exception | None) -> None:
        if error is not None:
            print(f"[!] Playback error in guild {self.guild.id}: {error}")
        with contextlib.suppress(RuntimeError):
            # The loop can be closed while a voice thread is still winding down.
            self.bot.loop.call_soon_threadsafe(self._finish, FINISHED, token)

    def _finish(self, reason: str, token: int | None = None) -> None:
        """Wake the playback loop, ignoring callbacks from a dead source."""
        if token is not None and token != self._active_token:
            # A source we already replaced fired its completion callback; letting
            # it through would instantly "finish" the track that replaced it.
            return
        if not self._awaiting_end:
            # Nothing is on air: a stale wake-up here would immediately finish
            # whatever track starts next.
            return
        self._active_token = None
        if self._finish_reason is None:
            self._finish_reason = reason
        self._next.set()

    def _abort(self, reason: str) -> None:
        """Stop playback now and invalidate anything that is still resolving."""
        self._abort_seq += 1
        self._skips = 0
        voice_client = self.voice_client
        if voice_client is not None:
            with contextlib.suppress(Exception):
                voice_client.stop()
        self._active_token = None
        self._paused = False
        # A forced order always wins over whatever reason was pending.
        self._finish_reason = reason
        if self._awaiting_end:
            self._awaiting_end = False
            self._next.set()
        else:
            # The loop can be waiting for a wake-up between tracks; make sure it
            # re-checks the (now empty) queue instead of falling asleep.
            self._wake.set()

    # ----------------------------------------------------- track manipulation

    def toggle_pause(self) -> bool | None:
        """Pause or resume the current track. Returns the new paused state."""
        voice_client = self.voice_client
        if voice_client is None or not self._awaiting_end:
            return None

        if self._paused:
            with contextlib.suppress(Exception):
                voice_client.resume()
            self._started_at += time.monotonic() - self._paused_at
            self._paused = False
            return False

        with contextlib.suppress(Exception):
            voice_client.pause()
        self._paused = True
        self._paused_at = time.monotonic()
        return True

    def seek(self, position: float) -> bool:
        """Restart the current track at ``position`` seconds. False if idle."""
        if self.current is None or not self._awaiting_end:
            return False
        self._seek_to = max(0.0, position)
        voice_client = self.voice_client
        if voice_client is not None:
            with contextlib.suppress(Exception):
                voice_client.stop()
        self._finish(SEEK)
        return True

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
                self._stall_token = self._play_token
                return
            # Connected, a track is on air, yet no audio is being pushed: the
            # player thread died without ever firing its completion callback.
            if self._stall_token != self._play_token:
                self._stall_token = self._play_token
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
            if self._finish_reason != RESUME:
                # /stop, /leave or a forced disconnect landed while we were
                # reconnecting: that wins over resuming.
                return
            if voice_client is None:
                self.clear()
                await self.announce(
                    "🔌 Lost the voice connection and could not restore it — "
                    "the queue has been cleared."
                )
                self._abort(STOPPED)
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
        was_playing = self.current is not None or bool(self.queue)
        self.clear()
        self._abort(STOPPED)
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

    if player.paused:
        embed.add_field(name="Playback", value="⏸️ Paused", inline=True)
    if player.loop:
        embed.add_field(name="Repeat", value="🔁 On", inline=True)

    if track.requester_id:
        embed.add_field(name="Requested by", value=f"<@{track.requester_id}>")
    footer = []
    if player.queue:
        footer.append(f"{len(player.queue)} track(s) up next")
    if player.paused:
        footer.append("paused")
    if footer:
        embed.set_footer(text=" · ".join(footer))
    return embed


def _queue_line(position: int, track: Track) -> str:
    return (
        f"`{position}.` {track.display} — `{format_duration(track.duration)}`"
    )


def queue_lines(tracks: Sequence[Track], start: int) -> list[str]:
    """Format tracks for an embed field."""
    return [_queue_line(start + offset, track) for offset, track in enumerate(tracks)]


def _page_layout(player: GuildPlayer, page_size: int) -> list[tuple[int, int]]:
    """Split the queue into ``(start, end)`` slices that each fit one embed.

    Discord rejects an embed field longer than 1024 characters, and a couple of
    long titles are enough to blow past that with a fixed page size, so the
    slices are measured rather than assumed.
    """
    slices: list[tuple[int, int]] = []
    index = 0
    total = len(player.queue)
    while index < total:
        end = min(index + page_size, total)
        used = 0
        while end > index:
            used = sum(
                len(_queue_line(position + 1, player.queue[position])) + 1
                for position in range(index, end)
            )
            if used <= QUEUE_FIELD_BUDGET:
                break
            end -= 1
        if end == index:
            # A single line over budget: show it anyway, trimmed below.
            end = index + 1
        slices.append((index, end))
        index = end
    return slices


def queue_pages(player: GuildPlayer, page_size: int = QUEUE_PAGE_SIZE) -> int:
    """How many pages the current queue needs at this page size."""
    return max(1, len(_page_layout(player, page_size)))


def queue_embed(player: GuildPlayer, page: int, page_size: int = QUEUE_PAGE_SIZE):
    """Build one page of the queue. Returns ``(embed, page, pages)``."""
    layout = _page_layout(player, page_size)
    pages = max(1, len(layout))
    page = min(max(page, 1), pages)
    start, end = layout[page - 1] if layout else (0, 0)

    embed = discord.Embed(title="🎶 Queue", colour=discord.Colour.blurple())
    if player.current is not None:
        value = (
            f"{player.current.display}\n"
            f"`{format_duration(player.elapsed)} / "
            f"{format_duration(player.current.duration)}`"
        )
        if player.paused:
            value = f"⏸️ {value}"
        if player.loop:
            value = f"{value}\n🔁 repeating"
        embed.add_field(name="Now playing", value=value[:1024], inline=False)

    lines = queue_lines(player.queue[start:end], start + 1)
    if lines:
        embed.add_field(name="Up next", value="\n".join(lines)[:1024], inline=False)
        embed.set_footer(
            text=(
                f"Page {page}/{pages} · {len(player.queue)} track(s) · "
                f"{format_duration(player.total_duration)} total"
            )
        )
    else:
        embed.set_footer(text="Nothing else is queued")

    return embed, page, pages
