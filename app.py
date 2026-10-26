#!/usr/bin/env python3
"""Clipper: a local video clipping tool.

Run locally:   python app.py   (then open http://127.0.0.1:8765)
Run on a server: see DEPLOY.md. It uses Docker, HTTPS and a password.

Add videos by uploading a file or pasting a YouTube link, pick a start and an
end on the timeline, and export the clip. Everything stays on your computer in
the ./workspace folder.
"""
import hmac
import importlib.util
import json
import os
import queue
import random
import re
import shutil
import subprocess
import threading
import time
import uuid
import webbrowser
import zipfile
from collections import Counter
from pathlib import Path
from urllib.parse import urlparse

import ai
import captions
import speech
import youtube as yt_api
from flask import Flask, Response, abort, jsonify, request, send_file, send_from_directory

# Settings come from environment variables so the same code runs on your PC and on a server.
HOST = os.environ.get("HOST", "127.0.0.1")
PORT = int(os.environ.get("PORT", "8765"))
IS_LOCAL = HOST in {"127.0.0.1", "localhost", "::1"}
LOGIN_USER = os.environ.get("CLIPPER_USER", "clipper")
LOGIN_PASSWORD = os.environ.get("CLIPPER_PASSWORD", "")
ALLOWED_HOSTS = {h.strip().lower() for h in os.environ.get("ALLOWED_HOSTS", "").split(",") if h.strip()}
RETENTION_HOURS = float(os.environ.get("RETENTION_HOURS", "0"))    # 0 = keep files forever
MAX_UPLOAD_MB = int(os.environ.get("MAX_UPLOAD_MB", "4096"))
MAX_CONCURRENT_JOBS = max(1, int(os.environ.get("MAX_CONCURRENT_JOBS", "2")))
MIN_FREE_GB = float(os.environ.get("MIN_FREE_GB", "1"))
YT_COOKIES = os.environ.get("YT_COOKIES", "")                        # optional cookies.txt for YouTube

if not IS_LOCAL and not LOGIN_PASSWORD:
    raise SystemExit("Refusing to start on a public address without CLIPPER_PASSWORD. "
                     "Set CLIPPER_PASSWORD (see DEPLOY.md).")

BASE = Path(__file__).parent.resolve()
DATA = Path(os.environ.get("CLIPPER_DATA", BASE / "workspace")).resolve()
SOURCES = DATA / "sources"
CLIPS = DATA / "clips"
LIBRARY = DATA / "library.json"
TRANSCRIPTS = DATA / "transcripts"
TMP = DATA / "tmp"
SETTINGS = DATA / "settings.json"
FONTS = BASE / "fonts"
BATCHES = DATA / "batches"
BATCH_PAUSE = float(os.environ.get("BATCH_PAUSE_SECONDS", "2"))     # gentle on the free voice service
BATCH_MAX_ITEMS = int(os.environ.get("BATCH_MAX_ITEMS", "300"))
for folder in (SOURCES, CLIPS, TRANSCRIPTS, TMP, BATCHES):
    folder.mkdir(parents=True, exist_ok=True)
shutil.rmtree(TMP, ignore_errors=True)   # leftovers from a crash
TMP.mkdir(parents=True, exist_ok=True)
speech.configure(DATA / "models")

app = Flask(__name__, static_folder=None)
app.config["MAX_CONTENT_LENGTH"] = MAX_UPLOAD_MB * 1024 * 1024

lib_lock = threading.RLock()
jobs = {}
jobs_lock = threading.Lock()
job_slots = threading.BoundedSemaphore(MAX_CONCURRENT_JOBS)

YOUTUBE_HOSTS = {"youtube.com", "www.youtube.com", "m.youtube.com", "music.youtube.com", "youtu.be"}
AUDIO_EXTS = {".mp3", ".m4a", ".wav", ".ogg", ".aac", ".flac"}
QUALITY_FORMATS = {
    "480": "bv*[vcodec^=avc1][height<=480]+ba[ext=m4a]/bv*[height<=480]+ba/b[height<=480]/b",
    "720": "bv*[vcodec^=avc1][height<=720]+ba[ext=m4a]/bv*[height<=720]+ba/b[height<=720]/b",
    "1080": "bv*[vcodec^=avc1][height<=1080]+ba[ext=m4a]/bv*[height<=1080]+ba/b[height<=1080]/b",
    "best": "bv*[vcodec^=avc1]+ba[ext=m4a]/bv*+ba/b",
}


# ---------------------------------------------------------------- library ---

def load_library():
    with lib_lock:
        if LIBRARY.exists():
            try:
                return json.loads(LIBRARY.read_text())
            except json.JSONDecodeError:
                pass
        return {"sources": [], "clips": []}


def update_library(change):
    """Read the library, apply change(lib), and write it back atomically."""
    with lib_lock:
        lib = load_library()
        change(lib)
        tmp = LIBRARY.with_suffix(".tmp")
        tmp.write_text(json.dumps(lib, indent=2))
        tmp.replace(LIBRARY)


# ------------------------------------------------------------------- jobs ---

def start_job(kind, label, target):
    job = {
        "id": uuid.uuid4().hex[:10],
        "kind": kind,
        "label": label,
        "status": "running",
        "progress": 0.0,
        "message": "Starting",
        "result": None,
        "created": time.time(),
    }
    with jobs_lock:
        jobs[job["id"]] = job

    def run():
        job["message"] = "Waiting for a free slot"
        with job_slots:  # keeps a small server from running too many ffmpeg jobs at once
            job["message"] = "Starting"
            try:
                job["result"] = target(job)
                job["progress"] = 1.0
                job["message"] = "Done"
                job["status"] = "done"
            except Exception as exc:  # surfaced to the UI
                job["message"] = str(exc) or exc.__class__.__name__
                job["status"] = "error"

    threading.Thread(target=run, daemon=True).start()
    return job


# ----------------------------------------------------------------- ffmpeg ---

def run_ffmpeg(args, duration, job, base=0.0, span=1.0, cwd=None):
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin",
           "-progress", "pipe:1", "-nostats", *args]
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, cwd=cwd)
    for line in proc.stdout:
        if line.startswith(("out_time_us=", "out_time_ms=")):
            try:
                micros = int(line.split("=", 1)[1])
            except ValueError:
                continue
            if duration > 0:
                job["progress"] = base + span * min(max(micros / 1e6 / duration, 0), 1)
    err = proc.stderr.read().strip()
    if proc.wait() != 0:
        raise RuntimeError(err.splitlines()[-1] if err else "ffmpeg failed")


def probe(path):
    result = subprocess.run(
        ["ffprobe", "-v", "error", "-print_format", "json", "-show_format", "-show_streams", str(path)],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        raise RuntimeError("That file isn't a video or audio file ffmpeg can read")
    info = json.loads(result.stdout)
    streams = info.get("streams", [])
    video = next((s for s in streams if s.get("codec_type") == "video"), None)
    audio = next((s for s in streams if s.get("codec_type") == "audio"), None)
    if not video and not audio:
        raise RuntimeError("No video or audio found in that file")
    try:
        duration = float(info.get("format", {}).get("duration"))
    except (TypeError, ValueError):
        duration = 0.0
    return {
        "duration": duration,
        "has_video": bool(video),
        "width": video.get("width") if video else None,
        "height": video.get("height") if video else None,
        "vcodec": video.get("codec_name") if video else None,
        "acodec": audio.get("codec_name") if audio else None,
    }


def needs_proxy(path, info):
    """True when browsers probably can't play the file, so we make a preview copy."""
    ext = path.suffix.lower()
    if not info["has_video"]:
        return ext not in AUDIO_EXTS
    if ext == ".webm":
        return False
    return not (
        ext in {".mp4", ".m4v", ".mov"}
        and info["vcodec"] == "h264"
        and info["acodec"] in (None, "aac", "mp3")
    )


def register_source(job, path, title, origin, url=None):
    try:
        job["message"] = "Reading the file"
        info = probe(path)
        preview = path.name
        if needs_proxy(path, info):
            job["message"] = "Making a preview that plays in your browser"
            base = job["progress"]
            span = max(0.0, 0.98 - base)
            if info["has_video"]:
                preview = f"{path.stem}.preview.mp4"
                args = ["-y", "-i", str(path),
                        "-vf", "scale=-2:'min(720,ih)'", "-pix_fmt", "yuv420p",
                        "-c:v", "libx264", "-preset", "veryfast", "-crf", "26",
                        "-c:a", "aac", "-b:a", "128k", "-movflags", "+faststart",
                        str(SOURCES / preview)]
            else:
                preview = f"{path.stem}.preview.mp3"
                args = ["-y", "-i", str(path), "-vn", "-c:a", "libmp3lame", "-q:a", "4",
                        str(SOURCES / preview)]
            run_ffmpeg(args, info["duration"], job, base=base, span=span)
    except Exception:
        for leftover in SOURCES.glob(f"{path.stem}*"):
            leftover.unlink(missing_ok=True)
        raise

    entry = {
        "id": path.stem,
        "title": title,
        "origin": origin,
        "url": url,
        "file": path.name,
        "preview": preview,
        "duration": info["duration"],
        "width": info["width"],
        "height": info["height"],
        "has_video": info["has_video"],
        "created": time.time(),
    }
    update_library(lambda lib: lib["sources"].insert(0, entry))
    return {"source_id": entry["id"]}


# ---------------------------------------------------------------- YouTube ---

def download_youtube(job, url, quality):
    try:
        import yt_dlp
    except ImportError:
        raise RuntimeError("yt-dlp isn't installed. Run: pip install -U yt-dlp")

    src_id = uuid.uuid4().hex[:10]

    def hook(d):
        if d.get("status") == "downloading":
            total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
            if total:
                job["progress"] = min(d.get("downloaded_bytes", 0) / total, 1) * 0.8
            job["message"] = "Downloading from YouTube"
        elif d.get("status") == "finished":
            job["message"] = "Finishing the download"

    options = {
        "format": QUALITY_FORMATS.get(quality, QUALITY_FORMATS["720"]),
        "outtmpl": str(SOURCES / f"{src_id}.%(ext)s"),
        "merge_output_format": "mp4",
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "progress_hooks": [hook],
    }
    cookie_copy = None
    if YT_COOKIES and Path(YT_COOKIES).is_file():
        cookie_copy = DATA / f"cookies-{src_id}.txt"  # yt-dlp rewrites this file, so use a copy
        shutil.copy(YT_COOKIES, cookie_copy)
        options["cookiefile"] = str(cookie_copy)
    try:
        with yt_dlp.YoutubeDL(options) as ydl:
            info = ydl.extract_info(url, download=True)
    except Exception as exc:
        for leftover in SOURCES.glob(f"{src_id}*"):
            leftover.unlink(missing_ok=True)
        message = re.sub(r"\x1b\[[0-9;]*m", "", str(exc)).replace("ERROR: ", "").strip()
        message = message.split("; please report")[0]
        raise RuntimeError(message.splitlines()[-1] if message else "YouTube download failed")
    finally:
        if cookie_copy:
            cookie_copy.unlink(missing_ok=True)

    files = [p for p in SOURCES.glob(f"{src_id}.*") if p.suffix not in {".part", ".ytdl"}]
    if not files:
        raise RuntimeError("The download finished but the file wasn't found")
    job["progress"] = max(job["progress"], 0.85)
    return register_source(job, files[0], info.get("title") or "YouTube video", "youtube", url)


# ------------------------------------------------------------------ clips ---

def safe_name(text):
    cleaned = re.sub(r"[^\w\- ]+", "", text, flags=re.UNICODE).strip().replace(" ", "_")
    return cleaned[:80] or "clip"


def stamp(seconds):
    m, s = divmod(int(round(seconds)), 60)
    h, m = divmod(m, 60)
    return f"{h}h{m:02d}m{s:02d}s" if h else f"{m}m{s:02d}s"


def cut_args(mode, source, start, length, out):
    head = ["-y", "-ss", f"{start:.3f}", "-i", str(source), "-t", f"{length:.3f}"]
    if mode == "copy":
        return head + ["-c", "copy", "-avoid_negative_ts", "make_zero", str(out)]
    if mode == "mp3":
        return head + ["-vn", "-c:a", "libmp3lame", "-q:a", "2", str(out)]
    return head + ["-pix_fmt", "yuv420p", "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
                   "-c:a", "aac", "-b:a", "160k", "-movflags", "+faststart", str(out)]


# -------------------------------------------------------- speech and AI ---

CAPTION_STYLES = {"pop", "clean", "box", "off"}
CAPTION_POSITIONS = {"lower", "center"}
CAPTION_COLORS = set(captions.COLORS)
CAPTION_SCRIPTS = {"native", "hinglish"}


def read_caption_color(data):
    """The caption colour chosen in the app. Returns (colour, error text or None)."""
    color = data.get("caption_color") or "default"
    return (color, None) if color in CAPTION_COLORS else (None, "Unknown caption colour")
LAYOUTS = {"fill", "blur", "split"}
LANG_CODES = {"auto", "en", "hi", "es", "fr", "de", "pt", "ar", "ta", "te", "bn", "mr"}
VOICE_IDS = {voice for voice, _ in speech.VOICES}


def read_settings():
    try:
        return json.loads(SETTINGS.read_text())
    except (OSError, json.JSONDecodeError):
        return {}


def write_settings(values):
    with lib_lock:
        current = read_settings()
        current.update(values)
        SETTINGS.write_text(json.dumps(current))
        try:
            SETTINGS.chmod(0o600)
        except OSError:
            pass


def youtube_settings():
    return read_settings().get("youtube") or {}


def youtube_redirect_uri():
    # Google requires an exact match, so this must be the address Clipper is reached at, port included.
    # It matches what you register in Google Cloud Console (see the connect box in AI settings).
    return f"http://127.0.0.1:{PORT}/api/youtube/oauth/callback"


def youtube_access_token():
    """A fresh access token for the connected channel, refreshed if needed. None if not connected."""
    cfg = youtube_settings()
    if not cfg.get("refresh_token"):
        return None
    return yt_api.refresh_access_token(cfg["client_id"], cfg["client_secret"], cfg["refresh_token"])


AI_KEY_ENV_VARS = ("GEMINI_API_KEY", "GOOGLE_API_KEY", "ANTHROPIC_API_KEY")


def ai_env_key():
    for name in AI_KEY_ENV_VARS:
        value = os.environ.get(name, "").strip()
        if value:
            return value
    return ""


def ai_key():
    """A Gemini or Anthropic key, from the server's environment or from AI settings in the app."""
    return ai_env_key() or read_settings().get("anthropic_key", "")


def load_transcript(src_id):
    try:
        return json.loads((TRANSCRIPTS / f"{src_id}.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def save_transcript(src_id, data):
    (TRANSCRIPTS / f"{src_id}.json").write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


def new_tmp(job):
    path = TMP / job["id"]
    path.mkdir(parents=True, exist_ok=True)
    return path


def extract_wav(job, path, start, length, wav, base, span, total):
    args = ["-y"]
    if start:
        args += ["-ss", f"{start:.3f}"]
    args += ["-i", str(path)]
    if length:
        args += ["-t", f"{length:.3f}"]
    args += ["-vn", "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", str(wav)]
    run_ffmpeg(args, total, job, base=base, span=span)


def whisper_words(job, wav, language, duration, base, span):
    if not speech.whisper_available():
        raise RuntimeError("Captions need faster-whisper. Run: pip install faster-whisper")
    if not speech.model_loaded():
        job["message"] = "Loading the speech model (the first time it downloads a few hundred MB)"
        speech.load_model()
    job["message"] = "Transcribing speech"
    return speech.transcribe(wav, language, duration, lambda p: job.__setitem__("progress", base + span * p))


def transcribe_full(job, src, language, tmp, base, span):
    """Transcribe the whole video once and keep the result for captions and AI."""
    job["message"] = "Extracting the audio"
    wav = tmp / "full.wav"
    extract_wav(job, SOURCES / src["file"], 0, 0, wav, base, span * 0.05, src["duration"])
    result = whisper_words(job, wav, language, src["duration"], base + span * 0.05, span * 0.95)
    save_transcript(src["id"], {"language": result["language"], "words": result["words"]})
    wav.unlink(missing_ok=True)
    return result["words"]


def words_for_range(job, src, start, end, language, tmp, base, span):
    """Words spoken between start and end, with times counted from the start of the clip."""
    length = end - start
    stored = load_transcript(src["id"])
    if stored:
        picked = []
        for w in stored["words"]:
            if start <= (w["s"] + w["e"]) / 2 <= end:
                picked.append({"w": w["w"], "s": max(0.0, w["s"] - start), "e": min(length, w["e"] - start)})
        return picked
    job["message"] = "Extracting the audio"
    wav = tmp / "range.wav"
    extract_wav(job, SOURCES / src["file"], start, length, wav, base, span * 0.1, length)
    result = whisper_words(job, wav, language, length, base + span * 0.1, span * 0.9)
    return result["words"]


def prepare_captions(tmp, words, style, position, cap_color="default", hook=None, duration=None):
    (tmp / "captions.ass").write_text(
        captions.build_ass(words, style, position, color=cap_color, hook=hook, duration=duration), encoding="utf-8")
    shutil.copytree(FONTS, tmp / "fonts", dirs_exist_ok=True)
    return "captions.ass"


# ------------------------------------------------------------ summary videos ---
# A long video becomes a 16:9 recap made of many short scenes. Every scene is rendered on its own (captions
# burned in) with identical settings, then the scenes are joined without re-encoding.

SUMMARY_W, SUMMARY_H = 1280, 720
SUMMARY_MINUTES = {3, 5, 10, 15, 20, 30}
SEGMENT_ENCODE = ["-c:v", "libx264", "-preset", "veryfast", "-crf", "22", "-pix_fmt", "yuv420p", "-r", "30",
                  "-c:a", "aac", "-b:a", "160k", "-ar", "44100", "-ac", "2"]


def words_in_range(words, start, end):
    """The words spoken between start and end, with times counted from `start`."""
    out = []
    for w in words:
        mid = (w["s"] + w["e"]) / 2
        if start <= mid < end:
            out.append({"w": w["w"], "s": max(0.0, w["s"] - start), "e": min(end - start, w["e"] - start)})
    return out


def speak(text, voice, rate, path):
    """Voiceover for one scene. Tries twice, because the free voice service sometimes hiccups."""
    for attempt in range(2):
        try:
            return speech.synthesize(text, voice, rate, path)
        except RuntimeError:
            if attempt:
                raise
            time.sleep(2)


def render_segment(job, tmp, n, source, start, length, words, narration, keep_volume, style, position, out,
                   base, span, cap_color="default"):
    """One scene: `length` seconds of the source, captions burned in, and optionally a narrator on top."""
    caption_file = None
    if style != "off" and words:
        caption_file = f"seg{n:03d}.ass"
        (tmp / caption_file).write_text(
            captions.build_ass(words, style, position, size=(SUMMARY_W, SUMMARY_H), color=cap_color),
            encoding="utf-8")
    graph = (f"[0:v]scale={SUMMARY_W}:{SUMMARY_H}:force_original_aspect_ratio=decrease,"
             f"pad={SUMMARY_W}:{SUMMARY_H}:(ow-iw)/2:(oh-ih)/2,setsar=1,fps=30")
    graph += f",ass={caption_file}:fontsdir=fonts[v]" if caption_file else "[v]"
    args = ["-y", "-ss", f"{start:.3f}", "-i", str(source)]
    if narration:
        args += ["-i", str(narration)]
        if keep_volume > 0:                              # the original sound stays underneath, quietly
            graph += (f";[0:a]volume={keep_volume:.2f}[o];[1:a]apad[n];"
                      "[o][n]amix=inputs=2:duration=longest:normalize=0[a]")
        else:
            graph += ";[1:a]apad[a]"
        audio = "[a]"
    else:
        audio = "0:a"
    args += ["-t", f"{length:.3f}", "-filter_complex", graph, "-map", "[v]", "-map", audio,
             *SEGMENT_ENCODE, str(out)]
    run_ffmpeg(args, length, job, base=base, span=span, cwd=tmp)


def join_segments(job, tmp, pieces, out, total, base, span):
    (tmp / "pieces.txt").write_text("".join(f"file '{p.name}'\n" for p in pieces), encoding="utf-8")
    run_ffmpeg(["-y", "-f", "concat", "-safe", "0", "-i", "pieces.txt", "-c", "copy",
                "-movflags", "+faststart", str(out)], total, job, base=base, span=span, cwd=tmp)


def build_summary(job, key, src, o):
    source = SOURCES / src["file"]
    duration = src["duration"]
    out = o["out"]
    has_audio = bool(probe(source)["acodec"])
    if o["style"] == "highlights" and not has_audio:
        raise RuntimeError("This video has no sound, so there is no speech to keep and caption. "
                           "Use the narrator style instead.")
    tmp = new_tmp(job)
    try:
        stored = load_transcript(src["id"])
        words = stored["words"] if stored else transcribe_full(job, src, o["language"], tmp, 0.0, 0.45)
        if not words:
            raise RuntimeError("No speech was found in this video, so there is nothing to summarise.")
        job["progress"] = max(job["progress"], 0.45)
        shutil.copytree(FONTS, tmp / "fonts", dirs_exist_ok=True)

        pieces, lengths = [], []
        if o["style"] == "explained":
            job["message"] = "Asking the AI to write the story"
            scenes = ai.plan_recap(key, words, duration, o["minutes"], o["narration"])
            job["progress"] = 0.5
            keep = o["keep_volume"] if has_audio else 0.0
            for i, scene in enumerate(scenes):
                job["message"] = f"Scene {i + 1} of {len(scenes)}: voiceover and picture"
                voice_file = tmp / f"voice{i:03d}.mp3"
                bounds = speak(scene["text"], o["voice"], o["rate"], voice_file)
                spoken_len = probe(voice_file)["duration"]
                spoken = (speech.words_from_boundaries(bounds, scene["text"], spoken_len)
                          or speech.estimate_words(scene["text"], spoken_len))
                length = spoken_len + 0.35                       # a short breath between scenes
                start = max(0.0, min(scene["start"], duration - length))
                piece = tmp / f"seg{i:03d}.mp4"
                render_segment(job, tmp, i, source, start, length, spoken, voice_file, keep, o["captions"],
                               o["caption_pos"], piece, 0.5 + 0.45 * i / len(scenes), 0.45 / len(scenes), o["caption_color"])
                voice_file.unlink(missing_ok=True)
                pieces.append(piece)
                lengths.append(length)
                time.sleep(0.2)
        else:
            job["message"] = "Asking the AI to choose the scenes"
            picks = ai.plan_highlights(key, words, duration, o["minutes"])
            job["progress"] = 0.5
            for i, pick in enumerate(picks):
                job["message"] = f"Scene {i + 1} of {len(picks)}: cutting and captioning"
                length = pick["end"] - pick["start"]
                piece = tmp / f"seg{i:03d}.mp4"
                render_segment(job, tmp, i, source, pick["start"], length,
                               words_in_range(words, pick["start"], pick["end"]), None, 0.0, o["captions"],
                               o["caption_pos"], piece, 0.5 + 0.45 * i / len(picks), 0.45 / len(picks), o["caption_color"])
                pieces.append(piece)
                lengths.append(length)

        job["message"] = "Joining the scenes"
        music = o["music"]
        target = tmp / "summary.mp4" if music else out
        finish = 0.97 if music else 1.0
        join_segments(job, tmp, pieces, target, sum(lengths), 0.95, finish - 0.95)
        if music:
            mix_music(job, target, music, out, finish, 1 - finish)
        total = probe(out)["duration"]
    except Exception:
        out.unlink(missing_ok=True)
        raise
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    entry = add_clip({"name": out.stem, "file": out.name, "kind": "summary", "source_id": src["id"],
                      "source_title": src["title"], "start": 0.0, "end": total, "mode": "summary",
                      "has_video": True})
    job["message"] = f"Made a {total / 60:.0f} minute video from {len(pieces)} scenes"
    return {"clip_id": entry["id"], "scenes": len(pieces)}


def mix_music(job, video_path, music, out, base=0.95, span=0.05):
    """Lay a music track under a finished video and write the result to `out`.

    music = {"file": name in sources, "volume": 0.02 to 1.0, "seconds": 0 for the whole video,
             or a number of seconds: the music then plays only that long from the start}.
    The video picture is copied as it is, so this step is quick.
    """
    info = probe(video_path)
    duration = info["duration"]
    music_path = SOURCES / music["file"]
    if not probe(music_path)["acodec"]:
        raise RuntimeError("The background music file has no sound in it. Pick another file.")
    job["message"] = "Adding the background music"
    volume, seconds = music["volume"], music["seconds"]
    if seconds > 0:                                     # a short sound at the start only
        seconds = min(float(seconds), duration)
        fade = min(0.3, seconds / 2)
        chain = (f"atrim=0:{seconds:.3f},asetpts=PTS-STARTPTS,"
                 f"afade=t=out:st={seconds - fade:.3f}:d={fade:.3f},volume={volume:.3f}")
        music_input = ["-i", str(music_path)]
    else:                                               # under the whole video, looped if it is short
        fade = min(1.5, duration / 2)
        chain = f"volume={volume:.3f},afade=t=out:st={duration - fade:.3f}:d={fade:.3f}"
        music_input = ["-stream_loop", "-1", "-i", str(music_path)]
    if info["acodec"]:
        graph = (f"[1:a]{chain}[m];"
                 "[0:a][m]amix=inputs=2:duration=first:dropout_transition=0:normalize=0[a]")
    else:
        graph = f"[1:a]{chain}[a]"
    args = ["-y", "-i", str(video_path), *music_input, "-filter_complex", graph,
            "-map", "0:v", "-map", "[a]", "-c:v", "copy", "-c:a", "aac", "-b:a", "192k",
            "-t", f"{duration:.3f}", "-movflags", "+faststart", str(out)]
    try:
        run_ffmpeg(args, duration, job, base=base, span=span)
    except RuntimeError as exc:
        if "normalize" in str(exc):
            raise RuntimeError("Your ffmpeg is too old for background music. Update it "
                               "(on Windows: winget upgrade Gyan.FFmpeg) and start Clipper again.")
        raise


def read_music(data, sources):
    """Background music chosen in the app. Returns (settings or None, error text or None)."""
    music_id = data.get("music_id") or ""
    if not music_id or music_id == "none":
        return None, None
    try:
        volume = float(data.get("music_volume", 0.2))
        seconds = int(data.get("music_seconds", 0))
    except (TypeError, ValueError):
        return None, "Unknown music setting"
    if not 0.02 <= volume <= 1.0 or not 0 <= seconds <= 60:
        return None, "Unknown music setting"
    found = next((s for s in sources if s["id"] == music_id), None)
    if not found:
        return None, "That music file is no longer in the library"
    return {"id": found["id"], "file": found["file"], "volume": volume, "seconds": seconds}, None


def music_from_cfg(cfg):
    """The music for a batch item, looked up again in case the library changed."""
    music_id = cfg.get("music_id")
    if not music_id:
        return None
    music, err = read_music({"music_id": music_id, "music_volume": cfg.get("music_volume", 0.2),
                             "music_seconds": cfg.get("music_seconds", 0)}, load_library()["sources"])
    if err:
        raise RuntimeError("The background music file was removed from the library.")
    return music


def render_short(job, tmp, src, start, length, layout, bg, style, position, language, out, music=None,
                 cap_color="default", cap_script="native", key=None, hook_text=None, hook_auto=False):
    caption_file, base, words = None, 0.0, None
    if style != "off" or hook_auto:
        words = words_for_range(job, src, start, start + length, language, tmp, 0.0, 0.35)
        if style != "off" and not words:
            raise RuntimeError("No speech was found in this part, so there is nothing to caption. "
                               "Turn captions off or pick a part with talking.")
        if cap_script == "hinglish" and words:
            job["message"] = "Writing the captions in Hinglish"
            words = ai.romanize_words(key, words)
            job["progress"] = max(job["progress"], 0.42)
    hook = hook_text
    if hook_auto:
        job["message"] = "Writing a hook"
        hook = ai.write_hook(key, words or [], length)
        job["progress"] = max(job["progress"], 0.47)
    if style != "off" or hook:
        caption_file, base = prepare_captions(
            tmp, words if style != "off" else [], style, position, cap_color, hook=hook, duration=length), 0.5
    job["message"] = "Rendering the short"
    bg_path = SOURCES / bg["file"] if bg else None
    bg_start = random.uniform(0, bg["duration"] - length) if bg and bg["duration"] > length else 0.0
    target = tmp / "no-music.mp4" if music else out
    finish = 0.95 if music else 1.0
    args = captions.short_args(SOURCES / src["file"], start, length, layout, bg_path, bg_start, caption_file, target)
    run_ffmpeg(args, length, job, base=base, span=finish - base, cwd=tmp)
    if music:
        mix_music(job, target, music, out, finish, 1 - finish)


def render_script(job, tmp, text, voice, rate, bg, color, style, position, out, music=None, cap_color="default"):
    job["message"] = "Creating the voiceover"
    voice_path = tmp / "voice.mp3"
    bounds = speech.synthesize(text, voice, rate, voice_path)
    duration = probe(voice_path)["duration"]
    job["progress"] = 0.25
    words = speech.words_from_boundaries(bounds, text, duration)
    if words is None:                        # the voice service sent no word timing
        try:
            words = whisper_words(job, voice_path, None, duration, 0.25, 0.15)["words"]
        except Exception:
            words = speech.estimate_words(text, duration)
    caption_file = prepare_captions(tmp, words, style, position, cap_color) if style != "off" else None
    job["message"] = "Rendering the video"
    bg_path = SOURCES / bg["file"] if bg else None
    bg_start = random.uniform(0, bg["duration"] - duration) if bg and bg["duration"] > duration + 1 else 0.0
    target = tmp / "no-music.mp4" if music else out
    finish = 0.95 if music else 1.0
    args = captions.script_args(voice_path, duration + 0.4, bg_path, bg_start, color, caption_file, target)
    run_ffmpeg(args, duration + 0.4, job, base=0.4, span=finish - 0.4, cwd=tmp)
    if music:
        mix_music(job, target, music, out, finish, 1 - finish)


# ----------------------------------------------------------------- routes ---

def disk_error():
    if shutil.disk_usage(DATA).free / 1e9 < MIN_FREE_GB:
        return jsonify(error="The server is almost out of disk space. Delete some videos or clips and try again."), 507
    return None


@app.errorhandler(413)
def too_large(_):
    return jsonify(error=f"That file is bigger than the {MAX_UPLOAD_MB} MB upload limit"), 413


@app.before_request
def guard():
    """Host check, same-origin check and (on a server) the login."""
    if request.path == "/healthz":
        return None
    hostname = (urlparse("//" + request.host).hostname or "").lower()
    allowed = ALLOWED_HOSTS or ({"127.0.0.1", "localhost"} if IS_LOCAL else set())
    if allowed and hostname not in allowed:
        abort(403)
    if LOGIN_PASSWORD:
        auth = request.authorization
        ok = (
            auth is not None
            and hmac.compare_digest((auth.username or "").encode(), LOGIN_USER.encode())
            and hmac.compare_digest((auth.password or "").encode(), LOGIN_PASSWORD.encode())
        )
        if not ok:
            if auth is not None:
                time.sleep(1)  # slows down password guessing
            return Response("Login required", 401, {"WWW-Authenticate": 'Basic realm="Clipper", charset="UTF-8"'})
    if request.method in {"POST", "DELETE"}:
        origin = request.headers.get("Origin")
        if origin and urlparse(origin).netloc.lower() != request.host.lower():
            abort(403)
    return None


@app.get("/healthz")
def healthz():
    return "ok"


@app.get("/")
def index():
    return send_from_directory(BASE / "static", "index.html")


@app.get("/api/library")
def library():
    lib = load_library()
    lib["tools"] = {
        "ytdlp": importlib.util.find_spec("yt_dlp") is not None,
        "whisper": speech.whisper_available(),
        "tts": speech.tts_available(),
        "ai": bool(ai_key()),
        "ai_from_env": bool(ai_env_key()),
    }
    return jsonify(lib)


@app.get("/api/jobs/<job_id>")
def job_status(job_id):
    job = jobs.get(job_id)
    if not job:
        return jsonify(error="Job not found"), 404
    return jsonify(job)


@app.post("/api/upload")
def upload():
    if (err := disk_error()):
        return err
    file = request.files.get("file")
    if not file or not file.filename:
        return jsonify(error="No file received"), 400
    ext = Path(file.filename).suffix.lower()
    if not re.fullmatch(r"\.[a-z0-9]{1,5}", ext):
        return jsonify(error="That file type isn't supported"), 400
    path = SOURCES / f"{uuid.uuid4().hex[:10]}{ext}"
    file.save(path)
    title = Path(file.filename).stem
    return jsonify(start_job("upload", title, lambda job: register_source(job, path, title, "upload")))


@app.post("/api/youtube")
def youtube():
    if (err := disk_error()):
        return err
    data = request.get_json(silent=True) or {}
    url = (data.get("url") or "").strip()
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or (parsed.hostname or "").lower() not in YOUTUBE_HOSTS:
        return jsonify(error="Paste a full YouTube link, like https://www.youtube.com/watch?v=..."), 400
    quality = data.get("quality", "720")
    return jsonify(start_job("youtube", url, lambda job: download_youtube(job, url, quality)))


@app.post("/api/clip")
def create_clip():
    if (err := disk_error()):
        return err
    data = request.get_json(silent=True) or {}
    lib = load_library()
    src = next((s for s in lib["sources"] if s["id"] == data.get("source_id")), None)
    if not src:
        return jsonify(error="Source not found"), 404
    try:
        start, end = float(data.get("start")), float(data.get("end"))
    except (TypeError, ValueError):
        return jsonify(error="Start and end must be numbers"), 400
    if start < 0 or end <= start or (src["duration"] and end > src["duration"] + 0.5):
        return jsonify(error="Pick a start and end inside the video"), 400

    mode = data.get("mode", "mp4")
    if mode not in {"mp4", "copy", "mp3", "short"}:
        return jsonify(error="Unknown export format"), 400
    layout = style = position = language = bg = music = None
    cap_color, cap_script = "default", "native"
    hook_text, hook_auto = None, False
    if mode == "short":
        if not src["has_video"]:
            return jsonify(error="Shorts need a video, not an audio file"), 400
        layout = data.get("layout", "fill")
        style = data.get("captions", "pop")
        position = data.get("caption_pos", "lower")
        language = data.get("language", "auto")
        if layout not in LAYOUTS or style not in CAPTION_STYLES or position not in CAPTION_POSITIONS \
                or language not in LANG_CODES:
            return jsonify(error="Unknown short setting"), 400
        if layout == "split":
            bg = next((s for s in lib["sources"]
                       if s["id"] == data.get("bg_id") and s["has_video"] and s["id"] != src["id"]), None)
            if not bg:
                return jsonify(error="Pick another video for the bottom half"), 400
        music, err = read_music(data, lib["sources"])
        if err:
            return jsonify(error=err), 400
        cap_color, err = read_caption_color(data)
        if err:
            return jsonify(error=err), 400
        cap_script = data.get("caption_script", "native")
        if cap_script not in CAPTION_SCRIPTS:
            return jsonify(error="Unknown caption setting"), 400
        if cap_script == "hinglish" and not ai_key():
            return jsonify(error="Hinglish captions need your Gemini or Anthropic API key under AI settings."), 400
        hook_text = (data.get("hook_text") or "").strip()[:200] or None
        hook_auto = bool(data.get("hook_auto")) and not hook_text
        if hook_auto and not ai_key():
            return jsonify(error="Writing a hook with AI needs your Gemini or Anthropic API key under AI settings."), 400
    if not src["has_video"] and mode == "mp4":
        mode = "mp3"
    ext = {"mp4": ".mp4", "mp3": ".mp3", "copy": Path(src["file"]).suffix, "short": ".mp4"}[mode]

    stem = safe_name(data.get("name") or f"{src['title']} {stamp(start)}-{stamp(end)}")
    out, n = CLIPS / f"{stem}{ext}", 2
    while out.exists():
        out = CLIPS / f"{stem}-{n}{ext}"
        n += 1

    def work(job):
        length = end - start
        tmp = new_tmp(job)
        try:
            if mode == "short":
                render_short(job, tmp, src, start, length, layout, bg, style, position, language, out, music,
                            cap_color, cap_script, ai_key(), hook_text, hook_auto)
            else:
                job["message"] = "Cutting the clip"
                run_ffmpeg(cut_args(mode, SOURCES / src["file"], start, length, out), length, job)
        except Exception:
            out.unlink(missing_ok=True)
            raise
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
        entry = {
            "id": uuid.uuid4().hex[:10],
            "name": out.stem,
            "file": out.name,
            "kind": "short" if mode == "short" else "clip",
            "source_id": src["id"],
            "source_title": src["title"],
            "start": start,
            "end": end,
            "mode": mode,
            "has_video": mode != "mp3",
            "created": time.time(),
        }
        update_library(lambda lib: lib["clips"].insert(0, entry))
        return {"clip_id": entry["id"]}

    return jsonify(start_job("clip", src["title"], work))


@app.post("/api/settings")
def save_settings():
    data = request.get_json(silent=True) or {}
    key = (data.get("anthropic_key") or "").strip()
    if key and not re.fullmatch(r"[\w\-.]{20,300}", key):
        return jsonify(error="That doesn't look like an API key"), 400
    write_settings({"anthropic_key": key})
    return jsonify(ok=True, ai=bool(ai_key()))


@app.get("/api/voices")
def voices():
    return jsonify(voices=[{"id": v, "label": label} for v, label in speech.VOICES],
                   rates=[{"id": k, "label": label} for k, label in speech.RATES.items()])


@app.post("/api/moments")
def find_moments():
    key = ai_key()
    if not key:
        return jsonify(error="Add your Gemini or Anthropic API key under AI settings first."), 400
    if (err := disk_error()):
        return err
    data = request.get_json(silent=True) or {}
    src = next((s for s in load_library()["sources"] if s["id"] == data.get("source_id")), None)
    if not src:
        return jsonify(error="Source not found"), 404
    if not src["duration"]:
        return jsonify(error="This file has no length information"), 400
    try:
        count = max(1, min(10, int(data.get("count", 5))))
        min_len, max_len = int(data.get("min_len", 30)), int(data.get("max_len", 60))
    except (TypeError, ValueError):
        return jsonify(error="Invalid settings"), 400
    if not 5 <= min_len < max_len <= 180:
        return jsonify(error="Invalid clip length"), 400
    language = data.get("language", "auto")
    if language not in LANG_CODES:
        return jsonify(error="Unknown language"), 400

    def work(job):
        tmp = new_tmp(job)
        try:
            stored = load_transcript(src["id"])
            words = stored["words"] if stored else transcribe_full(job, src, language, tmp, 0.0, 0.85)
            job["progress"] = max(job["progress"], 0.85)
            job["message"] = "Asking the AI for the best moments"
            found = ai.pick_moments(key, words, src["duration"], count, min_len, max_len)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

        def change(lib):
            for s in lib["sources"]:
                if s["id"] == src["id"]:
                    s["moments"] = found

        update_library(change)
        return {"count": len(found)}

    return jsonify(start_job("moments", src["title"], work))


@app.post("/api/summary")
def make_summary():
    key = ai_key()
    if not key:
        return jsonify(error="Add your Gemini or Anthropic API key under AI settings first."), 400
    if (err := disk_error()):
        return err
    data = request.get_json(silent=True) or {}
    lib = load_library()
    src = next((s for s in lib["sources"] if s["id"] == data.get("source_id")), None)
    if not src:
        return jsonify(error="Source not found"), 404
    if not src["has_video"]:
        return jsonify(error="Summary videos need a video, not an audio file"), 400
    try:
        minutes = int(data.get("minutes", 15))
    except (TypeError, ValueError):
        return jsonify(error="Unknown summary setting"), 400
    style, language, narration = data.get("style", "explained"), data.get("language", "auto"), data.get("narration", "english")
    voice, rate = data.get("voice", "en-IN-NeerjaNeural"), data.get("rate", "+0%")
    caption_style, position = data.get("captions", "clean"), data.get("caption_pos", "lower")
    keep_sound = data.get("keep_sound", "off")
    cap_color, color_err = read_caption_color(data)
    if color_err:
        return jsonify(error=color_err), 400
    if (minutes not in SUMMARY_MINUTES or style not in ("explained", "highlights") or language not in LANG_CODES
            or narration not in ai.LANGUAGES or voice not in VOICE_IDS or rate not in speech.RATES
            or caption_style not in CAPTION_STYLES or position not in CAPTION_POSITIONS
            or keep_sound not in ("off", "quiet")):
        return jsonify(error="Unknown summary setting"), 400
    if not src["duration"] or src["duration"] < minutes * 60 * 1.25:
        have = f"{(src['duration'] or 0) / 60:.0f}"
        return jsonify(error=f"This video is only {have} minutes long. Pick a shorter summary length."), 400
    music, err = read_music(data, lib["sources"])
    if err:
        return jsonify(error=err), 400

    opts = {"minutes": minutes, "style": style, "language": language, "narration": narration, "voice": voice,
            "rate": rate, "captions": caption_style, "caption_pos": position, "caption_color": cap_color,
            "keep_volume": 0.12 if keep_sound == "quiet" else 0.0, "music": music,
            "out": unique_path(safe_name(data.get("name") or f"{src['title'][:60]} summary"), ".mp4")}
    return jsonify(start_job("summary", src["title"], lambda job: build_summary(job, key, src, opts)))


@app.post("/api/hook/write")
def write_hook():
    key = ai_key()
    if not key:
        return jsonify(error="Add your Gemini or Anthropic API key under AI settings first."), 400
    data = request.get_json(silent=True) or {}
    lib = load_library()
    src = next((s for s in lib["sources"] if s["id"] == data.get("source_id")), None)
    if not src:
        return jsonify(error="Source not found"), 404
    try:
        start, end = float(data.get("start", 0)), float(data.get("end", 0))
    except (TypeError, ValueError):
        return jsonify(error="Unknown range"), 400
    if not (0 <= start < end <= src["duration"] + 0.5):
        return jsonify(error="That range is outside the video"), 400
    language = data.get("language", "auto")
    if language not in LANG_CODES:
        return jsonify(error="Unknown language"), 400

    def work(job):
        tmp = new_tmp(job)
        try:
            words = words_for_range(job, src, start, end, language, tmp, 0.0, 0.8)
            job["message"] = "Writing a hook"
            hook = ai.write_hook(key, words, end - start)
            return {"hook": hook}
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    return jsonify(start_job("hook", src["title"], work))


@app.post("/api/script/write")
def script_write():
    key = ai_key()
    if not key:
        return jsonify(error="Add your Gemini or Anthropic API key under AI settings first."), 400
    data = request.get_json(silent=True) or {}
    topic = (data.get("topic") or "").strip()
    if not 3 <= len(topic) <= 500:
        return jsonify(error="Describe the topic in a sentence or two"), 400
    try:
        seconds = int(data.get("seconds", 45))
    except (TypeError, ValueError):
        seconds = 45
    seconds = max(15, min(120, seconds))
    language = data.get("language", "english")
    if language not in ai.LANGUAGES:
        return jsonify(error="Unknown language"), 400

    def work(job):
        job["message"] = "Writing the script"
        return {"script": ai.write_script(key, topic, seconds, language)}

    return jsonify(start_job("script", topic[:60], work))


@app.post("/api/script/video")
def script_video():
    if (err := disk_error()):
        return err
    data = request.get_json(silent=True) or {}
    text = " ".join((data.get("script") or "").split())
    if not text:
        return jsonify(error="Write a script first"), 400
    if len(text) > 4000:
        return jsonify(error="Keep the script under 4000 characters"), 400
    voice, rate = data.get("voice", "en-IN-NeerjaNeural"), data.get("rate", "+0%")
    style, position = data.get("captions", "pop"), data.get("caption_pos", "lower")
    if voice not in VOICE_IDS or rate not in speech.RATES or style not in CAPTION_STYLES \
            or position not in CAPTION_POSITIONS:
        return jsonify(error="Unknown voice or caption setting"), 400
    bg, color = None, "#101320"
    if data.get("bg_type") == "video":
        bg = next((s for s in load_library()["sources"] if s["id"] == data.get("bg_id") and s["has_video"]), None)
        if not bg:
            return jsonify(error="Pick a background video from your library"), 400
    else:
        color = data.get("bg_color") or color
        if not re.fullmatch(r"#[0-9a-fA-F]{6}", color):
            return jsonify(error="Unknown background colour"), 400
    music, err = read_music(data, load_library()["sources"])
    if err:
        return jsonify(error=err), 400
    cap_color, err = read_caption_color(data)
    if err:
        return jsonify(error=err), 400

    stem = safe_name(data.get("name") or text[:40])
    out, n = CLIPS / f"{stem}.mp4", 2
    while out.exists():
        out = CLIPS / f"{stem}-{n}.mp4"
        n += 1

    def work(job):
        tmp = new_tmp(job)
        try:
            render_script(job, tmp, text, voice, rate, bg, color, style, position, out, music, cap_color)
            duration = probe(out)["duration"]
        except Exception:
            out.unlink(missing_ok=True)
            raise
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
        entry = {
            "id": uuid.uuid4().hex[:10],
            "name": out.stem,
            "file": out.name,
            "kind": "script",
            "source_id": bg["id"] if bg else None,
            "source_title": "a script",
            "start": 0.0,
            "end": duration,
            "mode": "script",
            "has_video": True,
            "created": time.time(),
        }
        update_library(lambda lib: lib["clips"].insert(0, entry))
        return {"clip_id": entry["id"]}

    return jsonify(start_job("script-video", text[:60], work))


@app.post("/api/batch/ideas")
def batch_ideas():
    key = ai_key()
    if not key:
        return jsonify(error="Add your Gemini or Anthropic API key under AI settings first."), 400
    data = request.get_json(silent=True) or {}
    niche = (data.get("niche") or "").strip()
    if not 3 <= len(niche) <= 300:
        return jsonify(error="Describe the niche in a few words"), 400
    try:
        count = max(5, min(100, int(data.get("count", 25))))
    except (TypeError, ValueError):
        count = 25
    language = data.get("language", "english")
    if language not in ai.LANGUAGES:
        return jsonify(error="Unknown language"), 400
    avoid = [a for a in (data.get("avoid") or []) if isinstance(a, str)][:150]

    def work(job):
        job["message"] = "Thinking up topics"
        return {"topics": ai.generate_topics(key, niche, count, language, avoid)}

    return jsonify(start_job("ideas", niche[:60], work))


@app.post("/api/batch/scripts")
def batch_scripts():
    if (err := disk_error()):
        return err
    data = request.get_json(silent=True) or {}
    mode = data.get("mode")
    entries = [" ".join(e.split()) for e in (data.get("entries") or []) if isinstance(e, str) and e.strip()]
    if mode not in {"scripts", "topics"}:
        return jsonify(error="Choose scripts or topics"), 400
    if not entries:
        return jsonify(error="Add at least one script or topic"), 400
    if len(entries) > BATCH_MAX_ITEMS:
        return jsonify(error=f"A batch can have up to {BATCH_MAX_ITEMS} videos. Split it into two batches."), 400
    if any(len(e) > (4000 if mode == "scripts" else 500) for e in entries):
        return jsonify(error="One of the entries is too long"), 400
    if mode == "topics" and not ai_key():
        return jsonify(error="Topics need the AI to write scripts. Add your Gemini or Anthropic API key under AI settings, "
                             "or switch to finished scripts."), 400
    voice, rate = data.get("voice", "en-IN-NeerjaNeural"), data.get("rate", "+0%")
    style, position = data.get("captions", "pop"), data.get("caption_pos", "center")
    language, bg = data.get("language", "english"), data.get("bg", "random")
    try:
        seconds = max(15, min(120, int(data.get("seconds", 45))))
    except (TypeError, ValueError):
        seconds = 45
    if (voice not in VOICE_IDS and voice not in MIX_GROUPS) or rate not in speech.RATES \
            or style not in CAPTION_STYLES or position not in CAPTION_POSITIONS or language not in ai.LANGUAGES:
        return jsonify(error="Unknown voice or caption setting"), 400
    if not (bg == "random" or re.fullmatch(r"color:#[0-9a-fA-F]{6}", bg or "")
            or (bg or "").startswith("video:")):
        return jsonify(error="Unknown background"), 400
    if bg.startswith("video:") and not any(s["id"] == bg[6:] and s["has_video"] for s in load_library()["sources"]):
        return jsonify(error="That background video is no longer in the library"), 400
    music, err = read_music(data, load_library()["sources"])
    if err:
        return jsonify(error=err), 400
    cap_color, err = read_caption_color(data)
    if err:
        return jsonify(error=err), 400

    items = [{"id": uuid.uuid4().hex[:8], "n": i, "label": e[:70],
              ("topic" if mode == "topics" else "script"): e, "status": "queued", "error": ""}
             for i, e in enumerate(entries, 1)]
    settings = {"mode": mode, "voice": voice, "rate": rate, "captions": style, "caption_pos": position,
                "language": language, "seconds": seconds, "bg": bg, "caption_color": cap_color}
    if music:
        settings.update(music_id=music["id"], music_volume=music["volume"], music_seconds=music["seconds"])
    b = new_batch("scripts", f"Voiceover videos ({len(items)})", settings, items)
    return jsonify(batch_summary(b))


@app.post("/api/batch/shorts")
def batch_shorts():
    if (err := disk_error()):
        return err
    data = request.get_json(silent=True) or {}
    library_sources = load_library()["sources"]
    ids = [i for i in (data.get("source_ids") or []) if isinstance(i, str)]
    sources = [s for s in library_sources if s["id"] in ids and s["has_video"]]
    if not sources:
        return jsonify(error="Pick at least one video"), 400
    try:
        per_source = max(1, min(100, int(data.get("per_source", 10))))
        min_len, max_len = int(data.get("min_len", 30)), int(data.get("max_len", 60))
        seg_len = max(10, min(120, int(data.get("seg_len", 45))))
    except (TypeError, ValueError):
        return jsonify(error="Invalid settings"), 400
    mode, layout = data.get("mode", "even"), data.get("layout", "fill")
    style, position = data.get("captions", "pop"), data.get("caption_pos", "lower")
    language, bg_id = data.get("language", "auto"), data.get("bg_id", "random")
    if mode not in {"ai", "even"} or layout not in LAYOUTS or style not in CAPTION_STYLES \
            or position not in CAPTION_POSITIONS or language not in LANG_CODES or not 5 <= min_len < max_len <= 180:
        return jsonify(error="Unknown setting"), 400
    if mode == "ai" and not ai_key():
        return jsonify(error="AI moments need your Gemini or Anthropic API key under AI settings. "
                             "Or pick 'Evenly spaced parts' instead."), 400
    if len(sources) * per_source > BATCH_MAX_ITEMS:
        return jsonify(error=f"That would be more than {BATCH_MAX_ITEMS} videos. Pick fewer videos or fewer shorts each."), 400
    if layout == "split" and sum(1 for s in library_sources if s["has_video"]) < 2:
        return jsonify(error="Split screen needs at least two videos in the library"), 400
    music, err = read_music(data, library_sources)
    if err:
        return jsonify(error=err), 400
    cap_color, err = read_caption_color(data)
    if err:
        return jsonify(error=err), 400
    cap_script = data.get("caption_script", "native")
    if cap_script not in CAPTION_SCRIPTS:
        return jsonify(error="Unknown caption setting"), 400
    if cap_script == "hinglish" and not ai_key():
        return jsonify(error="Hinglish captions need your Gemini or Anthropic API key under AI settings."), 400
    hook_mode = data.get("hook_mode", "none")
    if hook_mode not in {"none", "same", "auto"}:
        return jsonify(error="Unknown hook setting"), 400
    hook_text = (data.get("hook_text") or "").strip()[:200] if hook_mode == "same" else None
    if hook_mode == "same" and not hook_text:
        return jsonify(error="Type a hook, or choose a different hook option."), 400
    if hook_mode == "auto" and not ai_key():
        return jsonify(error="Writing a hook with AI needs your Gemini or Anthropic API key under AI settings."), 400

    settings = {"mode": mode, "per_source": per_source, "min_len": min_len, "max_len": max_len,
                "seg_len": seg_len, "layout": layout, "bg_id": bg_id, "captions": style,
                "caption_pos": position, "language": language, "caption_color": cap_color,
                "caption_script": cap_script, "hook_text": hook_text, "hook_auto": hook_mode == "auto"}
    if music:
        settings.update(music_id=music["id"], music_volume=music["volume"], music_seconds=music["seconds"])
    plan = [{"id": s["id"], "title": s["title"], "status": "queued", "error": ""} for s in sources]
    b = new_batch("shorts", f"Shorts from {len(sources)} video{'s' if len(sources) != 1 else ''}",
                  settings, [], plan)
    return jsonify(batch_summary(b))


@app.get("/api/batches")
def list_batches():
    ordered = sorted(list(batches.values()), key=lambda b: b["created"], reverse=True)
    return jsonify(batches=[batch_summary(b) for b in ordered])


@app.post("/api/batches/<batch_id>/<action>")
def batch_action(batch_id, action):
    b = batches.get(batch_id)
    if not b:
        return jsonify(error="Batch not found"), 404
    if action == "pause":
        batch_stop.setdefault(batch_id, threading.Event()).set()
        if b["status"] == "queued":
            b["status"], b["stage"] = "paused", "Paused. Click Resume to continue."
            save_batch(b)
    elif action in {"resume", "retry"}:
        if b["status"] in {"running", "queued"}:
            return jsonify(error="This batch is already running"), 409
        if action == "retry":
            for item in b["items"]:
                if item["status"] == "error":
                    item["status"], item["error"] = "queued", ""
            for bs in b.get("sources", []):
                if bs["status"] == "error":
                    bs["status"], bs["error"] = "queued", ""
        batch_stop.setdefault(batch_id, threading.Event()).clear()
        enqueue_batch(b)
    else:
        abort(404)
    return jsonify(batch_summary(b))


@app.delete("/api/batches/<batch_id>")
def delete_batch(batch_id):
    b = batches.get(batch_id)
    if not b:
        return jsonify(ok=True)
    if b["status"] in {"running", "queued"}:
        return jsonify(error="Pause the batch first, then remove it"), 409
    batches.pop(batch_id, None)
    (BATCHES / f"{batch_id}.json").unlink(missing_ok=True)
    return jsonify(ok=True)


def zip_response(clips, download_name):
    clips = [c for c in clips if (CLIPS / c["file"]).exists()]
    if not clips:
        return jsonify(error="There are no clips to download yet"), 404
    if (err := disk_error()):
        return err
    path = TMP / f"zip-{uuid.uuid4().hex[:8]}.zip"
    with zipfile.ZipFile(path, "w", zipfile.ZIP_STORED) as archive:   # videos are already compressed
        for c in clips:
            archive.write(CLIPS / c["file"], arcname=c["file"])
    response = send_file(path, as_attachment=True, download_name=download_name, mimetype="application/zip")
    response.call_on_close(lambda: path.unlink(missing_ok=True))
    return response


@app.get("/api/batches/<batch_id>/zip")
def batch_zip(batch_id):
    return zip_response([c for c in load_library()["clips"] if c.get("batch_id") == batch_id],
                        f"clipper-batch-{batch_id}.zip")


@app.get("/api/clips.zip")
def all_clips_zip():
    return zip_response(load_library()["clips"], "clipper-clips.zip")


@app.delete("/api/sources/<source_id>")
def delete_source(source_id):
    removed = []

    def change(lib):
        keep = []
        for s in lib["sources"]:
            (removed if s["id"] == source_id else keep).append(s)
        lib["sources"] = keep

    update_library(change)
    for s in removed:
        for name in {s["file"], s["preview"]}:
            (SOURCES / name).unlink(missing_ok=True)
        (TRANSCRIPTS / f"{s['id']}.json").unlink(missing_ok=True)
    return jsonify(ok=True)


@app.delete("/api/clips/<clip_id>")
def delete_clip(clip_id):
    removed = []

    def change(lib):
        keep = []
        for c in lib["clips"]:
            (removed if c["id"] == clip_id else keep).append(c)
        lib["clips"] = keep

    update_library(change)
    for c in removed:
        (CLIPS / c["file"]).unlink(missing_ok=True)
    return jsonify(ok=True)


@app.get("/media/<kind>/<path:filename>")
def media(kind, filename):
    folder = {"sources": SOURCES, "clips": CLIPS}.get(kind)
    if folder is None:
        abort(404)
    return send_from_directory(folder, filename, as_attachment=request.args.get("download") == "1")


@app.get("/api/youtube/status")
def youtube_status():
    cfg = youtube_settings()
    if not cfg.get("refresh_token"):
        return jsonify(connected=False, has_credentials=bool(cfg.get("client_id")))
    try:
        token = youtube_access_token()
        info = yt_api.channel_info(token)
    except RuntimeError as exc:
        return jsonify(connected=False, has_credentials=True, error=str(exc))
    return jsonify(connected=True, channel=info)


@app.post("/api/youtube/connect")
def youtube_connect():
    data = request.get_json(silent=True) or {}
    client_id = (data.get("client_id") or "").strip()
    client_secret = (data.get("client_secret") or "").strip()
    if not re.fullmatch(r"[\w.\-]{20,200}\.apps\.googleusercontent\.com", client_id):
        return jsonify(error="That doesn't look like a Google Client ID. It should end in "
                             ".apps.googleusercontent.com"), 400
    if not re.fullmatch(r"[\w\-]{10,100}", client_secret):
        return jsonify(error="That doesn't look like a Google Client Secret"), 400
    state = uuid.uuid4().hex
    write_settings({"youtube": {"client_id": client_id, "client_secret": client_secret,
                                "refresh_token": None, "oauth_state": state}})
    return jsonify(auth_url=yt_api.auth_url(client_id, youtube_redirect_uri(), state))


@app.get("/api/youtube/oauth/callback")
def youtube_oauth_callback():
    def page(message, ok):
        color = "#1a7f37" if ok else "#c0392b"
        body = (f"<p style='color:{color}'>{message}</p>"
               "<p>You can close this tab and go back to Clipper.</p>"
               "<script>setTimeout(() => window.close(), 4000)</script>")
        html = (f"<!doctype html><html><body style='font-family:sans-serif;padding:2rem;"
               f"max-width:32rem;margin:auto'><h2>Clipper</h2>{body}</body></html>")
        return Response(html, mimetype="text/html")

    cfg = youtube_settings()
    error = request.args.get("error")
    if error:
        return page(f"Google said: {error}. You can try Connect again from Clipper.", False)
    if not cfg.get("client_id") or request.args.get("state") != cfg.get("oauth_state"):
        return page("This link has expired or doesn't match. Go back to Clipper and click Connect again.", False)
    code = request.args.get("code")
    if not code:
        return page("Google didn't send back a code. Try Connect again.", False)
    try:
        refresh_token, access_token = yt_api.exchange_code(
            cfg["client_id"], cfg["client_secret"], youtube_redirect_uri(), code)
        info = yt_api.channel_info(access_token)
    except RuntimeError as exc:
        return page(str(exc), False)
    write_settings({"youtube": {**cfg, "refresh_token": refresh_token, "oauth_state": None}})
    return page(f"Connected to the YouTube channel \u201c{info['title']}\u201d.", True)


@app.post("/api/youtube/disconnect")
def youtube_disconnect():
    cfg = youtube_settings()
    if cfg.get("refresh_token"):
        yt_api.revoke(cfg["refresh_token"])
    write_settings({"youtube": {}})
    return jsonify(ok=True)


@app.post("/api/youtube/upload")
def youtube_upload():
    cfg = youtube_settings()
    if not cfg.get("refresh_token"):
        return jsonify(error="Connect your YouTube channel first, under AI settings."), 400
    data = request.get_json(silent=True) or {}
    clip = next((c for c in load_library()["clips"] if c["id"] == data.get("clip_id")), None)
    if not clip:
        return jsonify(error="Clip not found"), 404
    if not clip.get("has_video"):
        return jsonify(error="YouTube needs a video file, not an audio-only clip"), 400
    path = CLIPS / clip["file"]
    if not path.exists():
        return jsonify(error="That clip's file is missing"), 404
    title = (data.get("title") or clip["name"]).strip()[:100] or clip["name"]
    description = (data.get("description") or "").strip()
    tags = [t.strip() for t in (data.get("tags") or "").split(",") if t.strip()][:15]
    privacy = data.get("privacy", "private")
    category = data.get("category", "22")
    made_for_kids = bool(data.get("made_for_kids", False))
    if privacy not in yt_api.PRIVACY_STATUSES or category not in yt_api.CATEGORIES:
        return jsonify(error="Unknown upload setting"), 400
    if path.stat().st_size > 128 * 1024 * 1024 * 1024:
        return jsonify(error="That file is too large for YouTube's upload limit"), 400

    def work(job):
        job["message"] = "Signing in to YouTube"
        token = youtube_access_token()
        job["message"] = "Uploading to YouTube"

        def on_progress(fraction):
            job["progress"] = fraction
            job["message"] = f"Uploading to YouTube \u2013 {fraction * 100:.0f}%"

        video_id = yt_api.upload_video(token, path, title, description, tags, privacy, category,
                                        made_for_kids, on_progress)
        job["message"] = "Uploaded"
        return {"video_id": video_id, "url": f"https://youtu.be/{video_id}", "privacy": privacy}

    return jsonify(start_job("youtube-upload", title, work))


# ---------------------------------------------------------------- batches ---
# A batch is a saved to-do list of videos. One background worker goes through it item by item,
# retries failures, and can be paused and resumed (even after a restart).

batches = {}
batch_lock = threading.RLock()
batch_queue = queue.Queue()
batch_stop = {}
MIX_GROUPS = {"mix-en": "en-", "mix-hi": "hi-"}


def save_batch(b):
    with batch_lock:
        path = BATCHES / f"{b['id']}.json"
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(b, ensure_ascii=False), encoding="utf-8")
        tmp.replace(path)


def load_batches():
    for path in sorted(BATCHES.glob("*.json")):
        try:
            b = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if b.get("status") in {"running", "queued"}:      # the app was closed mid-batch
            b["status"] = "paused"
            b["stage"] = "Interrupted. Click Resume to continue."
            for item in b["items"]:
                if item["status"] == "running":
                    item["status"] = "queued"
            for bs in b.get("sources", []):
                if bs["status"] == "planning":
                    bs["status"] = "queued"
        batches[b["id"]] = b


def batch_summary(b):
    items = list(b["items"])
    counts = Counter(i["status"] for i in items)
    unplanned = [s for s in b.get("sources", []) if s["status"] in {"queued", "planning"}]
    per_source = b["settings"].get("per_source", 0)
    return {
        "id": b["id"], "kind": b["kind"], "title": b["title"], "status": b["status"], "stage": b.get("stage", ""),
        "created": b["created"], "total": len(items) + per_source * len(unplanned),
        "done": counts["done"],
        "failed": counts["error"] + sum(1 for s in b.get("sources", []) if s["status"] == "error"),
        "notes": [f"{s['title']}: {s['error']}" for s in b.get("sources", []) if s.get("error")],
        "items": [{"label": i["label"], "status": i["status"], "error": i.get("error", "")} for i in items],
    }


def scratch_job():
    return {"id": uuid.uuid4().hex[:10], "progress": 0.0, "message": ""}


def unique_path(stem, ext):
    out, n = CLIPS / f"{stem}{ext}", 2
    while out.exists():
        out = CLIPS / f"{stem}-{n}{ext}"
        n += 1
    return out


def disk_low():
    return shutil.disk_usage(DATA).free / 1e9 < MIN_FREE_GB


def process_item(b, item, stop, work):
    """Run work(item) with up to three tries. Returns True when it succeeded."""
    for attempt in range(3):
        if stop.is_set():
            item["status"] = "queued"
            return False
        item["status"], item["error"] = "running", ""
        save_batch(b)
        try:
            with job_slots:
                work(item)
            item["status"] = "done"
            save_batch(b)
            return True
        except Exception as exc:
            item["error"] = str(exc) or exc.__class__.__name__
            if attempt < 2:
                time.sleep(3 * (attempt + 1))
    item["status"] = "error"
    save_batch(b)
    return False


def pick_voice(setting, n):
    if setting in MIX_GROUPS:
        pool = [v for v, _ in speech.VOICES if v.startswith(MIX_GROUPS[setting])]
        return pool[(n - 1) % len(pool)]
    return setting


def pick_background(setting):
    """Returns (library video or None, solid colour)."""
    videos = [s for s in load_library()["sources"] if s["has_video"]]
    if setting == "random" and videos:
        return random.choice(videos), "#101320"
    if setting.startswith("video:"):
        found = next((s for s in videos if s["id"] == setting[6:]), None)
        if found:
            return found, "#101320"
    if setting.startswith("color:"):
        return None, setting[6:]
    return None, "#101320"


def add_clip(entry_fields):
    entry = {"id": uuid.uuid4().hex[:10], "created": time.time(), **entry_fields}
    update_library(lambda lib: lib["clips"].insert(0, entry))
    return entry


def make_script_item(b, item, key):
    cfg = b["settings"]
    text = item.get("script") or ""
    if not text:
        if not key:
            raise RuntimeError("Add your Gemini or Anthropic API key under AI settings first.")
        text = ai.write_script(key, item["topic"], cfg["seconds"], cfg["language"])
        item["script"] = " ".join(text.split())[:4000]
        save_batch(b)
    text = " ".join(text.split())[:4000]
    bg, color = pick_background(cfg["bg"])
    music = music_from_cfg(cfg)
    out = unique_path(safe_name(f"{item['n']:03d} {item['label']}"), ".mp4")
    job = scratch_job()
    tmp = new_tmp(job)
    try:
        render_script(job, tmp, text, pick_voice(cfg["voice"], item["n"]), cfg["rate"], bg, color,
                      cfg["captions"], cfg["caption_pos"], out, music, cfg.get("caption_color", "default"))
        duration = probe(out)["duration"]
    except Exception:
        out.unlink(missing_ok=True)
        raise
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    entry = add_clip({"name": out.stem, "file": out.name, "kind": "script", "batch_id": b["id"],
                      "source_id": bg["id"] if bg else None, "source_title": "a script",
                      "start": 0.0, "end": duration, "mode": "script", "has_video": True})
    item["clip_id"] = entry["id"]


def even_segments(duration, count, seg_len):
    segments, t = [], 0.0
    while t + max(10.0, seg_len * 0.5) <= duration:
        segments.append((round(t, 1), round(min(duration, t + seg_len), 1)))
        t += seg_len
    if len(segments) > count:
        if count == 1:
            return segments[:1]
        picks = sorted({round(i * (len(segments) - 1) / (count - 1)) for i in range(count)})
        segments = [segments[i] for i in picks]
    return segments


def plan_source(b, bs, src, key):
    """Decide which parts of a video to turn into shorts, and add them to the batch."""
    cfg = b["settings"]
    words = None
    if cfg["mode"] == "ai" or cfg["captions"] != "off":
        words = (load_transcript(src["id"]) or {}).get("words")
        if words is None:
            b["stage"] = f"Transcribing {src['title'][:50]} (long videos take a while)"
            save_batch(b)
            job = scratch_job()
            tmp = new_tmp(job)
            try:
                with job_slots:
                    words = transcribe_full(job, src, cfg["language"], tmp, 0.0, 1.0)
            finally:
                shutil.rmtree(tmp, ignore_errors=True)
    if cfg["mode"] == "ai":
        if not key:
            raise RuntimeError("Add your Gemini or Anthropic API key under AI settings first.")
        b["stage"] = f"Asking the AI for the best moments in {src['title'][:50]}"
        save_batch(b)
        moments = ai.pick_moments(key, words, src["duration"], cfg["per_source"], cfg["min_len"], cfg["max_len"])
        if not moments:
            raise RuntimeError("The AI found no clear standalone moments in this video.")
        parts = [(m["start"], m["end"], m["title"]) for m in moments]
    else:
        parts = [(s, e, f"Part {i}") for i, (s, e) in
                 enumerate(even_segments(src["duration"], cfg["per_source"], cfg["seg_len"]), 1)]
        if not parts:
            raise RuntimeError("This video is too short to split into parts.")
    for start, end, title in parts:
        b["items"].append({"id": uuid.uuid4().hex[:8], "n": len(b["items"]) + 1, "source_id": src["id"],
                           "label": f"{src['title'][:40]}: {title}", "start": start, "end": end,
                           "status": "queued", "error": ""})
    save_batch(b)


def make_short_item(b, item, src):
    cfg = b["settings"]
    bg = None
    if cfg["layout"] == "split":
        options = [s for s in load_library()["sources"] if s["has_video"] and s["id"] != src["id"]]
        bg = next((s for s in options if s["id"] == cfg["bg_id"]), None) if cfg["bg_id"] != "random" else None
        bg = bg or (random.choice(options) if options else None)
        if not bg:
            raise RuntimeError("Split screen needs another video in the library for the bottom half.")
    music = music_from_cfg(cfg)
    length = item["end"] - item["start"]
    out = unique_path(safe_name(f"{item['n']:03d} {item['label']}"), ".mp4")
    job = scratch_job()
    tmp = new_tmp(job)
    try:
        render_short(job, tmp, src, item["start"], length, cfg["layout"], bg, cfg["captions"],
                     cfg["caption_pos"], cfg["language"], out, music, cfg.get("caption_color", "default"),
                     cfg.get("caption_script", "native"), ai_key(), cfg.get("hook_text"), cfg.get("hook_auto", False))
    except Exception:
        out.unlink(missing_ok=True)
        raise
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    entry = add_clip({"name": out.stem, "file": out.name, "kind": "short", "batch_id": b["id"],
                      "source_id": src["id"], "source_title": src["title"], "start": item["start"],
                      "end": item["end"], "mode": "short", "has_video": True})
    item["clip_id"] = entry["id"]


def run_script_batch(b, stop):
    key, total = ai_key(), len(b["items"])
    for item in b["items"]:
        if item["status"] != "queued":
            continue
        if stop.is_set():
            break
        if disk_low():
            b["stage"] = "Paused: the disk is almost full. Free some space, then click Resume."
            stop.set()
            break
        b["stage"] = f"Video {item['n']} of {total}: {item['label'][:60]}"
        if process_item(b, item, stop, lambda it: make_script_item(b, it, key)) and BATCH_PAUSE:
            time.sleep(BATCH_PAUSE)


def run_shorts_batch(b, stop):
    key = ai_key()
    for bs in b["sources"]:
        if stop.is_set():
            break
        src = next((s for s in load_library()["sources"] if s["id"] == bs["id"] and s["has_video"]), None)
        if not src:
            bs["status"], bs["error"] = "error", "This video is no longer in the library."
            save_batch(b)
            continue
        if bs["status"] in {"queued", "planning"}:
            bs["status"], bs["error"] = "planning", ""
            b["stage"] = f"Planning: {src['title'][:50]}"
            save_batch(b)
            try:
                plan_source(b, bs, src, key)
            except Exception as exc:
                bs["status"], bs["error"] = "error", str(exc)
                save_batch(b)
                continue
            bs["status"] = "planned"
            save_batch(b)
        for item in [i for i in b["items"] if i["source_id"] == src["id"] and i["status"] == "queued"]:
            if stop.is_set():
                break
            if disk_low():
                b["stage"] = "Paused: the disk is almost full. Free some space, then click Resume."
                stop.set()
                break
            b["stage"] = f"Short {item['n']}: {item['label'][:60]}"
            process_item(b, item, stop, lambda it: make_short_item(b, it, src))


def run_batch(b):
    stop = batch_stop.setdefault(b["id"], threading.Event())
    stop.clear()
    b["status"] = "running"
    save_batch(b)
    try:
        (run_script_batch if b["kind"] == "scripts" else run_shorts_batch)(b, stop)
    finally:
        if stop.is_set():
            b["status"] = "paused"
            b["stage"] = b["stage"] if b["stage"].startswith("Paused") else "Paused. Click Resume to continue."
        else:
            failed = sum(1 for i in b["items"] if i["status"] == "error")
            skipped = sum(1 for s in b.get("sources", []) if s["status"] == "error")
            b["status"] = "done"
            b["stage"] = "Finished" + (f" with {failed} failed" if failed else "") \
                + (f", {skipped} video{'s' if skipped != 1 else ''} skipped" if skipped else "")
        save_batch(b)


def batch_worker():
    while True:
        b = batches.get(batch_queue.get())
        if not b or b["status"] != "queued":
            continue
        try:
            run_batch(b)
        except Exception as exc:
            b["status"], b["stage"] = "error", f"Stopped: {exc}"
            save_batch(b)


def enqueue_batch(b):
    b["status"], b["stage"] = "queued", "Waiting to start"
    save_batch(b)
    batch_queue.put(b["id"])


def sweep_orphans():
    """Delete half-written files left behind by a crash (files the library doesn't know about)."""
    lib = load_library()
    keep_clips = {c["file"] for c in lib["clips"]}
    keep_sources = {name for s in lib["sources"] for name in (s["file"], s["preview"])}
    for folder, keep in ((CLIPS, keep_clips), (SOURCES, keep_sources)):
        for path in folder.iterdir():
            if path.is_file() and path.name not in keep:
                path.unlink(missing_ok=True)


sweep_orphans()
load_batches()
threading.Thread(target=batch_worker, daemon=True).start()


def new_batch(kind, title, settings, items, sources=None):
    b = {"id": uuid.uuid4().hex[:8], "kind": kind, "title": title, "created": time.time(),
         "settings": settings, "items": items, "status": "queued", "stage": "Waiting to start"}
    if sources is not None:
        b["sources"] = sources
    with batch_lock:
        batches[b["id"]] = b
    enqueue_batch(b)
    return b


def cleanup_once():
    if RETENTION_HOURS > 0:
        cutoff = time.time() - RETENTION_HOURS * 3600
        old_sources, old_clips = [], []

        def change(lib):
            for key, bucket in (("sources", old_sources), ("clips", old_clips)):
                keep = []
                for item in lib[key]:
                    (bucket if item["created"] < cutoff else keep).append(item)
                lib[key] = keep

        update_library(change)
        for s in old_sources:
            for name in {s["file"], s["preview"]}:
                (SOURCES / name).unlink(missing_ok=True)
            (TRANSCRIPTS / f"{s['id']}.json").unlink(missing_ok=True)
        for c in old_clips:
            (CLIPS / c["file"]).unlink(missing_ok=True)
    with jobs_lock:
        for job_id in [j["id"] for j in jobs.values() if j["status"] != "running" and j["created"] < time.time() - 3600]:
            jobs.pop(job_id, None)


def cleanup_loop():
    while True:
        time.sleep(1800)
        try:
            cleanup_once()
        except Exception as exc:
            print("Cleanup problem:", exc)


threading.Thread(target=cleanup_loop, daemon=True).start()


if __name__ == "__main__":
    missing = [tool for tool in ("ffmpeg", "ffprobe") if not shutil.which(tool)]
    if missing:
        print(f"Missing: {', '.join(missing)}. Install ffmpeg first:")
        print("  macOS:   brew install ffmpeg")
        print("  Windows: winget install ffmpeg")
        print("  Linux:   sudo apt install ffmpeg")
        raise SystemExit(1)
    print(f"Clipper is running at http://{HOST}:{PORT}  (Ctrl+C to stop)")
    if IS_LOCAL and not os.environ.get("NO_BROWSER"):
        threading.Timer(1.2, lambda: webbrowser.open(f"http://{HOST}:{PORT}")).start()
    app.run(host=HOST, port=PORT, threaded=True)
