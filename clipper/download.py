"""URL validation, metadata fetching and video download via yt-dlp.

The clipper only accepts YouTube and Facebook hosts. The Video
Downloader accepts those PLUS TikTok and Instagram (see
DOWNLOAD_ALLOWED_HOSTS). All yt-dlp failures are translated into short,
human-readable messages — tracebacks are never surfaced to API callers.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from urllib.parse import urlparse

ALLOWED_HOSTS = {
    "youtube.com",
    "www.youtube.com",
    "m.youtube.com",
    "youtu.be",
    "facebook.com",
    "www.facebook.com",
    "fb.watch",
    "www.fb.watch",
}

# The Video Downloader accepts the same hosts as the clipper PLUS TikTok
# and Instagram (the Termux Manan Downloader's menu is TikTok / Facebook
# / YouTube Shorts; Instagram was added as a fourth platform). The
# clipper's ALLOWED_HOSTS is intentionally unchanged — Instagram links
# are downloader-only, because clipping Instagram links is not part of
# the clipper's job flow.
DOWNLOAD_ALLOWED_HOSTS = ALLOWED_HOSTS | {
    "tiktok.com",
    "www.tiktok.com",
    "m.tiktok.com",
    "vt.tiktok.com",
    "vm.tiktok.com",
    "instagram.com",
    "www.instagram.com",
}

# At most this many playlist items per download. Free-tier disks are
# small and ephemeral — a whole channel/playlist must not fill them.
MAX_PLAYLIST_ITEMS = 10

# Video extensions a completed yt-dlp download can have (we merge to
# mp4, but keep the others as a safety net when a merge is unavailable).
VIDEO_EXTENSIONS = {".mp4", ".mkv", ".webm", ".mov", ".m4v"}

# yt-dlp format: 1080p-or-below mp4 video + m4a audio, with fallbacks.
FORMAT_SELECTOR = (
    "bestvideo[height<=1080][ext=mp4]+bestaudio[ext=m4a]"
    "/bestvideo[height<=1080]+bestaudio"
    "/best[height<=1080]/best"
)

# Downloader quality choices (dropdown value -> max video height).
# "best" is the same as the default FORMAT_SELECTOR above: best quality
# up to 1080p.
QUALITY_HEIGHTS = {"best": 1080, "1080": 1080, "720": 720, "480": 480, "360": 360}

# YouTube player client: the android client ALONE, and NOTHING skipped.
# Tested 25 Nov 2026 (yt-dlp 2026.11.23, datacenter IP): android-only
# with no player_skip succeeded 4/4 — metadata + a full 270 s download
# of https://youtu.be/hRGzfqYAgbo — while every other combination failed:
#   * "android,ios,tv_embedded" + player_skip=webpage (the 0.2.1 fix):
#     FAILED — the live tool hit the bot check on Railway with it, and
#     the tool's own fetch_metadata failed with it locally too.
#   * player_skip=webpage on its own: FAILED even with android alone.
#     Skipping the webpage client breaks the android path — the webpage
#     download is what establishes the session (visitor data) that the
#     API calls then ride on. The 0.2.1 assumption was backwards.
#   * web client, with or without a bgutil PO token (bgutil-ytdlp-pot-
#     provider 2.0.2 + its token server on :4416): FAILED from a
#     datacenter IP, and adding web to the client list poisoned the
#     android path too. So no token server is needed or used — the
#     Dockerfile stays Node-free.
# The flags are ignored by non-YouTube extractors, so every call passes
# them.
YOUTUBE_PLAYER_CLIENTS = "android"

# A YouTube call that fails with a bot-check error is retried this many
# extra times after a sleep — the challenge is often transient (a fresh
# request seconds later can pass on the same IP; android-only passed
# 4/4 in testing, but Railway's IP may be more heavily flagged, so the
# retries buy a large margin).
YOUTUBE_BOT_RETRIES = 3
YOUTUBE_BOT_RETRY_DELAY_S = 15


def format_selector(quality: str = "best", max_size_mb: float | None = None) -> str:
    """Build a yt-dlp format selector for a quality + optional size cap.

    `quality` is one of "best" / "1080" / "720" / "480" / "360" (max
    video height). `max_size_mb` caps each format's filesize
    (`[filesize<50M]`); if no format fits the cap, yt-dlp fails with
    "Requested format is not available" and the caller turns that into
    a clear "no format fits" message. Unknown quality values fall back
    to "best".

    The bare `/best` last resort is kept ONLY for an unconstrained
    "best" request (the historical behaviour — e.g. a 4K-only video
    still downloads). Whenever a quality or size cap is requested the
    cap must be real, so no uncapped fallback is appended: a cap that
    nothing satisfies must fail loudly instead of silently returning
    a file that ignores the cap.
    """
    height = QUALITY_HEIGHTS.get((quality or "best").strip().lower(), 1080)
    size_pred = f"[filesize<{max_size_mb:g}M]" if max_size_mb else ""
    selector = (
        f"bestvideo[height<={height}]{size_pred}[ext=mp4]+bestaudio{size_pred}[ext=m4a]"
        f"/bestvideo[height<={height}]{size_pred}+bestaudio{size_pred}"
        f"/best[height<={height}]{size_pred}"
    )
    if (quality or "best").strip().lower() == "best" and not max_size_mb:
        selector += "/best"
    return selector


def validate_url(url: str) -> str:
    """Validate a source URL. Returns the stripped URL.

    Raises ValueError with a clear reason for anything invalid or blocked.
    """
    if not url or not isinstance(url, str):
        raise ValueError("Please provide a video URL.")
    url = url.strip()
    if not url:
        raise ValueError("Please provide a video URL.")
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise ValueError("URL must start with http:// or https://.")
    host = (parsed.netloc or "").lower().split(":")[0]
    if host not in ALLOWED_HOSTS:
        raise ValueError(
            "Only YouTube and Facebook video links are supported "
            "(youtube.com, youtu.be, facebook.com, fb.watch)."
        )
    if not parsed.path or parsed.path in ("/",):
        raise ValueError("That link does not point to a video.")
    return url


def _friendly_error(stderr: str, url: str) -> str:
    """Map yt-dlp stderr to a short human-readable message."""
    text = (stderr or "").lower()
    if "bot" in text or "sign in to confirm you’re not a bot" in text or "confirm you are not a bot" in text:
        return "YouTube blocked this request (bot check). Try a different video or try again later."
    if ("available in your country" in text or "blocked in your country" in text
            or "geo-block" in text or "geo-restricted" in text
            or "geo restricted" in text):
        # e.g. "[youtube] <id>: The uploader has not made this video
        # available in your country" — the video is geo-blocked for the
        # country the server runs in. Must come before the generic
        # "not available" check below, which would hide the real cause.
        return "This video isn't available in the server's country — try a different link."
    if "instagram" in url.lower() and ("login" in text or "log in" in text
            or "sign in" in text or "cookies" in text or "private" in text):
        # Instagram only works for public posts/reels; anything that
        # needs an account (or is private) fails in yt-dlp with a
        # login/cookies error. Say so plainly instead of the generic
        # "needs login" message.
        return "This Instagram post needs login or is private — only public posts and reels can be downloaded."
    if "tiktok" in url.lower() and ("unsupported url" in text or "unable to" in text):
        return "TikTok blocked this download — TikTok restricts most downloads. Try a YouTube or Facebook link instead."
    if "private" in text:
        return "This video is private."
    if "login" in text or "log in" in text or "cookies" in text or "sign in" in text:
        return "This video needs login or is private."
    if "age" in text and ("confirm" in text or "restricted" in text):
        return "This video is age-restricted and needs login."
    if "not available" in text or "removed" in text or "unavailable" in text:
        return "This video is not available (it may have been removed or made private)."
    if "unsupported url" in text:
        return "That link is not a supported video URL."
    if "timed out" in text or "timeout" in text:
        return "The request timed out. Please try again."
    if "no video formats" in text or "requested format not available" in text:
        return "No downloadable video format was found for this link."
    if "404" in text:
        return "That video was not found (404)."
    if "403" in text:
        return "Access to this video was denied by the platform (403)."
    # Fall back to the last non-empty stderr line, trimmed, without leaking
    # a traceback or a stack of internal detail.
    lines = [line.strip() for line in (stderr or "").splitlines() if line.strip()]
    for line in reversed(lines):
        if line.startswith("ERROR"):
            detail = line[len("ERROR"):].strip(" :")
            if detail:
                return detail[:300]
    if lines:
        return lines[-1][:300]
    return "Could not download this video. Please check the link and try again."


def _yt_dlp_cmd() -> list[str]:
    """Resolve the yt-dlp command to run.

    Prefer a `yt-dlp` binary on PATH; if there is none (e.g. the server
    was started without .venv/bin on PATH), fall back to running the
    installed yt_dlp package with the current Python interpreter.
    """
    binary = shutil.which("yt-dlp")
    if binary:
        return [binary]
    return [sys.executable, "-m", "yt_dlp"]


def _is_bot_check(stderr: str) -> bool:
    """True when yt-dlp stderr says YouTube challenged the request as a
    bot. Kept deliberately strict ("not a bot", "bot check") — a loose
    "bot" substring would also match words like "both"."""
    text = (stderr or "").lower()
    return (
        "not a bot" in text
        or "bot check" in text
        or "are a bot" in text
        or "not a robot" in text
    )


def _run_yt_dlp(args: list[str], timeout: int, player_client: str | None = None) -> subprocess.CompletedProcess:
    """Run yt-dlp. player_client selects the YouTube player client(s)
    (currently "android" only — see YOUTUBE_PLAYER_CLIENTS); it is
    ignored by non-YouTube extractors. A YouTube call that fails with a
    bot-check error is retried up to YOUTUBE_BOT_RETRIES times with a
    sleep between attempts — the challenge is often transient."""
    cmd = _yt_dlp_cmd()
    if player_client:
        cmd = cmd + [
            "--extractor-args", f"youtube:player_client={player_client}",
        ]
    is_youtube = any("youtube" in a or "youtu.be" in a for a in args)
    attempts = YOUTUBE_BOT_RETRIES + 1 if (is_youtube and player_client) else 1
    result: subprocess.CompletedProcess | None = None
    for attempt in range(attempts):
        try:
            result = subprocess.run(
                cmd + args,
                capture_output=True,
                text=True,
                timeout=timeout,
            )
        except FileNotFoundError:
            raise RuntimeError("yt-dlp is not installed on this server.")
        except subprocess.TimeoutExpired:
            raise RuntimeError("The request timed out. Please try again.")
        if (
            result.returncode == 0
            or attempt == attempts - 1
            or not _is_bot_check(result.stderr)
        ):
            return result
        time.sleep(YOUTUBE_BOT_RETRY_DELAY_S)
    # Unreachable (the loop always returns), kept so type checkers and
    # readers see a result is always produced.
    assert result is not None
    return result


def platform_for_url(url: str) -> str:
    """Human-readable platform label for a URL (YouTube / TikTok / Facebook)."""
    try:
        host = (urlparse(url).netloc or "").lower().split(":")[0]
    except Exception:
        return "Video"
    if "tiktok" in host:
        return "TikTok"
    if "instagram" in host:
        return "Instagram"
    if "facebook" in host or host.endswith("fb.watch"):
        return "Facebook"
    if "youtube" in host or host == "youtu.be":
        return "YouTube"
    return "Video"


def validate_download_url(url: str) -> str:
    """Validate a downloader URL. Same rules as validate_url, plus TikTok.

    Raises ValueError with a clear reason for anything invalid or blocked.
    """
    if not url or not isinstance(url, str):
        raise ValueError("Please provide a video URL.")
    url = url.strip()
    if not url:
        raise ValueError("Please provide a video URL.")
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise ValueError("URL must start with http:// or https://.")
    host = (parsed.netloc or "").lower().split(":")[0]
    if host not in DOWNLOAD_ALLOWED_HOSTS:
        raise ValueError(
            "Only YouTube, TikTok, Facebook and Instagram video links "
            "are supported (youtube.com, youtu.be, tiktok.com, "
            "facebook.com, fb.watch, instagram.com)."
        )
    if not parsed.path or parsed.path in ("/",):
        raise ValueError("That link does not point to a video.")
    return url


def _is_playlist_url(url: str) -> bool:
    """True for a YouTube /playlist link (the only playlist shape we treat
    as a multi-video download). A /watch link with a list= parameter is
    treated as a single video — paste the /playlist link to get the list."""
    parsed = urlparse(url)
    host = (parsed.netloc or "").lower().split(":")[0]
    return "youtube" in host and parsed.path.rstrip("/") == "/playlist"


def download_files(
    url: str,
    out_dir: str,
    limit: int = 0,
    max_file_mb: int = 2048,
    max_duration: float | None = None,
    quality: str = "best",
    max_size_mb: float | None = None,
) -> tuple[list[str], str | None]:
    """Download a video (or a playlist's first items) into out_dir.

    Mirrors the Termux Manan Downloader: best quality (capped at 1080p
    here, where the Termux tool used unbounded 'best'), file named after
    the video title, playlist capped by `limit` (0 = all, up to
    MAX_PLAYLIST_ITEMS). Returns (file paths, title or None).

    `quality` picks the max resolution ("best" / "1080" / "720" / "480"
    / "360"); `max_size_mb` caps each format's filesize in the format
    selector — if no format fits the cap at that quality, a clear
    "no format fits" error is raised instead of a generic download
    failure. The download is always the COMPLETE video for a single
    link (a playlist's items are each complete videos).

    YouTube is fetched with the android player client only (see
    YOUTUBE_PLAYER_CLIENTS) — tested 25 Nov 2026: android-only passed
    4/4 from a datacenter IP while the three-client set and the web
    client (with or without a PO token) all failed. A bot-check failure
    is retried up to YOUTUBE_BOT_RETRIES times with a sleep between
    attempts. The flags are ignored for TikTok/Facebook.

    Raises ValueError for over-long sources and RuntimeError with a
    human-readable message on any yt-dlp failure or over-size result.
    """
    url = validate_download_url(url)
    os.makedirs(out_dir, exist_ok=True)
    player_client = YOUTUBE_PLAYER_CLIENTS
    fmt = format_selector(quality, max_size_mb)

    if _is_playlist_url(url):
        cap = limit if 0 < limit <= MAX_PLAYLIST_ITEMS else MAX_PLAYLIST_ITEMS
        title: str | None = None
        args = [
            "-f", fmt,
            "--merge-output-format", "mp4",
            "--playlist-end", str(cap),
            "--no-warnings",
            "-o", os.path.join(out_dir, "%(title)s.%(ext)s"),
            url,
        ]
    else:
        # Single video: check duration first (same cap as clipping jobs),
        # then download just this video even if the URL carries a list=.
        if max_duration is not None:
            meta = fetch_metadata(
                url, player_client=player_client,
                validator=validate_download_url,
            )
            duration = meta.get("duration")
            if duration is not None and duration > max_duration:
                raise ValueError(
                    f"This video is {duration / 3600:.1f} hours long, "
                    f"over the {max_duration / 3600:.1f} hour limit."
                )
            title = meta.get("title")
        else:
            title = None
        args = [
            "-f", fmt,
            "--merge-output-format", "mp4",
            "--no-playlist",
            "--no-warnings",
            "-o", os.path.join(out_dir, "%(title)s.%(ext)s"),
            url,
        ]

    result = _run_yt_dlp(args, timeout=1800, player_client=player_client)
    if result.returncode != 0:
        err_text = (result.stderr or "").lower()
        if ("requested format is not available" in err_text
                or "no video formats" in err_text):
            if quality != "best" or max_size_mb:
                # The failure IS the cap: no format satisfies the
                # requested quality / size. Say so plainly instead of
                # the generic "no format" message.
                parts = []
                if quality != "best":
                    parts.append(f"{quality}p quality")
                if max_size_mb:
                    parts.append(f"{max_size_mb:g} MB size cap")
                raise RuntimeError(
                    "No format fits " + " and ".join(parts) +
                    " — try Best (up to 1080p) or a higher size limit."
                )
        raise RuntimeError(_friendly_error(result.stderr, url))

    files = sorted(
        str(p)
        for p in _iter_downloaded_files(out_dir)
    )
    if not files:
        raise RuntimeError("Download finished but no video file was found.")
    total_mb = sum(os.path.getsize(f) for f in files) / (1024 * 1024)
    if total_mb > max_file_mb:
        for f in files:
            try:
                os.remove(f)
            except OSError:
                pass
        raise RuntimeError(
            f"Downloaded files total {total_mb:.0f} MB, over the {max_file_mb} MB limit."
        )
    return files, title


def _iter_downloaded_files(out_dir: str):
    """Yield completed video files in out_dir (skip .part/.ytdl leftovers)."""
    from pathlib import Path

    for path in Path(out_dir).iterdir():
        if not path.is_file():
            continue
        if path.suffix.lower() in VIDEO_EXTENSIONS:
            yield path


def fetch_metadata(url: str, timeout: int = 60, player_client: str | None = YOUTUBE_PLAYER_CLIENTS,
                   validator=None) -> dict:
    """Fetch title / duration / thumbnail / uploader via yt-dlp --dump-json.

    Returns {"title": str|None, "duration": float|None,
             "thumbnail": str|None, "uploader": str|None}.
    Raises RuntimeError with a human-readable message on failure.
    `validator` overrides the URL check (the downloader passes
    validate_download_url so TikTok links are accepted here too).
    """
    (validator or validate_url)(url)
    result = _run_yt_dlp(
        ["--dump-json", "--no-playlist", "--no-warnings", url],
        timeout=timeout,
        player_client=player_client,
    )
    if result.returncode != 0:
        raise RuntimeError(_friendly_error(result.stderr, url))
    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError:
        raise RuntimeError("Could not read the video information. Please try again.")
    return {
        "title": data.get("title"),
        "duration": data.get("duration"),
        "thumbnail": data.get("thumbnail"),
        "uploader": data.get("uploader"),
    }


def download_audio(url: str, out_path: str, player_client: str | None = YOUTUBE_PLAYER_CLIENTS) -> str:
    """Download audio-only for a clipping job (small, fast).

    Used by the clipping pipeline: the Whisper transcript and the
    loudness curve only need audio, so the full video is never
    downloaded for a clipping job. The audio is extracted to m4a
    (transcoded by ffmpeg when the source audio is another codec).
    Returns out_path. Raises RuntimeError with a human-readable
    message on any yt-dlp failure.
    """
    os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)
    result = _run_yt_dlp(
        [
            "-f", "bestaudio/best",
            "--extract-audio", "--audio-format", "m4a",
            "--no-playlist",
            "--no-warnings",
            "-o", out_path,
            url,
        ],
        timeout=1800,
        player_client=player_client,
    )
    if result.returncode != 0:
        raise RuntimeError(_friendly_error(result.stderr, url))
    if not os.path.exists(out_path):
        candidates = [out_path + ".m4a"]
        found = next((c for c in candidates if os.path.exists(c)), None)
        if found is None:
            raise RuntimeError("Audio download finished but no audio file was found.")
        out_path = found
    return out_path


def download_section(
    url: str, start: float, end: float, out_path: str,
    player_client: str | None = YOUTUBE_PLAYER_CLIENTS,
) -> str:
    """Download ONLY the video section [start, end] for one clip.

    Uses yt-dlp `--download-sections`, which fetches just the requested
    range (verified: a 20 s section of a 55 s video downloads in ~6 s
    at ~1.5 MB instead of ~49 s / ~3.8 MB for the full video), and the
    section file starts exactly at `start` (frame-verified), so the
    caller renders it from offset 0. The video is never downloaded in
    full for a clipping job. Raises RuntimeError with a human-readable
    message on any yt-dlp failure (including when the platform does not
    support section downloads — the caller falls back to a full
    download in that case).
    """
    os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)
    section = f"*{float(start):.1f}-{float(end):.1f}"
    result = _run_yt_dlp(
        [
            "-f", FORMAT_SELECTOR,
            "--merge-output-format", "mp4",
            "--download-sections", section,
            "--no-playlist",
            "--no-warnings",
            "-o", out_path,
            url,
        ],
        timeout=1800,
        player_client=player_client,
    )
    if result.returncode != 0:
        raise RuntimeError(_friendly_error(result.stderr, url))
    if not os.path.exists(out_path):
        candidates = [out_path + ".mp4"]
        found = next((c for c in candidates if os.path.exists(c)), None)
        if found is None:
            raise RuntimeError("Section download finished but no video file was found.")
        out_path = found
    return out_path


def download_video(url: str, out_path: str, max_file_mb: int, max_duration: float | None = None,
                   player_client: str | None = YOUTUBE_PLAYER_CLIENTS) -> str:
    """Download the video to out_path (mp4, <=1080p). Returns out_path.

    Raises ValueError if the video exceeds max_duration (when given), and
    RuntimeError with a human-readable message on any yt-dlp failure.
    Raises RuntimeError if the downloaded file exceeds max_file_mb.
    """
    validate_url(url)
    if max_duration is not None:
        meta = fetch_metadata(url, player_client=player_client)
        duration = meta.get("duration")
        if duration is not None and duration > max_duration:
            raise ValueError(
                f"This video is {duration / 3600:.1f} hours long, "
                f"over the {max_duration / 3600:.1f} hour limit."
            )
    os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)
    # yt-dlp writes to a template; use the exact path via -o.
    result = _run_yt_dlp(
        [
            "-f", FORMAT_SELECTOR,
            "--merge-output-format", "mp4",
            "--no-playlist",
            "--no-warnings",
            "-o", out_path,
            url,
        ],
        timeout=1800,
        player_client=player_client,
    )
    if result.returncode != 0:
        raise RuntimeError(_friendly_error(result.stderr, url))
    if not os.path.exists(out_path):
        # yt-dlp may append an extension if the template lacked one.
        candidates = [out_path + ".mp4"]
        found = next((c for c in candidates if os.path.exists(c)), None)
        if found is None:
            raise RuntimeError("Download finished but the video file was not found.")
        out_path = found
    size_mb = os.path.getsize(out_path) / (1024 * 1024)
    if size_mb > max_file_mb:
        try:
            os.remove(out_path)
        except OSError:
            pass
        raise RuntimeError(
            f"Downloaded file is {size_mb:.0f} MB, over the {max_file_mb} MB limit."
        )
    return out_path
