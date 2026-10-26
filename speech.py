"""Speech tools: turn speech into timed words (Whisper) and turn a script into a voiceover (edge-tts).

Words look like {"w": "hello", "s": 1.2, "e": 1.6} with times in seconds.
"""
import asyncio
import importlib.util
import os
import threading

_model = None
_model_lock = threading.Lock()
_models_dir = None
_batched = None   # None = not tried yet; False = tried and unavailable/broken; else the pipeline

# Voices that are known to work with edge-tts (free, needs an internet connection).
VOICES = [
    ("en-IN-NeerjaNeural", "English (India), female"),
    ("en-IN-PrabhatNeural", "English (India), male"),
    ("hi-IN-SwaraNeural", "Hindi, female"),
    ("hi-IN-MadhurNeural", "Hindi, male"),
    ("en-US-AriaNeural", "English (US), female"),
    ("en-US-GuyNeural", "English (US), male"),
    ("en-US-JennyNeural", "English (US), female, friendly"),
    ("en-GB-SoniaNeural", "English (UK), female"),
    ("en-GB-RyanNeural", "English (UK), male"),
]
RATES = {"-10%": "Slower", "+0%": "Normal", "+10%": "A bit faster", "+20%": "Faster", "+30%": "Fastest"}


def configure(models_dir):
    global _models_dir
    _models_dir = str(models_dir)


def whisper_available():
    return importlib.util.find_spec("faster_whisper") is not None


def tts_available():
    return importlib.util.find_spec("edge_tts") is not None


def model_loaded():
    return _model is not None


def load_model():
    global _model
    with _model_lock:
        if _model is None:
            try:
                from faster_whisper import WhisperModel
            except ImportError:
                raise RuntimeError("Captions need faster-whisper. Run: pip install faster-whisper")
            size = os.environ.get("CLIPPER_WHISPER_MODEL", "small")
            # More threads than faster-whisper's own small default, but capped: some CPUs are unstable
            # with the speech library at very high thread counts, especially on long files. Override with
            # CLIPPER_WHISPER_THREADS if your PC handles more (or needs fewer).
            threads = int(os.environ.get("CLIPPER_WHISPER_THREADS", min(6, max(1, (os.cpu_count() or 4) - 1))))
            try:
                _model = WhisperModel(size, device="cpu", compute_type="int8", cpu_threads=threads,
                                      download_root=_models_dir)
            except Exception as exc:
                raise RuntimeError(f"Couldn't load the speech model ({size}). The first run needs an "
                                   f"internet connection to download it. Details: {exc}")
        return _model


def _batched_pipeline():
    """A faster, multi-core way to transcribe, when the installed faster-whisper has it. Tried once;
    if it isn't available or errors in any way, every future transcription just uses the normal, slower
    path instead (nothing breaks either way)."""
    global _batched
    if _batched is None:
        try:
            from faster_whisper import BatchedInferencePipeline
            _batched = BatchedInferencePipeline(model=load_model())
        except Exception:
            _batched = False
    return _batched or None


def transcribe(audio_path, language, duration, on_progress=None):
    """Return {"language": "en", "words": [...]} for an audio file."""
    model = load_model()
    kwargs = dict(language=None if language in (None, "", "auto") else language,
                 word_timestamps=True, vad_filter=True, beam_size=2)
    segments = info = None
    pipeline = _batched_pipeline()
    if pipeline:
        try:
            batch = max(1, int(os.environ.get("CLIPPER_WHISPER_BATCH", 8)))
            fast_segments, info = pipeline.transcribe(str(audio_path), batch_size=batch, **kwargs)
            segments = list(fast_segments)          # do the real work now, inside this try
        except Exception:
            segments = None                         # something about this file or version tripped it up
    if segments is None:
        segments, info = model.transcribe(str(audio_path), **kwargs)
    words = []
    for seg in segments:
        for w in seg.words or []:
            text = w.word.strip()
            if text:
                words.append({"w": text, "s": round(w.start, 3), "e": round(w.end, 3)})
        if on_progress and duration:
            on_progress(min(seg.end / duration, 1.0))
    return {"language": info.language, "words": words}


# -------------------------------------------------------------- voiceover ---

async def _synthesize(text, voice, rate, out_path):
    import edge_tts
    communicate = edge_tts.Communicate(text, voice, rate=rate, boundary="WordBoundary")
    bounds = []
    with open(out_path, "wb") as f:
        async for chunk in communicate.stream():
            if chunk["type"] == "audio":
                f.write(chunk["data"])
            elif chunk["type"] in ("WordBoundary", "SentenceBoundary"):
                start = chunk["offset"] / 1e7          # offsets come in 100-nanosecond units
                bounds.append({"text": chunk["text"], "s": start, "e": start + chunk["duration"] / 1e7})
    return bounds


def synthesize(text, voice, rate, out_path):
    """Write the voiceover to out_path (mp3) and return the timing events the service sent."""
    try:
        import edge_tts  # noqa: F401
    except ImportError:
        raise RuntimeError("Voiceovers need edge-tts. Run: pip install edge-tts")
    try:
        return asyncio.run(_synthesize(text, voice, rate, out_path))
    except Exception as exc:
        raise RuntimeError("Couldn't create the voiceover. The voice service needs an internet "
                           f"connection. Details: {exc}")


def _spread(tokens, start, end):
    """Give each token a share of [start, end] in proportion to its length."""
    weights = [len(t) + 1 for t in tokens]
    total = sum(weights)
    out, t = [], start
    for token, weight in zip(tokens, weights):
        share = (end - start) * weight / total
        out.append({"w": token, "s": t, "e": t + share})
        t += share
    return out


def words_from_boundaries(bounds, script, total):
    """Turn the service's timing events into words. Returns None if it sent none."""
    if not bounds:
        return None
    words = []
    for b in bounds:
        tokens = b["text"].split()
        if len(tokens) == 1:
            words.append({"w": tokens[0], "s": b["s"], "e": b["e"]})
        elif tokens:
            words.extend(_spread(tokens, b["s"], b["e"]))   # sentence-level timing only
    script_tokens = script.split()
    if len(script_tokens) == len(words):                    # bring back the script's punctuation
        for word, token in zip(words, script_tokens):
            word["w"] = token
    return words


def estimate_words(script, total):
    """Last resort when there is no timing at all: spread the words over the audio."""
    tokens = script.split()
    return _spread(tokens, 0.0, total) if tokens else []
