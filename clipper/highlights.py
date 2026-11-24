"""Highlight suggestions: scored windows over the video.

Signals (weights, renormalised when a signal is missing):
- loudness peaks 0.5  (per-second RMS from ffmpeg astats)
- excitement keywords 0.3 (English keyword hits in the transcript)
- speech density 0.2 (words per second)

Windows are `window_seconds` long (default 45 s) with a 5 s step. If the
video is shorter than the window, the single candidate is the whole video.

`generate_clips()` builds exactly N non-overlapping clips of a chosen
length from the same ranking: it takes the best moments first, then fills
any remainder from the most speech-dense parts of the transcript.
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
    window_seconds: float = WINDOW_SECONDS,
    max_highlights: int = MAX_HIGHLIGHTS,
) -> list[dict]:
    """Return up to `max_highlights` highlight windows, best score first.

    Each item: {"start": float, "end": float, "score": int,
                "reasons": [str, ...]}. Windows are `window_seconds` long
    (default 45 s); a shorter video yields the single whole-video window.
    """
    if not duration or duration <= 0:
        if transcript:
            duration = max(float(seg.get("end", 0)) for seg in transcript)
        if not duration or duration <= 0:
            return []
    duration = float(duration)
    window_seconds = float(window_seconds)

    if duration < window_seconds:
        windows = [(0.0, duration)]
    else:
        windows: list[tuple[float, float]] = []
        start = 0.0
        while start + window_seconds <= duration:
            windows.append((start, start + window_seconds))
            start += STEP_SECONDS
        if not windows:
            windows = [(0.0, duration)]
        elif windows[-1][1] < duration:
            # Cover a trailing partial stretch with a final window ending
            # at the video end, unless it duplicates the last window.
            tail = (max(0.0, duration - window_seconds), duration)
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
            for s, e in windows[:max_highlights]
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
    return candidates[:max_highlights]


def _overlaps(start: float, end: float, picked: list[dict]) -> bool:
    """True if [start, end) overlaps any window already picked."""
    return any(not (end <= p["start"] + 0.01 or start >= p["end"] - 0.01)
               for p in picked)


def _fill_from_density(
    transcript: list[dict],
    duration: float,
    clip_seconds: float,
    picked: list[dict],
    count: int,
) -> list[dict]:
    """Fill remaining slots with the most speech-dense windows.

    Slides a `clip_seconds` window across the video in half-length
    steps, scores each by words spoken inside it, and takes the best
    ones that do not overlap `picked`. With no transcript every window
    scores 0, so the earliest fitting windows are taken instead.
    """
    step = max(1.0, clip_seconds / 2)
    starts: list[float] = []
    t = 0.0
    while t + clip_seconds <= duration + 1e-9:
        starts.append(t)
        t += step
    if not starts and duration > 0:
        starts = [0.0]
    scored: list[tuple[int, float, float]] = []
    for s in starts:
        e = min(s + clip_seconds, duration)
        segs = _segments_in_window(transcript, s, e)
        words = sum(len(seg.get("text", "").split()) for seg in segs)
        scored.append((words, s, e))
    scored.sort(key=lambda item: (-item[0], item[1]))
    for _words, s, e in scored:
        if len(picked) >= count:
            break
        if _overlaps(s, e, picked):
            continue
        picked.append({
            "start": round(float(s), 1),
            "end": round(float(e), 1),
            "score": 50,
            "reasons": ["Speech-dense section"] if transcript else ["Section of the video"],
        })
    picked.sort(key=lambda c: c["start"])
    return picked


def generate_clips(
    transcript: list[dict],
    duration: float | None,
    loudness_curve: list[float] | None = None,
    count: int = 3,
    clip_seconds: float = 30.0,
) -> tuple[list[dict], str | None]:
    """Generate up to `count` non-overlapping clips of `clip_seconds` each.

    Takes the best moments first (the same loudness / keyword / density
    ranking as `suggest_highlights`, with windows of exactly
    `clip_seconds`), then fills any remainder from the most speech-dense
    parts of the transcript. Every returned clip is exactly
    `clip_seconds` long — except when the whole video is shorter than
    `clip_seconds`, in which case the single clip is the whole video.

    Returns (clips, note): clips are {"start", "end", "score",
    "reasons"} dicts in timeline order; note is None when exactly
    `count` clips were produced, otherwise a short explanation (e.g.
    the video is too short to fit that many clips of that length).
    """
    if not duration or duration <= 0:
        if transcript:
            duration = max(float(seg.get("end", 0)) for seg in transcript)
        if not duration or duration <= 0:
            return [], "Could not determine the video length."
    duration = float(duration)
    clip_seconds = float(clip_seconds)
    count = max(1, int(count))

    if clip_seconds > duration:
        # The whole video is shorter than one requested clip.
        return (
            [{"start": 0.0, "end": round(duration, 1), "score": 50,
              "reasons": ["Whole video"]}],
            f"Video is shorter than {clip_seconds:g} s, so this is the whole video as one clip.",
        )

    # Best moments first: rank windows of exactly clip_seconds and take
    # the top ones that do not overlap each other.
    candidates = suggest_highlights(
        transcript, duration, loudness_curve,
        window_seconds=clip_seconds, max_highlights=500,
    )
    picked: list[dict] = []
    for cand in candidates:
        s, e = float(cand["start"]), float(cand["end"])
        if e - s < clip_seconds - 0.5:
            # Short tail / whole-video window — not a full-length clip.
            continue
        if _overlaps(s, e, picked):
            continue
        picked.append(cand)
        if len(picked) >= count:
            break

    if len(picked) < count:
        picked = _fill_from_density(transcript, duration, clip_seconds, picked, count)

    picked.sort(key=lambda c: c["start"])
    if len(picked) == count:
        return picked, None
    if picked:
        return picked, (
            f"Only {len(picked)} non-overlapping clip(s) of {clip_seconds:g} s "
            f"fit in this video (it is {duration:.0f} s long)."
        )
    return picked, f"No {clip_seconds:g} s clip fits in this video."
