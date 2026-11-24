"""Manan Clipping Tool — backend package.

Modules:
- download: URL validation, yt-dlp metadata + download
- transcribe: speech-to-text via faster-whisper (fallback: openai-whisper)
- highlights: loudness / keyword / speech-density highlight scoring
- render: 9:16 vertical clip rendering with ffmpeg + ASS captions
"""

__version__ = "0.1.0"
