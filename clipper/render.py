"""Render a vertical 9:16 clip with ffmpeg + ASS captions.

Output: 1080x1920, 30 fps, libx264 CRF 18, AAC 128k, loudnorm audio,
hook text at the top, CTA at the bottom, and (when available) transcript
captions above the CTA. Clip length is clamped to 60 seconds and the
result is validated with ffprobe before it is returned.

Layout is the middle-band style: the source video is scaled DOWN to at
most 1080 px wide with lanczos and NEVER upscaled, then centred on a
deep-purple (#390039) 1080x1920 background, so a 16:9 source plays in a
sharp middle band with the hook on top and the CTA at the bottom. A
source that is already vertical 1080x1920 fills the frame as before.
"""

from __future__ import annotations

import json
import os
import subprocess
import tempfile

MAX_CLIP_SECONDS = 60.0
TARGET_WIDTH = 1080
TARGET_HEIGHT = 1920
# Deep-purple background for the middle-band layout (same colour as the
# approved v4 Shorts background).
BAND_BG_COLOR = "0x390039"


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

    Backslashes, braces, commas and newlines are escaped; a literal
    percent sign is safe in ASS but is doubled defensively because some
    formatting paths treat % specially. The Text field is the last field
    of a Dialogue line, so commas would be safe unescaped, but escaping
    them as \\, keeps the line unambiguous for strict parsers.
    """
    text = text.replace("\\", "\\\\")
    text = text.replace("{", "\\{").replace("}", "\\}")
    text = text.replace(",", "\\,")
    text = text.replace("%", "%%")
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
) -> str:
    """Build ASS subtitle content for one clip.

    Captions sit at the bottom with a high MarginV, the CTA sits at the
    bottom with a lower MarginV (so they never overlap), and the hook
    is top-aligned for the whole clip.
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
        # White text, black outline, semi-transparent black box.
        f"Style: Hook,{font},64,&H00FFFFFF,&H000000FF,&H80000000,&H80000000,1,0,0,0,100,100,0,0,1,3,0,8,60,60,120,1",
        f"Style: Caption,{font},46,&H00FFFFFF,&H000000FF,&H80000000,&H80000000,1,0,0,0,100,100,0,0,1,3,0,2,60,60,170,1",
        f"Style: CTA,{font},50,&H00FFFFFF,&H000000FF,&H80000000,&H80000000,1,0,0,0,100,100,0,0,1,3,0,2,60,60,40,1",
        "",
        "[Events]",
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text",
    ]
    if hook_text:
        lines.append(
            f"Dialogue: 0,{_ass_time(0)},{_ass_time(clip_duration)},Hook,,0,0,0,,"
            f"{escape_ass_text(hook_text)}"
        )
    if segments:
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
) -> dict:
    """Render a 9:16 clip from source_path[start:end].

    Returns {"path": str, "duration": float, "width": int,
             "height": int, "size": int}.
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

    font = _detect_font()
    ass_content = _build_ass(hook_text or "", cta_text or "", segments, start, end, font)

    os.makedirs(os.path.dirname(os.path.abspath(output_path)), exist_ok=True)
    tmp_dir = tempfile.mkdtemp(prefix="clip-render-")
    ass_path = os.path.join(tmp_dir, "subtitles.ass")
    try:
        with open(ass_path, "w", encoding="utf-8") as handle:
            handle.write(ass_content)
        # Middle-band layout (never upscale): scale the video down to at
        # most 1080 px wide with lanczos (trunc to an even width for
        # yuv420p; height -2 keeps the aspect ratio), then centre it on
        # the deep-purple 1080x1920 canvas. A 16:9 1080p source becomes a
        # 1080x608 band at y 656-1264, leaving the hook (top, MarginV
        # 120) and the captions/CTA (bottom, MarginV 170/40) on the
        # purple bands with no overlap. A vertical 1080x1920 source
        # fills the frame exactly as before.
        video_filter = (
            "scale='trunc(min(1080,iw)/2)*2':-2:flags=lanczos,"
            f"pad=1080:1920:(ow-iw)/2:(oh-ih)/2:color={BAND_BG_COLOR},"
            "fps=30,"
            f"ass={ass_path}"
        )
        cmd = [
            "ffmpeg", "-y", "-v", "error",
            "-ss", str(start), "-to", str(end),
            "-i", source_path,
            "-vf", video_filter,
            "-af", _loudnorm_filter(source_path, start, end),
            "-ac", "2",
            # veryfast + CRF 18: better quality than superfast/CRF 19 on
            # busy game footage (finer quantisation, more motion search).
            # Measured 21.9 s for a 20 s clip including the two-pass
            # loudnorm measurement (the old superfast/CRF 19 build took
            # 23.4 s for the same window — the middle-band layout
            # encodes fewer pixels, which pays for the slower preset).
            "-c:v", "libx264", "-crf", "18", "-preset", "veryfast",
            "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-b:a", "128k",
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
        }
    finally:
        try:
            if os.path.exists(ass_path):
                os.remove(ass_path)
            os.rmdir(tmp_dir)
        except OSError:
            pass
