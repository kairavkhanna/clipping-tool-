FROM python:3.12-slim

# ffmpeg cuts the clips. Deno (installed by pip from requirements.txt) lets yt-dlp read YouTube.
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt requirements-server.txt ./
RUN pip install --no-cache-dir -r requirements-server.txt

COPY *.py ./
COPY static static
COPY fonts fonts

ENV HOST=0.0.0.0 \
    PORT=8765 \
    CLIPPER_DATA=/data
VOLUME /data
EXPOSE 8765

# One worker on purpose: job progress is kept in memory. Threads handle many users.
CMD ["gunicorn", "--workers", "1", "--threads", "16", "--timeout", "0", "--bind", "0.0.0.0:8765", "app:app"]
