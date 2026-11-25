FROM python:3.12-slim

LABEL org.opencontainers.image.title="Manan Clipping Tool"
LABEL org.opencontainers.image.description="Manan Clipping Tool — turn long YouTube and Facebook videos into 9:16 vertical clips with captions, hooks and CTAs"
LABEL org.opencontainers.image.version="0.2.2"

# ffmpeg (and ffprobe) are required at runtime; fonts for ASS captions.
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg fonts-dejavu-core fontconfig \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
# faster-whisper pulls in ctranslate2 as a dependency.
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Jobs (downloads / transcripts / clips) live under JOB_DIR inside the
# container by default. Mount a volume at /app/jobs to persist them.
ENV JOB_DIR=/app/jobs
ENV WHISPER_MODEL=tiny
ENV MAX_DURATION=14400
ENV MAX_FILE_MB=2048

EXPOSE 8000

CMD ["sh", "-c", "uvicorn server:app --host 0.0.0.0 --port ${PORT:-8000}"]
