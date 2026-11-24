"""Speech-to-text via faster-whisper, falling back to openai-whisper.

Audio is first extracted to 16 kHz mono WAV with ffmpeg, which is what
Whisper expects. If neither Whisper package is installed, a specific
TranscriptionUnavailable exception is raised so the caller can keep the
job usable (manual rendering, no captions) instead of failing it.
"""

from __future__ import annotations

import os
import subprocess
import tempfile


class TranscriptionUnavailable(RuntimeError):
    """Raised when no Whisper implementation is installed."""


_NO_WHISPER_MESSAGE = (
    "Transcription is unavailable on this install — no Whisper "
    "package (faster-whisper or openai-whisper) is installed."
)


def _extract_audio_16k(source_path: str, wav_path: str) -> None:
    """Extract 16 kHz mono WAV audio from a video/audio file via ffmpeg."""
    result = subprocess.run(
        [
            "ffmpeg", "-y", "-v", "error",
            "-i", source_path,
            "-vn", "-ac", "1", "-ar", "16000",
            "-c:a", "pcm_s16le",
            wav_path,
        ],
        capture_output=True,
        text=True,
        timeout=1800,
    )
    if result.returncode != 0 or not os.path.exists(wav_path):
        raise RuntimeError(
            "Could not extract audio for transcription: "
            + (result.stderr.strip().splitlines()[-1] if result.stderr.strip() else "ffmpeg failed.")
        )


def _transcribe_faster_whisper(wav_path: str, model: str) -> list[dict]:
    from faster_whisper import WhisperModel

    whisper_model = WhisperModel(model, device="cpu", compute_type="int8")
    segments_iter, _info = whisper_model.transcribe(wav_path, word_timestamps=True)
    segments: list[dict] = []
    for seg in segments_iter:
        item: dict = {
            "start": float(seg.start),
            "end": float(seg.end),
            "text": seg.text.strip(),
        }
        words = getattr(seg, "words", None)
        if words:
            item["words"] = [
                {"start": float(w.start), "end": float(w.end), "word": w.word}
                for w in words
            ]
        segments.append(item)
    return segments


def _transcribe_openai_whisper(wav_path: str, model: str) -> list[dict]:
    import whisper  # openai-whisper

    whisper_model = whisper.load_model(model, device="cpu")
    result = whisper_model.transcribe(wav_path, word_timestamps=True)
    segments: list[dict] = []
    for seg in result.get("segments", []):
        item: dict = {
            "start": float(seg["start"]),
            "end": float(seg["end"]),
            "text": seg["text"].strip(),
        }
        words = seg.get("words")
        if words:
            item["words"] = [
                {"start": float(w["start"]), "end": float(w["end"]), "word": w["word"]}
                for w in words
            ]
        segments.append(item)
    return segments


def transcribe(source_path: str, model: str = "tiny") -> list[dict]:
    """Transcribe a video/audio file.

    Returns a list of {"start": float, "end": float, "text": str} segments,
    with a "words" list (word-level timing) when the engine provides it.
    Raises TranscriptionUnavailable if no Whisper package is installed.
    """
    if not os.path.exists(source_path):
        raise FileNotFoundError(f"Source file not found: {source_path}")
    tmp_dir = tempfile.mkdtemp(prefix="clip-transcribe-")
    wav_path = os.path.join(tmp_dir, "audio.wav")
    try:
        _extract_audio_16k(source_path, wav_path)
        try:
            return _transcribe_faster_whisper(wav_path, model)
        except ImportError:
            # faster-whisper (or a dependency of it) is not installed;
            # fall through to openai-whisper below.
            pass
        except Exception:
            # faster-whisper is installed but failed — e.g. the av 19
            # incompatibility (faster-whisper 1.2.1 passes the
            # `metadata_errors` kwarg that av 19 removed), or a runtime
            # failure on this file/model. Route through the same
            # fallback logic as a missing faster-whisper instead of
            # failing the job: if openai-whisper is there, use it; if
            # it is absent too, the job degrades gracefully (ready,
            # no captions) via TranscriptionUnavailable.
            try:
                return _transcribe_openai_whisper(wav_path, model)
            except ImportError:
                raise TranscriptionUnavailable(_NO_WHISPER_MESSAGE)
            # openai-whisper is installed but failed on this file:
            # that is a real failure, so surface it as-is.
            except Exception:
                raise
        try:
            return _transcribe_openai_whisper(wav_path, model)
        except ImportError:
            raise TranscriptionUnavailable(_NO_WHISPER_MESSAGE)
    finally:
        # Delete the temporary 16 kHz WAV (free-tier disk hygiene).
        try:
            if os.path.exists(wav_path):
                os.remove(wav_path)
            os.rmdir(tmp_dir)
        except OSError:
            pass
