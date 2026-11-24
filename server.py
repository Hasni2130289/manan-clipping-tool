"""Manan Clipping Tool — FastAPI server.

Serves the API and the frontend from web/ (the whole directory is served
as static files, so /, /app.js, /style.css and /assets/logo.jpg all work).

Auth is bearer-token based (see "Token auth" below): the frontend — which
may live on a different origin, e.g. a Muse Space — logs in via
POST /api/login, stores the returned token, and sends it as
`Authorization: Bearer <token>` on every other /api/* call. Static files,
GET /api/health and GET /api/clips/{id} (clip URLs are unguessable UUIDs
that media elements must load without an auth header) are public; the
app shell is useless without the API, and the frontend shows its own
login view when no token is stored, so page loads never redirect
to /login.

Job lifecycle (async via FastAPI BackgroundTasks):
    queued -> downloading -> transcribing -> ready
    ready/done -> rendering -> done   (further renders from done allowed)
    any failure -> error (with a human-readable message)

Jobs live in an in-memory dict and are persisted per job under
JOB_DIR/<job_id>/ (meta.json, transcript.json, audio.m4a, sections/<id>.mp4,
clips/<id>.mp4). A clipping job NEVER downloads the full video: only
audio-only for the transcript/highlights, plus one video section per
rendered clip. Terminal jobs (ready/done/error) older than JOB_RETENTION_DAYS are
pruned at startup and whenever a job reaches a terminal state.

Video Downloader (POST /api/download): downloads a YouTube / TikTok /
Facebook video (or a playlist's first items, capped) at best quality
<=1080p into DOWNLOADS_DIR/<id>/ and returns a one-time, unguessable
download link (GET /api/downloads/<id>, valid 10 minutes, deleted
after it is served once). The POST is token-gated; the GET is public
on purpose (a browser download link cannot send an Authorization
header) — the same reasoning as GET /api/clips/{id}.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import shutil
import uuid
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi import BackgroundTasks, FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from clipper.download import (
    QUALITY_HEIGHTS,
    download_audio,
    download_files,
    download_section,
    download_video,
    fetch_metadata,
    platform_for_url,
    validate_download_url,
    validate_url,
)
from config import BRAND_LINE, CONTACT_PHONE, CONTACT_URL, SOCIAL_LINKS, TAGLINE
from clipper.highlights import generate_clips, get_loudness_curve, suggest_highlights
from clipper.render import MAX_CLIP_SECONDS, render_clip
from clipper.transcribe import TranscriptionUnavailable, transcribe

VERSION = "0.1.0"
BASE_DIR = Path(__file__).resolve().parent
WEB_DIR = BASE_DIR / "web"

JOB_DIR = Path(os.environ.get("JOB_DIR", "./jobs"))
if not JOB_DIR.is_absolute():
    JOB_DIR = (BASE_DIR / JOB_DIR).resolve()
MAX_DURATION = int(os.environ.get("MAX_DURATION", "14400"))
MAX_FILE_MB = int(os.environ.get("MAX_FILE_MB", "2048"))
# Finished jobs (ready/done/error) older than this are deleted from disk
# and from the job list, at startup and whenever a job finishes. Free-tier
# disks are small and ephemeral — download clips promptly (see README).
JOB_RETENTION_DAYS = int(os.environ.get("JOB_RETENTION_DAYS", "7"))
WHISPER_MODEL = os.environ.get("WHISPER_MODEL", "tiny")

# Video Downloader storage: one temp directory per download under
# DOWNLOADS_DIR/<download_id>/. Links expire after DOWNLOAD_TTL_SECONDS
# (10 minutes) and are deleted after they are served once.
DOWNLOADS_DIR = Path(os.environ.get("DOWNLOADS_DIR", "./downloads"))
if not DOWNLOADS_DIR.is_absolute():
    DOWNLOADS_DIR = (BASE_DIR / DOWNLOADS_DIR).resolve()
DOWNLOAD_TTL_SECONDS = int(os.environ.get("DOWNLOAD_TTL_SECONDS", "600"))
HOST = os.environ.get("HOST", "0.0.0.0")
PORT = int(os.environ.get("PORT", "8000"))

app = FastAPI(title="Manan Clipping Tool", version=VERSION)

# ---------------------------------------------------------------------------
# Token auth + CORS
# ---------------------------------------------------------------------------
# The password itself is never stored anywhere — only its SHA-256 hex
# digest. The digest comes from APP_PASSWORD_HASH and there is NO
# built-in default: the code is published in a public repo, so a
# default hash would be brute-forceable. Set APP_PASSWORD_HASH to the
# SHA-256 hex digest of the password (see README) — in the Render
# dashboard for hosted runs, or in .env for local runs — and restart.
# With no hash set, login always fails (fail-closed).
DEFAULT_PASSWORD_HASH = ""
APP_PASSWORD_HASH = os.environ.get("APP_PASSWORD_HASH", DEFAULT_PASSWORD_HASH).strip().lower()

# Bearer tokens issued by POST /api/login, kept in memory only. No
# cookies are used anywhere: cookies do not work cross-origin, and the
# frontend may live on a different origin (a Muse Space) than this API.
tokens: set[str] = set()

# (method, path) pairs under /api/* that do NOT require a token.
# Everything else under /api/* requires `Authorization: Bearer <token>`.
PUBLIC_API_ROUTES = {
    ("POST", "/api/login"),
    ("GET", "/api/health"),
}


def _is_public_api_route(method: str, path: str) -> bool:
    """True for the API routes that do not need a token.

    GET /api/clips/{id} is public on purpose: the frontend's clip
    <video> preview src and download href are plain element
    attributes, so they cannot send an Authorization header — and
    cross-origin they cannot be given one any other way. Clip IDs are
    unguessable UUIDs, so a clip URL cannot be enumerated, and the
    jobs API stays token-gated, so nobody can list or discover clips
    without the password. Clips are the user's own output meant for
    public posting anyway. DELETE /api/clips/{id} stays token-gated.

    GET /api/downloads/{id} is public for the same reason: the browser
    follows a plain download link, which cannot carry a token. Download
    IDs are unguessable UUIDs, each link is one-time (deleted after it
    is served) and expires after 10 minutes, and creating a download
    still needs a token — so nobody can enumerate or mint downloads
    without the password.
    """
    if (method, path) in PUBLIC_API_ROUTES:
        return True
    if method == "GET" and path.startswith("/api/clips/"):
        return True
    return method == "GET" and path.startswith("/api/downloads/")


def _token_from_request(request: Request) -> str | None:
    """Extract the bearer token from the Authorization header, if any."""
    auth = request.headers.get("Authorization", "")
    if auth.lower().startswith("bearer "):
        token = auth[7:].strip()
        return token or None
    return None


def _is_authenticated(request: Request) -> bool:
    token = _token_from_request(request)
    return bool(token) and token in tokens


@app.middleware("http")
async def auth_gate(request: Request, call_next):
    """Gate every /api/* route except PUBLIC_API_ROUTES behind a token.

    Rationale: the app shell (static files, /login) is public but
    worthless without the API, and the frontend shows its own login
    view when no token is stored — so page loads must NOT redirect to
    /login (that would loop cross-origin). Unauthenticated API calls
    get 401 {"detail": "Authentication required."}; OPTIONS preflights
    pass through so the CORS middleware can answer them.
    """
    if request.method == "OPTIONS" or not request.url.path.startswith("/api/"):
        return await call_next(request)
    if _is_public_api_route(request.method, request.url.path) or _is_authenticated(request):
        return await call_next(request)
    return JSONResponse(status_code=401, content={"detail": "Authentication required."})


# CORS: the frontend may be hosted on a different origin (a Muse Space)
# than this API. Allowed origins come from CORS_ORIGINS (comma-separated);
# the default "*" allows any origin. Credentials are NOT used — auth is
# a bearer token in the Authorization header, never a cookie.
# NOTE: this must be added AFTER the auth middleware above: Starlette
# runs the most recently added middleware outermost, so CORS wraps the
# auth gate and its headers are present on every response, including
# the 401s the gate returns (a cross-origin client must be able to
# read the 401 to know to show its login view).
_cors_origins_raw = os.environ.get("CORS_ORIGINS", "*").strip()
_cors_origins = (
    ["*"]
    if _cors_origins_raw in ("", "*")
    else [origin.strip() for origin in _cors_origins_raw.split(",") if origin.strip()]
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins,
    allow_methods=["*"],
    allow_headers=["*"],
    allow_credentials=False,
)

# ---------------------------------------------------------------------------
# In-memory job store (+ per-job persistence under JOB_DIR/<job_id>/)
# ---------------------------------------------------------------------------
# Job dict shape:
#   id, url, title, duration, thumbnail, status, progress, message,
#   transcript: [{start, end, text, ...}],
#   highlights: [{start, end, score, reasons}],
#   clips: [{id, start, end, hook_text, cta_text, filename, size,
#            duration, created_at}]
jobs: dict[str, dict] = {}

# ---------------------------------------------------------------------------
# Video Downloader store (in-memory only — downloads are one-time and
# expire after DOWNLOAD_TTL_SECONDS, so nothing is persisted to disk
# beyond the temp file itself, which is deleted when served or expired).
# Record shape:
#   id, path, filename, size, content_type, title, platform, items,
#   expires_at (datetime), created_at (iso)
# ---------------------------------------------------------------------------
downloads: dict[str, dict] = {}


def _delete_download(download_id: str) -> None:
    """Remove a download record and its temp directory (best effort)."""
    record = downloads.pop(download_id, None)
    if record is not None:
        shutil.rmtree(Path(record["path"]).parent, ignore_errors=True)


def _sweep_expired_downloads() -> None:
    """Delete download records (and their temp files) past expiry."""
    now = datetime.now(timezone.utc)
    for download_id, record in list(downloads.items()):
        expires_at = record.get("expires_at")
        if isinstance(expires_at, datetime) and expires_at <= now:
            _delete_download(download_id)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _job_dir(job_id: str) -> Path:
    return JOB_DIR / job_id


def _save_job(job: dict) -> None:
    """Persist a job's meta + transcript to disk (best effort)."""
    try:
        directory = _job_dir(job["id"])
        (directory / "clips").mkdir(parents=True, exist_ok=True)
        meta = {k: v for k, v in job.items() if k != "transcript"}
        (directory / "meta.json").write_text(
            json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        (directory / "transcript.json").write_text(
            json.dumps(job.get("transcript", []), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except OSError:
        pass


def _load_jobs_from_disk() -> None:
    """Reload jobs persisted by a previous run (if any)."""
    if not JOB_DIR.exists():
        return
    for directory in sorted(JOB_DIR.iterdir()):
        if not directory.is_dir():
            continue
        meta_path = directory / "meta.json"
        if not meta_path.exists():
            continue
        try:
            job = json.loads(meta_path.read_text(encoding="utf-8"))
            transcript_path = directory / "transcript.json"
            job["transcript"] = (
                json.loads(transcript_path.read_text(encoding="utf-8"))
                if transcript_path.exists()
                else []
            )
            job.setdefault("highlights", [])
            job.setdefault("clips", [])
            # A job that was mid-flight when the server stopped is not
            # resumed automatically; surface it as an error, not a hang.
            if job.get("status") in ("queued", "downloading", "transcribing", "rendering"):
                job["status"] = "error"
                job["message"] = "Server restarted while this job was running."
                job["progress"] = job.get("progress", 0)
            jobs[job["id"]] = job
        except (OSError, json.JSONDecodeError, KeyError):
            continue


_load_jobs_from_disk()

TERMINAL_STATUSES = {"ready", "done", "error"}


def prune_old_jobs() -> int:
    """Delete terminal jobs (ready/done/error) older than JOB_RETENTION_DAYS.

    Removes both the job directory on disk (source video, transcript,
    rendered clips) and the entry in the in-memory job store. Jobs
    still running are never touched; jobs whose creation time is
    missing or unparseable are kept rather than deleted blindly.
    Returns the number of jobs pruned.
    """
    cutoff = datetime.now(timezone.utc) - timedelta(days=JOB_RETENTION_DAYS)
    pruned = 0
    for job_id, job in list(jobs.items()):
        if job.get("status") not in TERMINAL_STATUSES:
            continue
        try:
            created = datetime.fromisoformat(str(job.get("created_at", "")))
        except ValueError:
            continue
        if created.tzinfo is None:
            created = created.replace(tzinfo=timezone.utc)
        if created >= cutoff:
            continue
        shutil.rmtree(_job_dir(job_id), ignore_errors=True)
        jobs.pop(job_id, None)
        pruned += 1
    return pruned


prune_old_jobs()


def _set_status(job: dict, status: str, progress: int, message: str = "") -> None:
    job["status"] = status
    job["progress"] = progress
    if message:
        job["message"] = message
    _save_job(job)
    if status in TERMINAL_STATUSES:
        # The job just finished — drop any other finished jobs that
        # have aged past the retention window (free-tier disk hygiene).
        prune_old_jobs()


def _job_summary(job: dict) -> dict:
    return {
        "id": job["id"],
        "url": job["url"],
        "title": job.get("title"),
        "duration": job.get("duration"),
        "status": job["status"],
        "progress": job.get("progress", 0),
        "message": job.get("message", ""),
        "created_at": job["created_at"],
    }


def _job_detail(job: dict) -> dict:
    detail = _job_summary(job)
    detail.update({
        "thumbnail": job.get("thumbnail"),
        "transcript": job.get("transcript", []),
        "highlights": job.get("highlights", []),
        "clips": job.get("clips", []),
    })
    return detail


def _get_job_or_404(job_id: str) -> dict:
    job = jobs.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found.")
    return job


# ---------------------------------------------------------------------------
# Background pipeline: metadata -> download -> transcribe -> highlights
# ---------------------------------------------------------------------------
def _run_pipeline(job_id: str) -> None:
    job = jobs.get(job_id)
    if job is None:
        return
    job_dir = _job_dir(job_id)
    audio_path = job_dir / "audio.m4a"
    try:
        _set_status(job, "downloading", 10, "Fetching video info…")
        meta = fetch_metadata(job["url"], player_client="android")
        job["title"] = meta.get("title")
        job["duration"] = meta.get("duration")
        job["thumbnail"] = meta.get("thumbnail")
        duration = meta.get("duration")
        if duration is not None and duration > MAX_DURATION:
            raise ValueError(
                f"This video is {duration / 3600:.1f} hours long, "
                f"over the {MAX_DURATION / 3600:.1f} hour limit."
            )
        # Clipping never downloads the full video: audio-only is enough
        # for the transcript + highlight detection (small and fast), and
        # each clip's video is fetched as just its section at render time.
        _set_status(job, "downloading", 25, "Downloading audio…")
        download_audio(job["url"], str(audio_path), player_client="android")
        _set_status(job, "transcribing", 60, "Transcribing audio…")
        try:
            transcript = transcribe(str(audio_path), model=WHISPER_MODEL)
        except TranscriptionUnavailable:
            # Transcription is optional: the job is still usable for
            # manual renders (hook + CTA, no captions).
            job["transcript"] = []
            job["highlights"] = []
            _set_status(
                job, "ready", 100,
                "Transcription is unavailable on this install — you can "
                "still render clips with manual hook text and no captions.",
            )
            return
        job["transcript"] = transcript
        _set_status(job, "transcribing", 85, "Finding highlights…")
        loudness = None
        try:
            loudness = get_loudness_curve(str(audio_path))
        except Exception:
            loudness = None
        job["highlights"] = suggest_highlights(
            transcript, job.get("duration"), loudness_curve=loudness
        )
        _set_status(job, "ready", 100, "Ready — pick a highlight or set a custom clip.")
    except (ValueError, RuntimeError, FileNotFoundError) as exc:
        _set_status(job, "error", job.get("progress", 0), str(exc))
    except Exception:
        # Never leak a traceback to the client; keep the message generic.
        _set_status(job, "error", job.get("progress", 0),
                    "Something went wrong while processing this video. Please try again.")


# ---------------------------------------------------------------------------
# Background render: render one clip, then append/update it in clips[]
# ---------------------------------------------------------------------------
def _run_render(job_id: str, clip_id: str) -> None:
    job = jobs.get(job_id)
    if job is None:
        return
    clip = next((c for c in job.get("clips", []) if c["id"] == clip_id), None)
    if clip is None:
        return
    job_dir = _job_dir(job_id)
    output_path = job_dir / "clips" / f"{clip_id}.mp4"
    start = float(clip["start"])
    end = float(clip["end"])
    length = end - start
    # Transcript segments overlapping the clip, shifted so 0 == the
    # section file's start (the section starts exactly at clip start).
    overlapping = [
        seg for seg in job.get("transcript", [])
        if float(seg.get("end", 0)) > start and float(seg.get("start", 0)) < end
    ]
    # Only this clip's video section is downloaded — never the full video.
    section_path = job_dir / "sections" / f"{clip_id}.mp4"
    fallback_note = ""
    try:
        download_section(
            job["url"], start, end, str(section_path), player_client="android"
        )
        render_source = str(section_path)
        render_start, render_end = 0.0, length
        segments = [
            {**seg,
             "start": float(seg.get("start", 0)) - start,
             "end": float(seg.get("end", 0)) - start}
            for seg in overlapping
        ]
    except (ValueError, RuntimeError, FileNotFoundError):
        # Section downloads are not supported by every platform: fall
        # back to the full video for this clip (the old behaviour).
        fallback_note = " (section download unavailable — used the full video)"
        source_path = job_dir / "source.mp4"
        if not source_path.exists():
            download_video(job["url"], str(source_path), MAX_FILE_MB)
        render_source = str(source_path)
        render_start, render_end = start, end
        segments = overlapping
    try:
        info = render_clip(
            render_source,
            render_start,
            render_end,
            clip.get("hook_text", ""),
            clip.get("cta_text", ""),
            str(output_path),
            segments=segments,
        )
        # Update the clip record in place so GET /api/jobs/{id} -> clips[]
        # shows the finished file (size/duration) after the render.
        clip["size"] = info["size"]
        clip["duration"] = info["duration"]
        clip["filename"] = output_path.name
        _set_status(job, "done", 100, "Clip ready." + fallback_note)
    except (ValueError, RuntimeError, FileNotFoundError) as exc:
        clip["size"] = 0
        _set_status(job, "error", job.get("progress", 100),
                    f"Render failed: {exc}")
    except Exception:
        clip["size"] = 0
        _set_status(job, "error", job.get("progress", 100),
                    "Render failed: something went wrong while rendering the clip.")


# ---------------------------------------------------------------------------
# API
# ---------------------------------------------------------------------------
class CreateJobRequest(BaseModel):
    url: str


class RenderRequest(BaseModel):
    start: float
    end: float
    hook_text: str = ""
    cta_text: str = ""


class GenerateClipsRequest(BaseModel):
    # How many clips to make (1-10) and how long each one is in seconds
    # (15-60). The backend picks the best moments and returns the clip
    # windows for the editor — nothing is rendered yet, so the user can
    # still adjust each clip's start/end before rendering it.
    count: int = 3
    clip_seconds: float = 30.0


class DownloadRequest(BaseModel):
    url: str
    # How many playlist items to download (0 = all, up to 10). Ignored
    # for a single-video URL. Validated in the handler for a friendly 400.
    limit: int = 0
    # Max resolution: "best" (up to 1080p), "1080", "720", "480", "360".
    quality: str = "best"
    # Optional per-video size cap in MB (None = no cap). If no format
    # fits the cap at the chosen quality, the download fails with a
    # clear "no format fits" message instead of a generic error.
    max_size_mb: float | None = None


class LoginRequest(BaseModel):
    password: str = ""


@app.get("/api/health")
def health() -> dict:
    return {"status": "ok", "version": VERSION}


@app.post("/api/login")
def login(payload: LoginRequest) -> JSONResponse:
    """Check the password and, if correct, issue a bearer token.

    The entered password is hashed with SHA-256 and compared against the
    stored hash with a constant-time compare. A wrong password returns
    401 with no hint about the stored hash. On success the client
    stores the returned token and sends it as
    `Authorization: Bearer <token>` on every other /api/* call.
    """
    entered_hash = hashlib.sha256(payload.password.encode("utf-8")).hexdigest()
    if not hmac.compare_digest(entered_hash, APP_PASSWORD_HASH):
        raise HTTPException(status_code=401, detail="Wrong password")
    token = secrets.token_urlsafe(32)
    tokens.add(token)
    return JSONResponse(content={"ok": True, "token": token})


@app.post("/api/logout")
def logout(request: Request) -> JSONResponse:
    """Invalidate the bearer token from the Authorization header."""
    token = _token_from_request(request)
    if token:
        tokens.discard(token)
    return JSONResponse(content={"ok": True})


@app.get("/api/social-links")
def social_links() -> dict:
    """Return the Manan Automation brand block + social links (auth required)."""
    return {
        "brand_line": BRAND_LINE,
        "tagline": TAGLINE,
        "contact": {"phone": CONTACT_PHONE, "url": CONTACT_URL},
        "links": list(SOCIAL_LINKS),
    }


@app.get("/login", response_model=None)
def login_page() -> Response:
    """Serve the standalone login page (web/login.html)."""
    login_html = WEB_DIR / "login.html"
    if login_html.exists():
        return FileResponse(login_html, media_type="text/html")
    return JSONResponse(
        status_code=503,
        content={"detail": "Login page (web/login.html) is not installed yet."},
    )


@app.post("/api/jobs", status_code=201)
def create_job(payload: CreateJobRequest, background_tasks: BackgroundTasks) -> JSONResponse:
    try:
        url = validate_url(payload.url)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    job_id = uuid.uuid4().hex[:16]
    job: dict = {
        "id": job_id,
        "url": url,
        "title": None,
        "duration": None,
        "thumbnail": None,
        "status": "queued",
        "progress": 0,
        "message": "Queued — starting download…",
        "created_at": _now_iso(),
        "transcript": [],
        "highlights": [],
        "clips": [],
    }
    jobs[job_id] = job
    _save_job(job)
    background_tasks.add_task(_run_pipeline, job_id)
    return JSONResponse(
        status_code=201,
        content={
            "id": job["id"],
            "url": job["url"],
            "status": job["status"],
            "created_at": job["created_at"],
        },
    )


@app.get("/api/jobs")
def list_jobs() -> list[dict]:
    ordered = sorted(jobs.values(), key=lambda j: j["created_at"], reverse=True)
    return [_job_summary(job) for job in ordered]


@app.get("/api/jobs/{job_id}")
def get_job(job_id: str) -> dict:
    return _job_detail(_get_job_or_404(job_id))


@app.post("/api/jobs/{job_id}/render", status_code=201)
def render_job_clip(
    job_id: str, payload: RenderRequest, background_tasks: BackgroundTasks
) -> JSONResponse:
    job = _get_job_or_404(job_id)
    if job["status"] not in ("ready", "done"):
        raise HTTPException(
            status_code=400,
            detail=f"Job is {job['status']} — clips can only be rendered once the job is ready.",
        )
    start = float(payload.start)
    end = float(payload.end)
    if start < 0:
        raise HTTPException(status_code=400, detail="Clip start must be 0 or later.")
    if end <= start:
        raise HTTPException(status_code=400, detail="Clip end must be after clip start.")
    if end - start > MAX_CLIP_SECONDS:
        # Clamp, don't reject: the contract allows windows over 60 s.
        end = start + MAX_CLIP_SECONDS
    clip_id = uuid.uuid4().hex[:16]
    clip: dict = {
        "id": clip_id,
        "start": start,
        "end": end,
        "hook_text": payload.hook_text or "",
        "cta_text": payload.cta_text or "",
        "filename": f"{clip_id}.mp4",
        "size": 0,
        "duration": round(end - start, 2),
        "created_at": _now_iso(),
    }
    # Append the clip record BEFORE the background render starts, so the
    # clip is visible in GET /api/jobs/{id} -> clips[] immediately, and
    # _run_render updates this same record (size/duration) on completion.
    job["clips"].append(clip)
    _set_status(job, "rendering", 100, "Rendering clip…")
    background_tasks.add_task(_run_render, job_id, clip_id)
    return JSONResponse(status_code=201, content=dict(clip))


@app.post("/api/jobs/{job_id}/generate-clips")
def generate_job_clips(job_id: str, payload: GenerateClipsRequest) -> JSONResponse:
    """Generate N clips of X seconds from the job's best moments.

    Returns {"clips": [{start, end, score, reasons}], "note": str|None,
    "count": int, "clip_seconds": float}. Nothing is rendered here —
    the frontend shows the windows in the editor, where each clip's
    start/end stays editable before it is rendered.
    """
    job = _get_job_or_404(job_id)
    if job["status"] not in ("ready", "done"):
        raise HTTPException(
            status_code=400,
            detail=f"Job is {job['status']} — clips can only be generated once the job is ready.",
        )
    count = payload.count
    if count < 1 or count > 10:
        raise HTTPException(
            status_code=400, detail="Number of clips must be between 1 and 10."
        )
    clip_seconds = float(payload.clip_seconds)
    if clip_seconds < 15 or clip_seconds > 60:
        raise HTTPException(
            status_code=400,
            detail="Clip length must be between 15 and 60 seconds.",
        )
    duration = job.get("duration")
    if not duration and job.get("transcript"):
        duration = max(float(seg.get("end", 0)) for seg in job["transcript"])
    # Recompute the loudness curve for the ranking (it is not persisted
    # on the job); if it fails, the ranking falls back to transcript
    # signals only — same as the pipeline does.
    loudness = None
    source_path = _job_dir(job_id) / "source.mp4"
    if source_path.exists():
        try:
            loudness = get_loudness_curve(str(source_path))
        except Exception:
            loudness = None
    clips, note = generate_clips(
        job.get("transcript", []), duration,
        loudness_curve=loudness, count=count, clip_seconds=clip_seconds,
    )
    return JSONResponse(content={
        "clips": clips,
        "note": note,
        "count": len(clips),
        "clip_seconds": clip_seconds,
    })


@app.get("/api/clips/{clip_id}")
def get_clip(clip_id: str) -> FileResponse:
    for job in jobs.values():
        clip = next((c for c in job.get("clips", []) if c["id"] == clip_id), None)
        if clip is not None:
            path = _job_dir(job["id"]) / "clips" / f"{clip_id}.mp4"
            if not path.exists():
                raise HTTPException(status_code=404, detail="Clip file not found (render may still be running).")
            return FileResponse(path, media_type="video/mp4", filename=clip.get("filename", f"{clip_id}.mp4"))
    raise HTTPException(status_code=404, detail="Clip not found.")


@app.delete("/api/clips/{clip_id}")
def delete_clip(clip_id: str) -> dict:
    for job in jobs.values():
        clips = job.get("clips", [])
        clip = next((c for c in clips if c["id"] == clip_id), None)
        if clip is not None:
            job["clips"] = [c for c in clips if c["id"] != clip_id]
            path = _job_dir(job["id"]) / "clips" / f"{clip_id}.mp4"
            try:
                if path.exists():
                    path.unlink()
            except OSError:
                pass
            _save_job(job)
            return {"deleted": True}
    raise HTTPException(status_code=404, detail="Clip not found.")


# ---------------------------------------------------------------------------
# Video Downloader — the website version of the Termux Manan Downloader.
# POST is token-gated (the auth middleware gates every /api/* route that
# is not public); GET is public on purpose, see _is_public_api_route.
# The POST is synchronous and runs yt-dlp in the threadpool, so a long
# video can take a while — the frontend shows the wake-up notice while
# it waits. One download = one file (a playlist becomes a zip).
# ---------------------------------------------------------------------------
@app.post("/api/download", status_code=201)
def create_download(payload: DownloadRequest) -> JSONResponse:
    _sweep_expired_downloads()
    try:
        url = validate_download_url(payload.url)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    limit = payload.limit
    if limit < 0 or limit > 10:
        raise HTTPException(
            status_code=400,
            detail="Quantity must be between 0 (all) and 10.",
        )
    quality = (payload.quality or "best").strip().lower()
    if quality not in QUALITY_HEIGHTS:
        raise HTTPException(
            status_code=400,
            detail="Quality must be one of: Best, 1080p, 720p, 480p, 360p.",
        )
    max_size_mb = payload.max_size_mb
    if max_size_mb is not None and max_size_mb <= 0:
        raise HTTPException(
            status_code=400,
            detail="Max size must be more than 0 MB, or leave it blank for no cap.",
        )
    download_id = uuid.uuid4().hex[:16]
    work_dir = DOWNLOADS_DIR / download_id
    try:
        files, title = download_files(
            url, str(work_dir), limit, MAX_FILE_MB, MAX_DURATION,
            quality=quality, max_size_mb=max_size_mb,
        )
    except ValueError as exc:
        shutil.rmtree(work_dir, ignore_errors=True)
        raise HTTPException(status_code=400, detail=str(exc))
    except RuntimeError as exc:
        # Upstream failure (bot check, login-required, age-restricted,
        # platform blocked the download): 502, with the friendly message.
        shutil.rmtree(work_dir, ignore_errors=True)
        raise HTTPException(status_code=502, detail=str(exc))
    except Exception:
        shutil.rmtree(work_dir, ignore_errors=True)
        raise HTTPException(
            status_code=500,
            detail="Something went wrong while downloading. Please try again.",
        )
    if not files:
        shutil.rmtree(work_dir, ignore_errors=True)
        raise HTTPException(
            status_code=502, detail="Download finished but no video file was found."
        )

    if len(files) == 1:
        path = Path(files[0])
        filename = path.name
        content_type = "video/mp4"
    else:
        # A playlist: zip the videos into one file so the browser gets a
        # single download, then drop the originals to save disk.
        zip_name = "videos.zip"
        zip_path = work_dir / zip_name
        try:
            with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
                for f in files:
                    zf.write(f, arcname=Path(f).name)
        except OSError:
            shutil.rmtree(work_dir, ignore_errors=True)
            raise HTTPException(
                status_code=500,
                detail="Could not package the downloaded videos. Please try again.",
            )
        for f in files:
            try:
                os.remove(f)
            except OSError:
                pass
        path = zip_path
        filename = zip_name
        content_type = "application/zip"

    size = path.stat().st_size
    expires_at = datetime.now(timezone.utc) + timedelta(seconds=DOWNLOAD_TTL_SECONDS)
    downloads[download_id] = {
        "id": download_id,
        "path": str(path),
        "filename": filename,
        "size": size,
        "content_type": content_type,
        "title": title,
        "platform": platform_for_url(url),
        "items": len(files),
        "expires_at": expires_at,
        "created_at": _now_iso(),
    }
    return JSONResponse(
        status_code=201,
        content={
            "id": download_id,
            "download_url": f"/api/downloads/{download_id}",
            "filename": filename,
            "size": size,
            "content_type": content_type,
            "title": title,
            "platform": platform_for_url(url),
            "items": len(files),
            "expires_in": DOWNLOAD_TTL_SECONDS,
        },
    )


@app.get("/api/downloads/{download_id}")
def get_download(download_id: str, background_tasks: BackgroundTasks) -> FileResponse:
    """Serve a one-time download link, then delete the temp file.

    The file is deleted by a background task that runs after the
    response is sent, so the link works exactly once. Expired links
    (older than DOWNLOAD_TTL_SECONDS) are swept first and return 404.
    """
    _sweep_expired_downloads()
    record = downloads.get(download_id)
    if record is None:
        raise HTTPException(
            status_code=404, detail="Download link not found or expired."
        )
    path = Path(record["path"])
    if not path.exists():
        _delete_download(download_id)
        raise HTTPException(
            status_code=404, detail="Download link not found or expired."
        )
    background_tasks.add_task(_delete_download, download_id)
    return FileResponse(
        path,
        media_type=record["content_type"],
        filename=record["filename"],
    )


# ---------------------------------------------------------------------------
# Frontend: serve the whole web/ directory as static files.
#   /            -> web/index.html
#   /app.js      -> web/app.js
#   /style.css   -> web/style.css
#   /assets/logo.jpg -> web/assets/logo.jpg (header img + favicon)
# ---------------------------------------------------------------------------
if WEB_DIR.exists():
    app.mount("/", StaticFiles(directory=str(WEB_DIR), html=True), name="web")
else:
    @app.get("/")
    def _missing_frontend() -> JSONResponse:
        return JSONResponse(
            status_code=503,
            content={"detail": "Frontend (web/) is not installed yet."},
        )
