# Manan Clipping Tool

Turn a long YouTube or Facebook video into vertical 9:16 clips (up to 60 seconds each) with captions, a hook line at the top and a call-to-action at the bottom.

Paste a link → the Manan Clipping Tool downloads the video, transcribes it (Whisper), suggests the 8 most exciting 45-second windows (loudness peaks + excitement keywords + speech density), and renders any window you pick as a 1080×1920 MP4 ready for TikTok / Reels / Shorts.

**Stack:** Python 3.11+ · FastAPI + uvicorn · yt-dlp (subprocess CLI) · faster-whisper (fallback: openai-whisper) · system ffmpeg. No other heavy dependencies.

---

## Quick start (local)

### Prerequisites

- Python 3.11 or newer (`python3 --version`)
- ffmpeg + ffprobe on your PATH (`ffmpeg -version`)
- pip

### Install

```bash
git clone <your-repo-url> clip-extractor
cd clip-extractor
python3 -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

`requirements.txt` installs `fastapi`, `uvicorn[standard]`, `yt-dlp` and `faster-whisper` (which pulls in `ctranslate2`). `openai-whisper` is listed there as a commented-out optional fallback — uncomment it only if you want the fallback transcription engine.

Copy the example config and adjust if needed:

```bash
cp .env.example .env
```

| Variable | Default | Meaning |
|---|---|---|
| `JOB_DIR` | `./jobs` (relative to project root) | Where sources, transcripts and clips are stored |
| `MAX_DURATION` | `14400` (4 h) | Longest source video accepted, in seconds |
| `MAX_FILE_MB` | `2048` | Largest downloaded source file, in MB |
| `WHISPER_MODEL` | `tiny` | Whisper model — keep `tiny` on small machines |
| `HOST` | `0.0.0.0` | Bind address |
| `PORT` | `8000` | Listen port |
| `APP_PASSWORD_HASH` | no default — login fails until it is set | SHA-256 hex digest of the login password (see "Password protection" below) |
| `CORS_ORIGINS` | `*` (any origin) | Comma-separated origins allowed to call the API cross-origin (e.g. the Muse Space frontend); `*` allows all |
| `JOB_RETENTION_DAYS` | `7` | Finished jobs (ready/done/error) older than this many days are deleted — download clips promptly |
| `DOWNLOADS_DIR` | `./downloads` (relative to project root) | Where Video Downloader temp files live (one folder per download; deleted after serving or after 10 minutes) |
| `DOWNLOAD_TTL_SECONDS` | `600` (10 min) | How long a Video Downloader link stays valid |

### Run locally

```bash
uvicorn server:app --reload
```

Then open <http://localhost:8000> — the web UI is served from `web/` by the same server. Health check: <http://localhost:8000/api/health> should return `{"status":"ok","version":"0.1.0"}`.

### How to use it

1. Paste a YouTube or Facebook video URL and submit — a job is created (`queued → downloading → transcribing → ready`).
2. When the job is `ready`, the transcript and up to 8 suggested highlights appear.
3. Pick a highlight (or set a custom start/end, max 60 s), type a hook line and a CTA, and render.
4. When rendering finishes (`done`), the clip appears in the job's clip list — download it from `GET /api/clips/{clip_id}`.

### Video Downloader

The **Video downloader** tab is the website version of the Termux **Manan Downloader** (`manan_downloader.py` in the `Hasni2130289/manan-automation` repo): same idea — paste a link, get the video file — but in the browser instead of Termux, behind the same password gate.

1. Open the **Video downloader** tab, paste a **TikTok, Facebook or YouTube** video link.
2. Set **how many videos** (0 = all, for playlists — max 10). For a single video the number does not matter.
3. Press **Download**. The server fetches the video at best quality (up to 1080p) and the file downloads in your browser.

What you get back: one MP4 file (named after the video title). A playlist comes back as a single zip file with up to 10 videos. The download link is one-time and expires after 10 minutes — save the file right away; the server deletes it after it is served once (or when it expires).

**Limits (honest version):**

- **Best quality, up to 1080p** — the Termux tool used unbounded `best`; the website caps at 1080p so files stay small on the free tier.
- **Playlists capped at 10 videos.** A `/watch` link with a `list=` parameter downloads just that one video — paste the `/playlist` link to get the list.
- **File size cap** is `MAX_FILE_MB` (default 2048 MB locally; the Render Blueprint sets 512 MB). Single videos longer than `MAX_DURATION` are rejected.
- **YouTube** usually works for public videos, but age-restricted, login-required, private or bot-blocked videos fail with a clear message.
- **Facebook** only sometimes works for public videos and breaks often; anything needing login will not download.
- **TikTok often blocks downloads.** If a TikTok link fails, try a YouTube or Facebook link instead.
- **Long downloads may time out** on the free tier — short-to-medium videos work best.

**Copyright:** only download videos you have the right to download. Downloading other creators' content without permission can draw claims or strikes — the same warning as for clipping.

### API (summary)

Auth is token-based: log in once, then send the token as `Authorization: Bearer <token>` on every API call except the public ones below. The frontend stores the token (localStorage) and shows its own login view when there is no token — page loads never redirect to a login page.

| Method | Endpoint | Description |
|---|---|---|
| `GET` | `/api/health` | `{"status":"ok","version":"0.1.0"}` (public, no token needed) |
| `POST` | `/api/login` `{"password": "..."}` | Log in → `{"ok": true, "token": "..."}`; wrong password → `401 {"detail": "Wrong password"}` (public) |
| `POST` | `/api/logout` | Log out: invalidates the bearer token → `{"ok": true}` (token required) |
| `GET` | `/api/social-links` | The social links from `config.py` (token required) |
| `POST` | `/api/jobs` `{"url": "..."}` | Create a job → `201 {"id","url","status":"queued","created_at"}`; `400` for invalid/blocked URLs (token required) |
| `GET` | `/api/jobs` | List of job summaries (token required) |
| `GET` | `/api/jobs/{job_id}` | Full job incl. `transcript`, `highlights`, `clips` (token required) |
| `POST` | `/api/jobs/{job_id}/render` `{"start","end","hook_text","cta_text"}` | Render a clip → `201` clip object (windows over 60 s are clamped to 60 s) (token required) |
| `GET` | `/api/clips/{clip_id}` | Download the rendered MP4 (public — clip IDs are unguessable UUIDs, and `<video>` previews / download links cannot send an auth header cross-origin; the jobs API stays token-gated so clips cannot be listed or discovered without the password) |
| `DELETE` | `/api/clips/{clip_id}` | Delete a clip → `{"deleted": true}` (token required) |
| `POST` | `/api/download` `{"url": "...", "limit": 0}` | Download a video (token required) → `201 {"id","download_url","filename","size","content_type","platform","items","expires_in"}`; `limit` is the playlist quantity (0 = all, max 10); a playlist comes back as a zip. `400` for invalid URLs/quantity, `502` when the platform blocks the download |
| `GET` | `/api/downloads/{download_id}` | Download the file from `POST /api/download` (public — one-time, unguessable UUID, expires after 10 minutes; the file is deleted after it is served) |

Unauthenticated calls to a token-gated endpoint return `401 {"detail": "Authentication required."}`. Static files (`/`, `/app.js`, `/style.css`, `/assets/logo.jpg`, `/login`) are public; the app shell is useless without the API.

Only these hosts are accepted: `youtube.com`, `www.youtube.com`, `m.youtube.com`, `youtu.be`, `facebook.com`, `www.facebook.com`, `fb.watch`, `www.fb.watch`. Anything else is rejected with `400`. The Video Downloader additionally accepts TikTok (`tiktok.com`, `www.tiktok.com`, `m.tiktok.com`, `vt.tiktok.com`, `vm.tiktok.com`).

---

## Password protection

The app sits behind a login screen. Open the app, enter the password, and the server returns a bearer token (`POST /api/login` → `{"ok": true, "token": "..."}`). The frontend stores that token and sends it as an `Authorization: Bearer <token>` header on every API call. `POST /api/logout` invalidates the token. There are no cookies anywhere — cookies do not work cross-origin, and the frontend may live on a different origin (a Muse Space link) than this API.

Public routes (no token needed): `POST /api/login`, `GET /api/health`, `GET /api/clips/{clip_id}`, `GET /api/downloads/{download_id}` (one-time, expires after 10 minutes), and all static files. Every other `/api/*` route returns `401 {"detail": "Authentication required."}` without a valid token.

### How to change the password

The password itself is never stored anywhere — only its SHA-256 hex digest. To set a new password:

```bash
printf '%s' 'new-password' | sha256sum
```

Take the hex digest that command prints and set it as the `APP_PASSWORD_HASH` environment variable (in `.env` for local/systemd runs, or in the Render/Railway dashboard for hosted runs), then restart the server. Never put the plain password in a file, in the README, or in a chat message — only the hash.

---

## Social links

The app's "Manan Automation" social links live in exactly one place: `config.py` — the brand line, tagline, WhatsApp contact row, and the `SOCIAL_LINKS` list. To change any link, edit that one file and restart the server. The frontend fetches them from `GET /api/social-links` (which returns `{"brand_line", "tagline", "contact": {"phone", "url"}, "links": [...]}`) — no other file needs to change, except the login page: `web/login.html` mirrors the same list inline because it loads before any login exists, so update its copy too when a link changes.

---

## Where can this run?

**Your Hostinger Business shared hosting plan cannot run this app.** Python and FFmpeg are supported exclusively on Hostinger VPS — shared / Web / Cloud plans have no persistent Python process and no FFmpeg. Your Node.js "Web app" sites (digitalstore.manansmm.com, onlinestore.manansmm.com, tools.manansmm.com) work on shared hosting because Hostinger supports Node.js there, but Python does not get the same treatment — so the WordPress, PHP/HTML, and "Web App" options on your plan cannot run this tool.

That leaves two routes, cheapest first:

1. **Render free tier — recommended, no extra cost (primary route).** One web service serves the frontend AND the API, so the Render URL is the single public link — share it with anyone (they hit the password gate first, then the full tool). Optionally point a subdomain such as `clips.manansmm.com` at it from your Hostinger DNS panel. Step-by-step below.
2. **Hostinger VPS (KVM, Ubuntu) — paid alternative.** Short pointer below; the generic "Deploy on a VPS" section has the Docker/systemd steps.

---

## Deploy on Render (free tier — recommended)

This is the primary route — and the **single public link**. The Blueprint deploys **one web service that serves the frontend (`web/`) AND the API together**, so the Render URL (e.g. `https://manan-clipping-tool.onrender.com`) opens the full tool: login page with the password gate, then clipping, transcripts, highlights, and renders. **Share that one link with anyone** — they will be asked for the password first, and nothing works without it. No separate frontend hosting is needed; the Muse Space copy is just your private copy in Muse. Your domain stays at Hostinger — optionally point a subdomain at the service (step 3). The repo ships a `render.yaml` Blueprint, so Render picks up the whole configuration automatically. The Blueprint uses the **Docker** runtime (required — the `Dockerfile` installs ffmpeg, which Render's native Python environment does not have).

### 1. Put the code in a GitHub repo

```bash
cd manan-clipping-tool
git init
git add .
git commit -m "Manan Clipping Tool"
# create an empty repo on github.com, then:
git remote add origin https://github.com/<your-username>/manan-clipping-tool.git
git push -u origin main
```

Do **not** commit a `.env` file or any password — secrets go in the platform's environment-variable settings, never in git.

### 2. Create the service from the Blueprint

1. Render Dashboard → **New → Blueprint** → connect your repo (GitHub, GitLab or Bitbucket).
2. `render.yaml` is picked up automatically: one free web service, Docker runtime, health check on `/api/health`, and the environment variables `WHISPER_MODEL=tiny`, `MAX_DURATION=3600`, `MAX_FILE_MB=512`, `JOB_RETENTION_DAYS=7`.
3. Set `APP_PASSWORD_HASH` in the dashboard (it is marked `sync: false` in `render.yaml`, so it is never stored in the file): generate it locally with `printf '%s' 'your-password' | sha256sum` and paste **only the hex digest**. Never paste the plain password anywhere else.
4. Click **Deploy**. Render gives you a URL like `https://manan-clipping-tool.onrender.com` — open it, you should see the login page. **This URL is the single shareable link for the whole tool** (frontend + API on one service) — send it to anyone; they log in with the password and get the full app. (The free service sleeps after inactivity, so the first open can take ~30–60 s to wake.)

### 3. Point a subdomain at it from your Hostinger panel (optional — nicer link)

Use a subdomain such as `clips.manansmm.com` if you want a branded link instead of the `onrender.com` URL. (A subdomain is required — a `CNAME` cannot sit on the bare domain `manansmm.com` itself.)

1. Hostinger hPanel → **Domains → DNS** (for `manansmm.com`).
2. Add a record: Type `CNAME`, Name/host `clips`, Value/target `<your-service>.onrender.com`, TTL default.
3. Then in Render → your service → **Settings → Custom Domains** → add `clips.manansmm.com` (Render issues the HTTPS certificate automatically).
4. Wait for DNS to propagate (usually minutes, up to a few hours), then open `https://clips.manansmm.com` — the Manan Clipping Tool login page should load with HTTPS.

### Free-tier tuning (read before you deploy)

- **RAM is tight (512 MB on Render free).** Whisper `tiny` is the only model that fits — do not raise `WHISPER_MODEL`. `base` is the absolute max and will likely OOM; anything larger will not run. Transcribing a long video may still OOM; if it does, transcription fails gracefully (the job becomes `ready` with a message and manual rendering still works).
- **Disk is ephemeral.** The filesystem is wiped on every deploy/restart, temp files are cleaned after each job, and finished jobs older than `JOB_RETENTION_DAYS` (default 7) are deleted automatically — download clips promptly, they will not survive a restart.
- **Sleep / cold starts.** The free service sleeps after inactivity, so the first request after a pause can take 30–60 s to wake.
- **Long downloads may time out.** Free-tier request timeouts can interrupt large downloads or long transcriptions. Short-to-medium videos work best; a 4-hour source is unlikely to finish on a free instance — that is why the Blueprint sets `MAX_DURATION=3600` and `MAX_FILE_MB=512`.
- **No GPU.** Everything runs on CPU; transcription speed is roughly real-time or slower on free hardware.

### Railway (short alternative)

Same idea on Railway's free tier: Dashboard → **New Project → Deploy from GitHub repo** → pick the repo (Railway detects the `Dockerfile`) → **Variables** tab — add the same variables as above (`APP_PASSWORD_HASH` as the SHA-256 hex digest only, `WHISPER_MODEL=tiny`, `MAX_DURATION=3600`, `MAX_FILE_MB=512`) → **Settings → Networking → Generate Domain** — Railway gives you a public domain like `manan-clipping-tool.up.railway.app`. Open it and check the login page loads. For the subdomain, use the same CNAME approach as Render but with the Railway domain as the target, then add `clips.manansmm.com` under Railway → your service → **Settings → Networking → Custom Domain**. (If Railway instead shows you an IP, use an `A` record pointing at that IP.)

---

## Deploy on a VPS

### With Docker (recommended)

```bash
docker build -t manan-clipping-tool .
docker run -d --name manan-clipping-tool -p 8000:8000 \
  -v /srv/manan-clipping-tool/jobs:/app/jobs \
  -e WHISPER_MODEL=tiny \
  manan-clipping-tool
```

Mounting a volume at `/app/jobs` keeps downloads and clips across restarts. Put a reverse proxy (nginx / Caddy) in front for HTTPS.

### With systemd (no Docker)

```bash
# on the server, as a dedicated user
python3 -m venv /opt/manan-clipping-tool/.venv
/opt/manan-clipping-tool/.venv/bin/pip install -r requirements.txt
sudo apt-get install -y ffmpeg fonts-dejavu-core
```

`/etc/systemd/system/manan-clipping-tool.service`:

```ini
[Unit]
Description=Manan Clipping Tool
After=network.target

[Service]
User=www-data
WorkingDirectory=/opt/manan-clipping-tool
EnvironmentFile=/opt/manan-clipping-tool/.env
ExecStart=/opt/manan-clipping-tool/.venv/bin/uvicorn server:app --host 0.0.0.0 --port 8000
Restart=on-failure

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now manan-clipping-tool
```

Give the machine at least 2 CPU / 4 GB RAM for comfortable use; transcription is CPU-bound and the `tiny` model is the only sensible default on a small VPS. Disk fills up with downloaded sources — set `JOB_DIR` on a volume with room, and prune old jobs regularly (each job keeps its `source.mp4`).

### Hostinger VPS (paid alternative)

> **Verified fact:** Python and FFmpeg are supported **exclusively on Hostinger VPS**. Shared / Web / Cloud hosting cannot run a persistent Python app — see "Where can this run?" above.

If you prefer hosting it yourself instead of Render: in Hostinger hPanel → **VPS** → create a **KVM VPS with Ubuntu 22.04 or 24.04**, at least **2 vCPU / 4 GB RAM**, then follow the generic "Deploy on a VPS" section above (Docker or systemd). For a public URL, add an **A record** in hPanel → **Domains → DNS** pointing a subdomain such as `clips.manansmm.com` at the VPS IP, put nginx in front, and get a free HTTPS certificate with `certbot --nginx -d clips.manansmm.com`. After a `git pull`, restart the service: `sudo systemctl restart manan-clipping-tool`.

---

## Platform limitations (honest version)

- **YouTube usually works for public videos**, but age-restricted, login-required, private, or bot-blocked videos can fail. If YouTube shows a bot check, the job fails with a clear message ("YouTube blocked this request (bot check)") — there is no workaround in the MVP; try a different video or try again later.
- **Facebook only sometimes works for public videos and breaks often.** Facebook changes its video delivery frequently, so a link that works today may fail tomorrow. Private videos, groups-only videos and anything needing login will not download.
- **Livestreams:** live capture while a stream is running is out of scope for the MVP. For livestreams, use the VOD link after the stream ends.
- **Only YouTube and Facebook links are accepted.** Any other host is rejected with `400`.
- Transcription is English-optimised in the highlight keywords, but Whisper itself transcribes many languages; highlight scoring still works, keyword hits are just rarer for non-English speech.
- If no Whisper package is installed, transcription is skipped: the job still becomes `ready` with the message "Transcription is unavailable on this install — you can still render clips with manual hook text and no captions." Rendering without a transcript produces a clip with hook + CTA only.

## Copyright note

Clipping other creators' content can draw Content ID claims or copyright strikes — even short clips, even with edits. Only clip content you own or have the rights to use. If you clip someone else's video, get permission first, and understand that a claim or strike is still possible.

---

## How to run the test render

Render a clip straight from a local file without the server, using `clipper.render` directly:

```bash
# from the project root, with the venv active and ffmpeg installed
python3 -c "
from clipper.render import render_clip
print(render_clip(
    'sample.mp4',          # any local video file
    10, 40,                # start / end in seconds (max 60 s)
    'WAIT FOR THE TWIST',  # hook text (top)
    'SUBSCRIBE FOR MORE',  # CTA text (bottom)
    'test-clip.mp4',       # output path
    segments=[{'start': 10.0, 'end': 14.0, 'text': 'Hello, this is a caption.'}],
))
"
```

The one-liner prints `{"path": ..., "duration": ..., "width": 1080, "height": 1920, "size": ...}` and writes `test-clip.mp4` (1080×1920, ≤ 60 s). Verify it with:

```bash
ffprobe -v error -select_streams v:0 -show_entries stream=width,height \
  -show_entries format=duration -of default=noprint_wrappers=1 test-clip.mp4
```

Expected: `width=1080`, `height=1920`, `duration` ≤ 60.5.

---

## Project layout

```
server.py                 FastAPI app: API + token auth + CORS + serves web/ as static files
config.py                 SOCIAL_LINKS — the one list of social links
clipper/
  __init__.py
  download.py             URL validation, yt-dlp metadata + download, Video Downloader (incl. TikTok hosts)
  transcribe.py           faster-whisper (fallback openai-whisper)
  highlights.py           loudness / keyword / density highlight scoring
  render.py               9:16 ffmpeg render with ASS captions, hook, CTA, two-pass loudnorm
requirements.txt
Dockerfile
render.yaml               Render Blueprint (Docker runtime, free tier)
.env.example
web/                      frontend (index.html, app.js, style.css, assets/)
jobs/                     per-job storage (created at runtime, git-ignored)
downloads/                Video Downloader temp files (created at runtime, git-ignored)
```
