"""Highlight suggestions: 45-second windows scored 0-100.

Signals (weights, renormalised when a signal is missing):
- loudness peaks 0.5  (per-second RMS from ffmpeg astats)
- excitement keywords 0.3 (English keyword hits in the transcript)
- speech density 0.2 (words per second)

Windows are 45 s long with a 5 s step. If the video is shorter than
30 s, the single candidate is the whole video.
"""

from __future__ import annotations

import re
import subprocess

WINDOW_SECONDS = 45.0
STEP_SECONDS = 5.0
MAX_HIGHLIGHTS = 8

EXCITEMENT_KEYWORDS = [
    "wow",
    "insane",
    "crazy",
    "oh my god",
    "oh my gosh",
    "unbelievable",
    "no way",
    "let's go",
    "lets go",
    "huge",
    "impossible",
    "what the",
    "look at this",
    "did you see",
    "amazing",
    "epic",
    "clutch",
    "he did it",
    "no he didn't",
    "no he didnt",
    "what just happened",
    "incredible",
    "no freaking way",
    "that was insane",
    "oh no",
    "wait what",
    "holy",
    "this is crazy",
    "unreal",
    "mind blown",
]

_RMS_PATTERNS = [
    re.compile(r"lavfi\.astats\.\w+\.RMS_level[=:]\s*(-?\d+(?:\.\d+)?|-inf)"),
    re.compile(r"RMS level dB:\s*(-?\d+(?:\.\d+)?|-inf)"),
    re.compile(r"RMS_level[=:]\s*(-?\d+(?:\.\d+)?|-inf)"),
]


def get_loudness_curve(path: str) -> list[float] | None:
    """Per-second RMS loudness (dB) via one ffmpeg astats call.

    Returns a list of RMS dB values, one per second of audio, or None if
    ffmpeg fails or no RMS values can be parsed (caller treats None as
    "no loudness signal" and renormalises the other weights).
    """
    try:
        result = subprocess.run(
            [
                "ffmpeg", "-v", "info",
                "-i", path,
                "-vn",
                "-af", "astats=metadata=1:reset=1",
                "-f", "null", "-",
            ],
            capture_output=True,
            text=True,
            timeout=1800,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None
    stderr = result.stderr or ""
    values: list[float] = []
    for line in stderr.splitlines():
        for pattern in _RMS_PATTERNS:
            match = pattern.search(line)
            if match:
                raw = match.group(1)
                if raw == "-inf":
                    values.append(-70.0)
                else:
                    try:
                        values.append(float(raw))
                    except ValueError:
                        pass
                break
    if not values:
        return None
    # astats emits one RMS value per audio frame; map the values evenly
    # onto seconds by averaging them into per-second buckets. We do not
    # know the frame rate here, so bucket proportionally by index using
    # the duration implied by the count is unreliable — instead, treat
    # the stream as roughly one value per frame at the source rate and
    # resample: if there are far more values than seconds, average chunks.
    # Estimate duration from the last "time=" progress line if present.
    duration = None
    time_match = re.findall(r"time=(\d+):(\d+):(\d+(?:\.\d+)?)", stderr)
    if time_match:
        h, m, s = time_match[-1]
        duration = int(h) * 3600 + int(m) * 60 + float(s)
    if duration and duration > 0:
        n_seconds = max(1, int(duration) + 1)
        per_second: list[float] = []
        chunk = len(values) / n_seconds
        for i in range(n_seconds):
            start = int(i * chunk)
            end = max(start + 1, int((i + 1) * chunk))
            bucket = values[start:end]
            per_second.append(sum(bucket) / len(bucket) if bucket else -70.0)
        return per_second
    # No duration parsed: average values into 1-second buckets assuming
    # a common 30 fps frame rate is wrong more often than it helps, so
    # return the raw values downsampled to at most one value per 30.
    if len(values) > 600:
        step = len(values) // 600 + 1
        return [sum(values[i:i + step]) / len(values[i:i + step])
                for i in range(0, len(values), step)]
    return values


def _fmt_time(seconds: float) -> str:
    total = int(seconds)
    minutes, secs = divmod(total, 60)
    return f"{minutes}:{secs:02d}"


def _normalise(values: list[float]) -> list[float]:
    """Min-max normalise a list to 0..1 (0.5 for every item if all equal)."""
    if not values:
        return []
    lo, hi = min(values), max(values)
    if hi - lo < 1e-9:
        return [0.5] * len(values)
    return [(v - lo) / (hi - lo) for v in values]


def _segments_in_window(transcript: list[dict], start: float, end: float) -> list[dict]:
    return [
        seg for seg in transcript
        if float(seg.get("end", 0)) > start and float(seg.get("start", 0)) < end
    ]


def suggest_highlights(
    transcript: list[dict],
    duration: float | None,
    loudness_curve: list[float] | None = None,
) -> list[dict]:
    """Return up to 8 highlight windows, best score first.

    Each item: {"start": float, "end": float, "score": int,
                "reasons": [str, ...]}.
    """
    if not duration or duration <= 0:
        if transcript:
            duration = max(float(seg.get("end", 0)) for seg in transcript)
        if not duration or duration <= 0:
            return []
    duration = float(duration)

    if duration < 30:
        windows = [(0.0, duration)]
    else:
        windows: list[tuple[float, float]] = []
        start = 0.0
        while start + WINDOW_SECONDS <= duration:
            windows.append((start, start + WINDOW_SECONDS))
            start += STEP_SECONDS
        if not windows:
            windows = [(0.0, duration)]
        elif windows[-1][1] < duration:
            # Cover a trailing partial stretch with a final window ending
            # at the video end, unless it duplicates the last window.
            tail = (max(0.0, duration - WINDOW_SECONDS), duration)
            if tail[0] - windows[-1][0] >= STEP_SECONDS:
                windows.append(tail)

    loudness_raw: list[float] = []
    keyword_raw: list[float] = []
    density_raw: list[float] = []
    loudest_second: list[float | None] = []
    for start, end in windows:
        # Loudness: mean RMS in the window (higher dB = louder).
        if loudness_curve:
            lo = max(0, int(start))
            hi = min(len(loudness_curve), max(lo + 1, int(end)))
            bucket = loudness_curve[lo:hi]
            loudness_raw.append(sum(bucket) / len(bucket) if bucket else -70.0)
            loudest_second.append(float(lo + bucket.index(max(bucket)))) if bucket else None
        else:
            loudness_raw.append(0.0)
            loudest_second.append(None)
        # Keywords + density from transcript segments overlapping the window.
        segs = _segments_in_window(transcript, start, end)
        text = " ".join(seg.get("text", "") for seg in segs).lower()
        keyword_raw.append(float(sum(text.count(kw) for kw in EXCITEMENT_KEYWORDS)))
        word_count = sum(len(seg.get("text", "").split()) for seg in segs)
        span = max(1.0, end - start)
        density_raw.append(word_count / span)

    has_loudness = bool(loudness_curve)
    has_transcript = bool(transcript)
    weights = {
        "loudness": 0.5 if has_loudness else 0.0,
        "keywords": 0.3 if has_transcript else 0.0,
        "density": 0.2 if has_transcript else 0.0,
    }
    total_weight = sum(weights.values())
    if total_weight <= 0:
        # No signals at all: return the first windows with a neutral score.
        return [
            {"start": round(s, 1), "end": round(e, 1), "score": 50,
             "reasons": ["Opening section of the video"]}
            for s, e in windows[:MAX_HIGHLIGHTS]
        ]
    for key in weights:
        weights[key] /= total_weight

    loud_norm = _normalise(loudness_raw) if has_loudness else [0.0] * len(windows)
    kw_norm = _normalise(keyword_raw) if has_transcript else [0.0] * len(windows)
    dens_norm = _normalise(density_raw) if has_transcript else [0.0] * len(windows)

    candidates: list[dict] = []
    median_density = sorted(density_raw)[len(density_raw) // 2] if density_raw else 0.0
    for i, (start, end) in enumerate(windows):
        score = (
            weights["loudness"] * loud_norm[i]
            + weights["keywords"] * kw_norm[i]
            + weights["density"] * dens_norm[i]
        ) * 100
        reasons: list[str] = []
        if has_loudness and loud_norm[i] >= 0.6 and loudest_second[i] is not None:
            reasons.append(f"Loud peak around {_fmt_time(loudest_second[i])}")
        if has_transcript and keyword_raw[i] >= 1:
            count = int(keyword_raw[i])
            reasons.append(
                f"{count} excitement keyword{'s' if count != 1 else ''}"
            )
        if has_transcript and density_raw[i] > 0 and density_raw[i] >= median_density and dens_norm[i] >= 0.6:
            reasons.append("High speech density")
        if not reasons:
            reasons.append("Strong overall moment in this section")
        candidates.append({
            "start": round(float(start), 1),
            "end": round(float(end), 1),
            "score": int(round(score)),
            "reasons": reasons,
        })
    candidates.sort(key=lambda c: c["score"], reverse=True)
    return candidates[:MAX_HIGHLIGHTS]
