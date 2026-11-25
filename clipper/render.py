"""Render a vertical 9:16 clip with ffmpeg + ASS captions.

Output: 1080x1920, 30 fps, libx264 CRF 18, AAC 128k, loudnorm audio,
hook text at the top, CTA at the bottom, and (when available) transcript
captions above the CTA. Clip length is clamped to 60 seconds and the
result is validated with ffprobe before it is returned.

Layout is full-width: the source video is scaled to the full 1080 px
width with lanczos (upscaled with a light unsharp when the source is
narrower than 1080, so small sources still fill the frame crisply) and
centred vertically on a 1080x1920 background, so a 16:9 source plays in
a 1080x608 band, a 4:5 source in a 1080x1350 band, and a vertical
1080x1920 source fills the frame. The old centre-crop upscale
(scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920)
is never used — it stretched a 607x1080 centre slice ~1.78x and caused
the pixelation the user reported.

The background and the text colour rotate per clip (THEMES): every clip
in a job gets a different pairing, so consecutive clips never look the
same. Hook and CTA text are big and bold, like the approved v4 demo
frame («WAIT FOR END TWIST» / «SUBSCRIBE FOR MORE»).
"""

from __future__ import annotations

import json
import os
import subprocess
import tempfile

MAX_CLIP_SECONDS = 60.0
TARGET_WIDTH = 1080
TARGET_HEIGHT = 1920

# Background + text-colour pairings. Every theme is dark enough that
# light text reads on it, and the ASS outline (black) protects the text
# further. The server rotates through these by clip index within a job,
# so consecutive clips always differ. "background" is the display string
# shown on the clip card; "bg" (solids) / "c0"+"c1" (gradients) are the
# ffmpeg colour values (RRGGBB, no #).
THEMES = [
    {"name": "Deep Purple", "kind": "solid", "bg": "390039",
     "background": "#390039", "text": "FFFFFF"},
    {"name": "Midnight Gold", "kind": "solid", "bg": "0B1E3A",
     "background": "#0B1E3A", "text": "FFD166"},
    {"name": "Dark Teal", "kind": "solid", "bg": "062E2B",
     "background": "#062E2B", "text": "7FE7FF"},
    {"name": "Crimson Night", "kind": "solid", "bg": "3A0B14",
     "background": "#3A0B14", "text": "FFFFFF"},
    {"name": "Forest Lime", "kind": "solid", "bg": "0E2A12",
     "background": "#0E2A12", "text": "B6FF5C"},
    {"name": "Sunset Glow", "kind": "gradient", "c0": "2B0A3D", "c1": "7A2E0D",
     "background": "#2B0A3D → #7A2E0D", "text": "FFE8B0"},
]


def theme_for_index(index: int) -> dict:
    """Theme for a clip index: deterministic rotation through THEMES."""
    return THEMES[int(index) % len(THEMES)]


def _ass_color(rgb_hex: str) -> str:
    """Convert RRGGBB to an ASS colour (&HBBGGRR, fully opaque)."""
    rgb_hex = (rgb_hex or "FFFFFF").strip().lstrip("#")
    if len(rgb_hex) != 6:
        rgb_hex = "FFFFFF"
    return f"&H00{rgb_hex[4:6]}{rgb_hex[2:4]}{rgb_hex[0:2]}"


def _ass_time(seconds: float) -> str:
    """Format seconds as ASS time H:MM:SS.cc (centiseconds)."""
    seconds = max(0.0, float(seconds))
    hours = int(seconds // 3600)
    minutes = int((seconds % 3600) // 60)
    secs = seconds % 60
    whole = int(secs)
    centis = int(round((secs - whole) * 100))
    if centis == 100:
        whole += 1
        centis = 0
    return f"{hours}:{minutes:02d}:{whole:02d}.{centis:02d}"


def escape_ass_text(text: str) -> str:
    """Escape text for an ASS Dialogue line.

    Backslashes, braces and newlines are escaped (braces would open
    override tags, newlines become \\N). Commas and percent signs are
    left alone: the Text field is the last field of a Dialogue line,
    so commas need no escaping, and ASS has no %-escapes — escaping
    them as \\, / %% used to render a literal backslash / double
    percent on screen (every captioned comma shipped that way).
    """
    text = text.replace("\\", "\\\\")
    text = text.replace("{", "\\{").replace("}", "\\}")
    text = text.replace("\r\n", "\\N").replace("\n", "\\N").replace("\r", "\\N")
    return text


def _detect_font() -> str:
    """Pick a font for captions: DejaVu Sans via fc-match, else default."""
    try:
        result = subprocess.run(
            ["fc-match", "DejaVu Sans", "--format", "%{family}"],
            capture_output=True, text=True, timeout=10,
        )
        family = (result.stdout or "").strip().split(",")[0].strip()
        if result.returncode == 0 and family:
            return family
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        pass
    return "DejaVu Sans"


def _build_ass(
    hook_text: str,
    cta_text: str,
    segments: list[dict] | None,
    start: float,
    end: float,
    font: str,
    text_color: str = "&H00FFFFFF",
    captions: bool = True,
    hook_fontsize: int = 120,
    hook_margin_v: int = 100,
) -> str:
    """Build ASS subtitle content for one clip.

    The hook is big and bold at the top (120 px, like the approved v4
    demo frame; smaller — 84 px — when the video band is tall and the
    top band is short, so the hook never spills onto the video), the
    CTA is big at the bottom (96 px), and the spoken-word captions sit
    above the CTA (48 px). When captions is False the transcript
    segments are left out entirely — the hook and CTA still render.
    Margins keep the three text zones apart: hook MarginV 100 (50 when
    small), captions MarginV 270, CTA MarginV 30.
    """
    clip_duration = end - start
    lines = [
        "[Script Info]",
        "ScriptType: v4.00+",
        "PlayResX: 1080",
        "PlayResY: 1920",
        "ScaledBorderAndShadow: yes",
        "",
        "[V4+ Styles]",
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, "
        "OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, "
        "ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, "
        "Alignment, MarginL, MarginR, MarginV, Encoding",
        # Big bold text in the theme colour, black outline, semi-transparent
        # black box. Hook 120 px / CTA 96 px match the v4 demo frame.
        f"Style: Hook,{font},{hook_fontsize},{text_color},&H000000FF,&H80000000,&H80000000,1,0,0,0,100,100,0,0,1,5,0,8,60,60,{hook_margin_v},1",
        f"Style: Caption,{font},48,{text_color},&H000000FF,&H80000000,&H80000000,1,0,0,0,100,100,0,0,1,3,0,2,60,60,270,1",
        f"Style: CTA,{font},96,{text_color},&H000000FF,&H80000000,&H80000000,1,0,0,0,100,100,0,0,1,4,0,2,60,60,30,1",
        "",
        "[Events]",
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text",
    ]
    if hook_text:
        lines.append(
            f"Dialogue: 0,{_ass_time(0)},{_ass_time(clip_duration)},Hook,,0,0,0,,"
            f"{escape_ass_text(hook_text)}"
        )
    if captions and segments:
        for seg in segments:
            seg_start = float(seg.get("start", 0)) - start
            seg_end = float(seg.get("end", 0)) - start
            text = (seg.get("text") or "").strip()
            if not text:
                continue
            seg_start = max(0.0, seg_start)
            seg_end = min(clip_duration, seg_end)
            if seg_end <= seg_start:
                continue
            lines.append(
                f"Dialogue: 0,{_ass_time(seg_start)},{_ass_time(seg_end)},Caption,,0,0,0,,"
                f"{escape_ass_text(text)}"
            )
    if cta_text:
        lines.append(
            f"Dialogue: 0,{_ass_time(0)},{_ass_time(clip_duration)},CTA,,0,0,0,,"
            f"{escape_ass_text(cta_text)}"
        )
    return "\n".join(lines) + "\n"


def _ffprobe_info(path: str) -> dict:
    result = subprocess.run(
        [
            "ffprobe", "-v", "error",
            "-select_streams", "v:0",
            "-show_entries", "stream=width,height",
            "-show_entries", "format=duration",
            "-of", "json",
            path,
        ],
        capture_output=True, text=True, timeout=60,
    )
    if result.returncode != 0:
        raise RuntimeError(f"ffprobe failed on rendered clip: {result.stderr.strip()}")
    return json.loads(result.stdout)


def _probe_source(path: str) -> tuple[int | None, int | None, bool]:
    """Probe a source file: (width, height, has_audio).

    The width decides whether the render upscales (with unsharp) or
    downscales; has_audio decides whether the render maps an audio
    stream at all, so silent sources render instead of failing on
    `-map 1:a`. Returns (None, None, False) when probing fails.
    """
    result = subprocess.run(
        [
            "ffprobe", "-v", "error",
            "-show_entries", "stream=codec_type,width,height",
            "-of", "json",
            path,
        ],
        capture_output=True, text=True, timeout=60,
    )
    if result.returncode != 0:
        return None, None, False
    try:
        streams = json.loads(result.stdout).get("streams", [])
    except json.JSONDecodeError:
        return None, None, False
    width = height = None
    has_audio = False
    for stream in streams:
        if stream.get("codec_type") == "video" and width is None:
            width = stream.get("width")
            height = stream.get("height")
        if stream.get("codec_type") == "audio":
            has_audio = True
    return width, height, has_audio


def _background_lavfi(theme: dict) -> str:
    """Lavfi source string for the theme's 1080x1920 background.

    The background is fed to ffmpeg as a LIVE lavfi source, not as a
    looped PNG file: `-loop 1` on a PNG re-decodes the whole 1080x1920
    image on every frame (~21 s of the render on this machine — the
    single biggest cost in the pipeline), while the lavfi source
    generates each frame for ~nothing (measured: the whole filter
    chain drops from ~32 s to ~5 s on the 20 s test clip). Solids use
    the `color` source; the gradient theme uses `gradients` (vertical,
    top colour to bottom colour). No external assets.
    """
    if theme.get("kind") == "gradient":
        # The gradient is purely vertical, so it is generated as an
        # 8 px wide strip and stretched to full width in the
        # filtergraph: generating the gradient at full 1080x1920 costs
        # ~11 s per 20 s clip (per-pixel interpolation on every frame),
        # while the 8 px strip + bilinear scale costs ~3 s — and the
        # result is pixel-identical (the strip is constant across x).
        return (
            "gradients=s=8x1920"
            f":c0=0x{theme['c0']}:c1=0x{theme['c1']}"
            ":x0=4:y0=0:x1=4:y1=1920:r=30"
        )
    return f"color=c=0x{theme['bg']}:s=1080x1920:r=30"


def _measure_loudness(source_path: str, start: float, end: float) -> dict | None:
    """Pass 1 of two-pass loudnorm: measure the clip's loudness.

    Runs ffmpeg with loudnorm print_format=json over the clip window
    and parses the measured values (input_i / input_tp / input_lra /
    input_thresh / target_offset) out of stderr. Returns a dict with
    the pass-2 parameter names (measured_I, measured_TP, measured_LRA,
    measured_thresh, offset), or None if measurement fails for any
    reason — the caller then falls back to single-pass loudnorm.
    Note: no `-v error` here — loudnorm prints its JSON summary to
    stderr at info log level, so quieting ffmpeg would hide it.
    """
    cmd = [
        "ffmpeg", "-y",
        "-ss", str(start), "-to", str(end),
        "-i", source_path,
        "-vn",
        "-af", "loudnorm=I=-15:TP=-1.5:LRA=11:print_format=json",
        "-f", "null", "-",
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
    except subprocess.TimeoutExpired:
        return None
    text = result.stderr or ""
    start_idx = text.find("{")
    end_idx = text.rfind("}")
    if start_idx == -1 or end_idx == -1 or end_idx <= start_idx:
        return None
    try:
        data = json.loads(text[start_idx:end_idx + 1])
        return {
            "measured_I": float(data["input_i"]),
            "measured_TP": float(data["input_tp"]),
            "measured_LRA": float(data["input_lra"]),
            "measured_thresh": float(data["input_thresh"]),
            "offset": float(data["target_offset"]),
        }
    except (json.JSONDecodeError, KeyError, TypeError, ValueError):
        return None


def _loudnorm_filter(source_path: str, start: float, end: float) -> str:
    """Audio filter for the render: two-pass loudnorm.

    A single loudnorm pass undershoots the -15 LUFS target on this
    material (measured -17.47 LUFS), so measure first and apply the
    measured values with linear=true; if measurement fails, fall back
    to the single-pass filter rather than failing the render.
    """
    measured = _measure_loudness(source_path, start, end)
    if measured is not None:
        return (
            "loudnorm=I=-15:TP=-1.5:LRA=11"
            f":measured_I={measured['measured_I']}"
            f":measured_TP={measured['measured_TP']}"
            f":measured_LRA={measured['measured_LRA']}"
            f":measured_thresh={measured['measured_thresh']}"
            f":offset={measured['offset']}"
            ":linear=true,aresample=48000"
        )
    return "loudnorm=I=-15:TP=-1.5:LRA=11,aresample=48000"


def render_clip(
    source_path: str,
    start: float,
    end: float,
    hook_text: str,
    cta_text: str,
    output_path: str,
    segments: list[dict] | None = None,
    captions: bool = True,
    theme_index: int = 0,
) -> dict:
    """Render a 9:16 clip from source_path[start:end].

    Returns {"path": str, "duration": float, "width": int,
             "height": int, "size": int, "theme": str, "background": str}.
    Raises ValueError for bad windows and RuntimeError on ffmpeg/ffprobe
    failure or on a validation mismatch.
    """
    if not os.path.exists(source_path):
        raise FileNotFoundError(f"Source file not found: {source_path}")
    start = float(start)
    end = float(end)
    if start < 0:
        raise ValueError("Clip start must be 0 or later.")
    if end <= start:
        raise ValueError("Clip end must be after clip start.")
    if end - start > MAX_CLIP_SECONDS:
        end = start + MAX_CLIP_SECONDS
    clip_duration = end - start

    theme = theme_for_index(theme_index)
    font = _detect_font()
    # Hook sizing adapts to the video band: a 16:9 source leaves a
    # 656 px top band, so the hook is 120 px like the v4 demo frame; a
    # tall band (4:5: 285 px, 9:16: none) leaves little room above the
    # video, so the hook drops to 84 px and hugs the top instead of
    # spilling onto the video. The band height follows the source
    # aspect at full 1080 px width (height rounded even, as scale does).
    src_width, src_height, has_audio = _probe_source(source_path)
    hook_fontsize, hook_margin_v = 120, 100
    if src_width and src_height:
        video_h = int(round(1080 * src_height / src_width / 2.0) * 2)
        if (TARGET_HEIGHT - video_h) // 2 < 360:
            hook_fontsize, hook_margin_v = 84, 50
    text_color = _ass_color(theme["text"])
    ass_content = _build_ass(
        hook_text or "", cta_text or "", segments, start, end, font,
        text_color=text_color, captions=captions,
        hook_fontsize=hook_fontsize, hook_margin_v=hook_margin_v,
    )

    os.makedirs(os.path.dirname(os.path.abspath(output_path)), exist_ok=True)
    tmp_dir = tempfile.mkdtemp(prefix="clip-render-")
    ass_path = os.path.join(tmp_dir, "subtitles.ass")
    try:
        with open(ass_path, "w", encoding="utf-8") as handle:
            handle.write(ass_content)
        # Full-width layout: scale the video to exactly 1080 px wide
        # with bicubic (height follows the aspect, even-rounded). A
        # 16:9 source becomes a 1080x608 band centred vertically
        # (y 656-1264), a 4:5 source a 1080x1350 band (y 285-1635), and
        # a vertical 1080x1920 source fills the frame. When the source
        # is narrower than 1080 it is scaled UP with a light unsharp so
        # small sources (e.g. 640x360) still fill the frame crisply —
        # never the old centre-crop upscale, which stretched a
        # 607x1080 centre slice and caused the pixelation.
        scale_filter = "scale=1080:-2:flags=bicubic"
        if src_width is not None and src_width < TARGET_WIDTH:
            scale_filter += ",unsharp=5:5:0.6:5:5:0.0"
        # The bg is already 30 fps and the fg is forced to 30 fps, so
        # the chain stays 30 fps end to end — no second fps filter
        # after the ass (the old one was redundant).
        if theme.get("kind") == "gradient":
            # Gradient themes are generated as an 8 px strip (see
            # _background_lavfi) and stretched to full width here.
            video_filter = (
                "[0:v]scale=1080:1920:flags=bilinear[bg];"
                f"[1:v]{scale_filter},fps=30[fg];"
                f"[bg][fg]overlay=(W-w)/2:(H-h)/2,ass={ass_path}[v]"
            )
        else:
            video_filter = (
                f"[1:v]{scale_filter},fps=30[fg];"
                f"[0:v][fg]overlay=(W-w)/2:(H-h)/2,ass={ass_path}[v]"
            )
        # Input 0 is the background as a LIVE lavfi source (see
        # _background_lavfi): a looped PNG file would be re-decoded
        # on every frame and cost ~21 s per 20 s clip. The `-t`
        # BEFORE `-i` caps the endless lavfi source at the clip
        # length; without it ffmpeg would run forever.
        cmd = [
            "ffmpeg", "-y", "-v", "error",
            "-t", str(clip_duration),
            "-f", "lavfi", "-i", _background_lavfi(theme),
            "-ss", str(start), "-to", str(end),
            "-i", source_path,
            "-filter_complex", video_filter,
            "-map", "[v]",
        ]
        if has_audio:
            cmd += [
                "-map", "1:a",
                "-af", _loudnorm_filter(source_path, start, end),
                "-ac", "2", "-c:a", "aac", "-b:a", "128k",
            ]
        cmd += [
            # veryfast + CRF 18: measured fastest-for-size on this
            # pipeline — superfast/ultrafast were no quicker (the
            # filter chain, not the encoder, was the bottleneck) and
            # produced 2-3x larger files at the same CRF.
            "-c:v", "libx264", "-crf", "18", "-preset", "veryfast",
            "-pix_fmt", "yuv420p",
            "-movflags", "+faststart",
            output_path,
        ]
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
        except subprocess.TimeoutExpired:
            raise RuntimeError("Clip rendering timed out (over 10 minutes).")
        if result.returncode != 0:
            detail = (result.stderr or "").strip().splitlines()
            raise RuntimeError(
                "ffmpeg failed to render the clip: "
                + (detail[-1] if detail else "unknown ffmpeg error")
            )
        if not os.path.exists(output_path):
            raise RuntimeError("ffmpeg finished but no clip file was produced.")
        info = _ffprobe_info(output_path)
        streams = info.get("streams", [])
        width = streams[0].get("width") if streams else None
        height = streams[0].get("height") if streams else None
        duration = float(info.get("format", {}).get("duration") or 0)
        if width != TARGET_WIDTH or height != TARGET_HEIGHT:
            raise RuntimeError(
                f"Rendered clip is {width}x{height}, expected "
                f"{TARGET_WIDTH}x{TARGET_HEIGHT}."
            )
        if duration > MAX_CLIP_SECONDS + 0.5:
            raise RuntimeError(
                f"Rendered clip is {duration:.1f}s long, over the 60s limit."
            )
        return {
            "path": output_path,
            "duration": duration if duration else clip_duration,
            "width": int(width),
            "height": int(height),
            "size": os.path.getsize(output_path),
            "theme": theme["name"],
            "background": theme["background"],
        }
    finally:
        try:
            if os.path.exists(ass_path):
                os.remove(ass_path)
        except OSError:
            pass
        try:
            os.rmdir(tmp_dir)
        except OSError:
            pass
