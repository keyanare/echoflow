"""Track metadata extraction and audio stream resolution on top of yt-dlp."""

from __future__ import annotations

import asyncio
import re
import shlex
from dataclasses import dataclass
from typing import Any, Iterable

import aiohttp
import discord
import yt_dlp

MAX_PLAYLIST_ITEMS = 100

SEARCH_TIMEOUT = 60.0
STREAM_TIMEOUT = 30.0
PROBE_TIMEOUT = 10.0


class _QuietLogger:
    """Swallows yt-dlp output — failures are reported to the user as TrackError."""

    def debug(self, message: str) -> None: ...

    def info(self, message: str) -> None: ...

    def warning(self, message: str) -> None: ...

    def error(self, message: str) -> None: ...


_BASE_OPTIONS: dict[str, Any] = {
    "format": "bestaudio/best",
    "logger": _QuietLogger(),
    "quiet": True,
    "no_warnings": True,
    "noprogress": True,
    "logtostderr": False,
    "nocheckcertificate": True,
    "ignoreerrors": False,
    "skip_download": True,
    "source_address": "0.0.0.0",
    "socket_timeout": 15,
    "retries": 3,
}

SEARCH_OPTIONS: dict[str, Any] = _BASE_OPTIONS | {
    "default_search": "auto",
    "extract_flat": "in_playlist",
    "playlistend": MAX_PLAYLIST_ITEMS,
    "noplaylist": False,
}

STREAM_OPTIONS: dict[str, Any] = _BASE_OPTIONS | {"noplaylist": True}

# YouTube hands out stream links that are tied to the client that asked for them.
# When one of them gets rejected (the classic "403 Forbidden" right after playback
# starts), asking again as a different client usually produces a working link.
PLAYER_CLIENTS: tuple[str | None, ...] = (None, "web_safari", "android_vr", "tv")

FFMPEG_OPTIONS = "-vn"
FFMPEG_BEFORE_OPTIONS = (
    "-nostdin -reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5"
)


class TrackError(Exception):
    """A track could not be found, resolved or streamed."""


@dataclass(slots=True)
class Track:
    url: str
    title: str
    duration: float | None = None
    requester_id: int | None = None

    @property
    def display(self) -> str:
        return f"[{discord.utils.escape_markdown(self.title)}]({self.url})"


def format_duration(seconds: float | None) -> str:
    if not seconds:
        return "live"
    total = int(seconds)
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"


async def search(query: str) -> tuple[list[Track], str | None]:
    """Resolve a URL or a search query into tracks and an optional playlist title."""
    is_search = not query.startswith(("http://", "https://"))
    target = f"ytsearch1:{query}" if is_search else query

    data = await _extract(SEARCH_OPTIONS, target, timeout=SEARCH_TIMEOUT)
    entries = data.get("entries")

    if entries is None:
        track = _track_from(data)
        if track is None:
            raise TrackError("this link has no playable audio")
        return [track], None

    tracks = [_track_from(entry) for entry in list(entries) if entry]
    tracks = [track for track in tracks if track is not None]
    if not tracks:
        raise TrackError("nothing playable was found for this query")

    if is_search:
        return tracks[:1], None
    return tracks, data.get("title")


async def create_source(
    track: Track, *, seek: float = 0.0, volume: float = 0.5
) -> discord.PCMVolumeTransformer:
    """Resolve a fresh stream link for `track` and wrap it into an audio source."""
    stream_url, headers, info = await _resolve_stream(track)

    if track.duration is None:
        track.duration = info.get("duration")
    if info.get("title"):
        track.title = info["title"]

    before = [FFMPEG_BEFORE_OPTIONS]
    if seek > 0:
        before.append(f"-ss {seek:.2f}")
    if headers:
        blob = "".join(f"{name}: {value}\r\n" for name, value in headers.items())
        before.append(f"-headers {shlex.quote(blob)}")

    source = discord.FFmpegPCMAudio(
        stream_url,
        before_options=" ".join(before),
        options=FFMPEG_OPTIONS,
    )
    return discord.PCMVolumeTransformer(source, volume=volume)


async def _resolve_stream(track: Track) -> tuple[str, dict[str, str], dict[str, Any]]:
    last_error: TrackError | None = None

    for client in _player_clients(track.url):
        options = dict(STREAM_OPTIONS)
        if client is not None:
            options["extractor_args"] = {"youtube": {"player_client": [client]}}

        try:
            data = await _extract(options, track.url, timeout=STREAM_TIMEOUT)
        except TrackError as exc:
            last_error = exc
            continue

        info = _first_entry(data)
        stream_url = info.get("url")
        if not stream_url:
            last_error = TrackError("no audio-only stream is available")
            continue

        headers = {
            str(name): str(value)
            for name, value in (info.get("http_headers") or {}).items()
        }
        problem = await _stream_problem(stream_url, headers)
        if problem is None:
            return stream_url, headers, info

        last_error = TrackError(
            f"the stream link was rejected by the server ({problem})"
        )

    raise last_error or TrackError("could not resolve an audio stream")


def _player_clients(url: str) -> tuple[str | None, ...]:
    """Client fallbacks only help on YouTube; elsewhere they just repeat the request."""
    if "youtube.com" in url or "youtu.be" in url:
        return PLAYER_CLIENTS
    return (None,)


async def _stream_problem(url: str, headers: dict[str, str]) -> str | None:
    """Fetch the first bytes; returns None if fine, else why it is not playable."""
    if not url.startswith(("http://", "https://")):
        return None

    probe_headers = headers | {"Range": "bytes=0-1"}
    timeout = aiohttp.ClientTimeout(total=PROBE_TIMEOUT)
    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(url, headers=probe_headers) as response:
                return None if response.status < 400 else f"HTTP {response.status}"
    except asyncio.TimeoutError:
        return "timed out"
    except (aiohttp.ClientError, OSError) as exc:
        return type(exc).__name__


async def _extract(
    options: dict[str, Any], query: str, *, timeout: float
) -> dict[str, Any]:
    loop = asyncio.get_running_loop()

    def run() -> Any:
        with yt_dlp.YoutubeDL(options) as ydl:
            return ydl.extract_info(query, download=False)

    try:
        data = await asyncio.wait_for(loop.run_in_executor(None, run), timeout)
    except asyncio.TimeoutError as exc:
        raise TrackError("the source took too long to answer") from exc
    except yt_dlp.utils.DownloadError as exc:
        raise TrackError(_clean_error(str(exc))) from exc
    except Exception as exc:  # network stack, extractor bugs, ...
        raise TrackError(f"unexpected extraction error: {exc}") from exc

    if not data:
        raise TrackError("no data was returned for this link")
    return data


def _first_entry(data: dict[str, Any]) -> dict[str, Any]:
    entries: Iterable[Any] | None = data.get("entries")
    if entries is None:
        return data
    for entry in entries:
        if entry:
            return entry
    return data


def _track_from(entry: dict[str, Any]) -> Track | None:
    url = _entry_url(entry)
    if not url:
        return None
    return Track(
        url=url,
        title=entry.get("title") or "Untitled",
        duration=entry.get("duration"),
    )


def _entry_url(entry: dict[str, Any]) -> str | None:
    """Pick a stable page URL — never a CDN link, those expire while queued."""
    # Flat playlist entries: `url` is the canonical link yt-dlp expects to be
    # handed back later. On SoundCloud that is the track-id form, which keeps
    # working after an uploader renames a track and its permalink 404s.
    if entry.get("_type") in {"url", "url_transparent"} and entry.get("url"):
        return entry["url"]

    # Fully extracted entries: `url` is the expiring stream, so never use it.
    for key in ("webpage_url", "original_url"):
        if entry.get(key):
            return entry[key]

    video_id = entry.get("id")
    extractor = str(entry.get("ie_key") or entry.get("extractor_key") or "")
    if video_id and extractor.lower().startswith("youtube"):
        return f"https://www.youtube.com/watch?v={video_id}"

    return None


def _clean_error(message: str) -> str:
    text = message.strip().splitlines()[0]
    text = re.sub(r"^ERROR:\s*", "", text)
    text = re.sub(r"^(\[[^\]]+\]\s*)+", "", text)
    text = re.sub(r"^(?:[\w-]{11}|\d{5,12}):\s*", "", text)
    if len(text) > 200:
        text = f"{text[:197]}..."
    return text or "unknown error"
