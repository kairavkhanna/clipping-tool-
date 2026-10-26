"""AI helpers: find the best moments in a transcript and write short voiceover scripts.

Works with either a Google Gemini key (free tier available, get one at aistudio.google.com/app/apikey)
or an Anthropic key (pay as you go). The service is chosen from the key itself: keys starting with
"sk-" go to Anthropic, anything else (Gemini keys start with "AIza") goes to Gemini.
Both use plain HTTPS, so there is nothing extra to install.
"""
import json
import os
import re
import time
import urllib.error
import urllib.request

API_URL = os.environ.get("CLIPPER_AI_URL", "https://api.anthropic.com/v1/messages")
MODEL = os.environ.get("CLIPPER_AI_MODEL", "claude-sonnet-5")

# Gemini models are tried in this order. If one is retired (404) or has no quota (429), the next is tried,
# and the one that works is remembered. Override with CLIPPER_GEMINI_MODEL="model-a,model-b".
GEMINI_URL = os.environ.get("CLIPPER_GEMINI_URL",
                            "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent")
GEMINI_MODELS = [m.strip() for m in os.environ.get(
    "CLIPPER_GEMINI_MODEL", "gemini-2.5-flash,gemini-3.5-flash,gemini-3.8-flash").split(",") if m.strip()]
_gemini_working = None

# When the AI service is busy (HTTP 429, 500, 502, 503, 504, 529) Clipper waits and tries again this many times,
# waiting this many seconds before each new attempt.
BUSY_CODES = (429, 500, 502, 503, 504, 529)
RETRY_WAITS = (5, 15, 30, 60)
BUSY_MESSAGE = ("The AI service is busy or limiting requests right now (high demand, or a free tier limit). "
                "Clipper tried several times. Wait a few minutes and try again.")

MAX_TRANSCRIPT_CHARS = 350_000

LANGUAGES = {
    "english": "English",
    "hindi": "Hindi, written in Devanagari script",
    "hinglish": "Hinglish: Hindi written in Roman letters, the way people text",
}


def ask(api_key, system, user, max_tokens=4096, timeout=180):
    """Send one prompt and return the reply text. Uses Anthropic for "sk-" keys, Gemini otherwise."""
    if api_key.startswith("sk-"):
        return ask_claude(api_key, system, user, max_tokens, timeout)
    return ask_gemini(api_key, system, user, max_tokens, timeout)


def _error_detail(err):
    body = err.read().decode("utf-8", "replace")
    try:
        return json.loads(body)["error"]["message"]
    except Exception:
        return body[:200]


def ask_claude(api_key, system, user, max_tokens=4096, timeout=180):
    payload = {"model": MODEL, "max_tokens": max_tokens, "system": system,
               "messages": [{"role": "user", "content": user}]}
    body = json.dumps(payload).encode()
    for attempt in range(len(RETRY_WAITS) + 1):
        request = urllib.request.Request(
            API_URL, data=body, method="POST",
            headers={"content-type": "application/json", "x-api-key": api_key,
                     "anthropic-version": "2023-06-01"})
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                data = json.loads(response.read())
        except urllib.error.HTTPError as err:
            detail = _error_detail(err)
            if err.code == 401:
                raise RuntimeError("The API key was rejected. Check the key in AI settings.")
            if err.code in BUSY_CODES:
                if attempt < len(RETRY_WAITS):
                    time.sleep(RETRY_WAITS[attempt])
                    continue
                raise RuntimeError(BUSY_MESSAGE)
            raise RuntimeError(f"The AI service returned an error: {detail}")
        except (urllib.error.URLError, TimeoutError):
            raise RuntimeError("Couldn't reach the AI service. Check your internet connection.")
        return "".join(b.get("text", "") for b in data.get("content", []) if b.get("type") == "text")


def ask_gemini(api_key, system, user, max_tokens=4096, timeout=180):
    global _gemini_working
    payload = {
        "systemInstruction": {"parts": [{"text": system}]},
        "contents": [{"role": "user", "parts": [{"text": user}]}],
        # Gemini's internal "thinking" counts against this limit too, so leave plenty of room.
        "generationConfig": {"maxOutputTokens": min(max_tokens * 4, 32000)},
    }
    body = json.dumps(payload).encode()
    last_error = None
    for attempt in range(len(RETRY_WAITS) + 1):
        order = ([_gemini_working] if _gemini_working else []) + [m for m in GEMINI_MODELS if m != _gemini_working]
        busy = False
        for model in order:
            request = urllib.request.Request(
                GEMINI_URL.format(model=model), data=body, method="POST",
                headers={"content-type": "application/json", "x-goog-api-key": api_key})
            try:
                with urllib.request.urlopen(request, timeout=timeout) as response:
                    data = json.loads(response.read())
            except urllib.error.HTTPError as err:
                detail = _error_detail(err)
                if err.code in (401, 403) or (err.code == 400 and "api key" in detail.lower()):
                    raise RuntimeError("The API key was rejected. Check the key in AI settings.")
                if err.code == 404:      # this model name was retired or isn't available: try the next one
                    last_error = RuntimeError(f"The AI model {model} isn't available. {detail}")
                    continue
                if err.code in BUSY_CODES:   # busy or out of free quota: try the next model, then wait and retry
                    busy = True
                    last_error = RuntimeError(BUSY_MESSAGE)
                    continue
                raise RuntimeError(f"The AI service returned an error: {detail}")
            except (urllib.error.URLError, TimeoutError):
                raise RuntimeError("Couldn't reach the AI service. Check your internet connection.")

            candidates = data.get("candidates") or []
            if not candidates:
                reason = (data.get("promptFeedback") or {}).get("blockReason")
                raise RuntimeError("The AI declined to answer" + (f" ({reason})" if reason else "")
                                   + ". Try again or use a different video or topic.")
            parts = (candidates[0].get("content") or {}).get("parts") or []
            text = "".join(p.get("text", "") for p in parts if not p.get("thought"))
            if not text.strip():
                raise RuntimeError("The AI returned an empty answer. Try again.")
            _gemini_working = model
            return text
        if not busy:
            break                        # nothing was busy (only unavailable models): waiting won't help
        if attempt < len(RETRY_WAITS):
            time.sleep(RETRY_WAITS[attempt])
    raise last_error or RuntimeError("No Gemini model is available for this key.")


def transcript_lines(words):
    """Group words into lines of roughly 8 to 15 seconds, each starting with its time in seconds."""
    lines, current, line_start = [], [], 0.0
    for w in words:
        if current:
            elapsed = w["s"] - line_start
            ends_sentence = current[-1]["w"][-1:] in ".?!।"
            if (elapsed >= 8 and ends_sentence) or elapsed >= 15:
                lines.append(f"[{int(line_start)}] " + " ".join(x["w"] for x in current))
                current = []
        if not current:
            line_start = w["s"]
        current.append(w)
    if current:
        lines.append(f"[{int(line_start)}] " + " ".join(x["w"] for x in current))
    return lines


def pick_moments(api_key, words, duration, count=5, min_len=30, max_len=60):
    if not words:
        raise RuntimeError("No speech was found in this video, so there is nothing for the AI to read.")
    transcript = "\n".join(transcript_lines(words))
    if len(transcript) > MAX_TRANSCRIPT_CHARS:
        raise RuntimeError("This video has too much speech for AI analysis in one go. "
                           "Try a shorter video or clip it into parts first.")
    system = ("You are an expert editor of viral short-form video for TikTok, Reels and YouTube Shorts. "
              "You read transcripts and choose the moments that work as standalone clips. "
              "You answer with JSON only.")
    user = f"""Video length: {duration:.0f} seconds. The transcript has a timestamp in seconds at the start of each line.

Pick up to {count} moments that would work as standalone short clips.
Rules:
- Each clip must be between {min_len} and {max_len} seconds long.
- Start on a strong hook: a bold claim, a question, a surprising fact, or the start of a story. Do not start mid-sentence.
- End right after a payoff or a complete thought, at the end of a sentence.
- A clip must make sense without the rest of the video.
- Clips must not overlap.
- Use only times that appear in the transcript. Start at a line's timestamp or later, and end at the end of a line.
- Order the results from strongest to weakest.
- If fewer than {count} moments are good, return fewer.

Return only a JSON array with no other text. Each item has: "start" (seconds, a number), "end" (seconds, a number), "title" (at most 8 words, in the language of the transcript), "reason" (one sentence on why it would hold attention), "score" (an integer from 1 to 10).

Transcript:
{transcript}"""
    reply = ask(api_key, system, user, max_tokens=3000)
    return parse_moments(reply, words, duration, count, min_len, max_len)


def parse_moments(reply, words, duration, count, min_len, max_len):
    match = re.search(r"\[.*\]", reply, re.S)
    if not match:
        raise RuntimeError("The AI's reply wasn't in the expected format. Try again.")
    try:
        items = json.loads(match.group(0))
    except json.JSONDecodeError:
        raise RuntimeError("The AI's reply wasn't in the expected format. Try again.")

    starts = [w["s"] for w in words]
    ends = [w["e"] for w in words]
    moments = []
    for item in items if isinstance(items, list) else []:
        try:
            start, end = float(item["start"]), float(item["end"])
            title = str(item.get("title", "")).strip() or "Untitled moment"
            reason = str(item.get("reason", "")).strip()
            score = max(1, min(10, int(item.get("score", 5))))
        except (KeyError, TypeError, ValueError):
            continue
        start = min(starts, key=lambda x: abs(x - start))   # snap to real word boundaries
        end = min(ends, key=lambda x: abs(x - end))
        start = round(max(0.0, start - 0.1), 1)
        end = round(min(duration, end + 0.25), 1)
        length = end - start
        if length < max(5.0, min_len * 0.6) or length > max_len * 1.5:
            continue
        if any(start < m["end"] and end > m["start"] for m in moments):   # no overlaps
            continue
        moments.append({"start": start, "end": end, "title": title[:90], "reason": reason[:300], "score": score})
    return moments[:count]


def write_script(api_key, topic, seconds=45, language="english"):
    target_words = int(seconds * 2.6)
    system = ("You write voiceover scripts for short vertical videos. "
              "You output only the words the narrator will say.")
    user = f"""Write a voiceover script of about {target_words} words (about {seconds} seconds when read aloud) on this topic: {topic}

Language: {LANGUAGES.get(language, LANGUAGES['english'])}

Rules:
- Open with a hook in the first sentence.
- Use short, punchy sentences in a conversational voice.
- No headings, stage directions, emojis, hashtags or sound effects.
- End with a strong closing line.

Return only the script text."""
    return ask(api_key, system, user, max_tokens=1500).strip().strip('"')


def generate_topics(api_key, niche, count=25, language="english", avoid=()):
    """Return a list of distinct topic ideas for short videos in a niche."""
    system = "You come up with topic ideas for short vertical videos. You answer with JSON only."
    avoid_text = ""
    if avoid:
        listed = "\n".join("- " + a[:120] for a in list(avoid)[:150])
        avoid_text = f"\nDo not repeat or closely copy any of these existing topics:\n{listed}\n"
    user = f"""Give me {count} distinct topic ideas for short vertical videos in this niche: {niche}

Language: {LANGUAGES.get(language, LANGUAGES['english'])}
{avoid_text}
Rules:
- Each topic is one line, under 100 characters, specific and interesting enough to hook a viewer.
- Cover a wide range of angles. No two topics should be about the same thing.
- No numbering and no quotation marks.

Return only a JSON array of strings."""
    reply = ask(api_key, system, user, max_tokens=4000)
    match = re.search(r"\[.*\]", reply, re.S)
    if not match:
        raise RuntimeError("The AI's reply wasn't in the expected format. Try again.")
    try:
        items = json.loads(match.group(0))
    except json.JSONDecodeError:
        raise RuntimeError("The AI's reply wasn't in the expected format. Try again.")
    seen, topics = set(a.lower() for a in avoid), []
    for item in items if isinstance(items, list) else []:
        text = " ".join(str(item).split()).strip(' "\'')
        if text and text.lower() not in seen:
            seen.add(text.lower())
            topics.append(text[:200])
    return topics[:count]


# ------------------------------------------------------------ summary videos ---

WORDS_PER_SECOND = 2.5     # how fast a narrator speaks at normal speed


def _json_list(reply):
    match = re.search(r"\[.*\]", reply, re.S)
    if not match:
        raise RuntimeError("The AI's reply wasn't in the expected format. Try again.")
    try:
        items = json.loads(match.group(0))
    except json.JSONDecodeError:
        raise RuntimeError("The AI's reply wasn't in the expected format. Try again.")
    return items if isinstance(items, list) else []


def _transcript_for(words):
    if not words:
        raise RuntimeError("No speech was found in this video, so there is nothing for the AI to read.")
    transcript = "\n".join(transcript_lines(words))
    if len(transcript) > MAX_TRANSCRIPT_CHARS:
        raise RuntimeError("This video has too much speech for the AI to read in one go. "
                           "Clip it into parts and summarise each part.")
    return transcript


def plan_recap(api_key, words, duration, minutes, language="english"):
    """Write the narration for an 'explained' recap.

    Returns scenes in story order: [{"start": second of the source where the picture begins, "text": narration}].
    """
    transcript = _transcript_for(words)
    total_words = int(minutes * 60 * WORDS_PER_SECOND)
    scenes = max(6, round(total_words / 34))
    system = ("You are a skilled video essayist who makes 'explained' recap videos, like 'the whole movie explained "
              "in 15 minutes'. You read a transcript and write the narration a narrator will speak over the video. "
              "You answer with JSON only.")
    user = f"""Source video length: {duration / 60:.0f} minutes. The transcript has a timestamp in seconds at the start of each line.

Write the narration for an "explained" recap of about {minutes} minutes: a narrator tells the whole story or content from beginning to end, in the order it happens, in this language: {LANGUAGES.get(language, LANGUAGES['english'])}.

Rules:
- Write about {scenes} scenes. Each scene is 25 to 45 words, about 10 to 18 seconds when read aloud. All the scenes together are about {total_words} words.
- Each scene has "start": the second in the source video where that scene's picture should begin. Choose a timestamp from the transcript that matches what the narration describes. Scenes should move forward through the video and cover it from beginning to end, skipping filler.
- Explain what happens and why it matters. Name the people or characters when they first appear. Connect scenes with cause and effect. Speak in the present tense.
- Do not read the transcript back and do not quote long dialogue. Retell it in your own words.
- Open with a hook and finish with a closing line about how it ends.
- The "text" must be plain spoken words: no timestamps, headings, stage directions or emojis.

Return only a JSON array with no other text. Each item has: "start" (seconds, a number) and "text" (the narration for the scene).

Transcript:
{transcript}"""
    reply = ask(api_key, system, user, max_tokens=16000, timeout=900)
    return parse_recap(reply, words, duration)


def parse_recap(reply, words, duration):
    starts = [w["s"] for w in words]
    scenes = []
    for item in _json_list(reply):
        try:
            start = float(item["start"])
            text = " ".join(str(item["text"]).split())
        except (KeyError, TypeError, ValueError):
            continue
        if len(text) < 8:
            continue
        start = min(starts, key=lambda x: abs(x - start))              # snap to a real spoken moment
        start = round(min(max(0.0, start - 0.1), max(0.0, duration - 5)), 1)
        scenes.append({"start": start, "text": text[:700]})
    if len(scenes) < 3:
        raise RuntimeError("The AI didn't write enough scenes. Try again.")
    return scenes


def plan_highlights(api_key, words, duration, minutes):
    """Choose the scenes to keep so a long video shrinks to about `minutes`. Returns [{"start", "end"}] in order."""
    transcript = _transcript_for(words)
    target = minutes * 60
    system = ("You are an expert film and video editor. You read a transcript and decide which scenes to keep "
              "when cutting a long video down to a short version that still tells the whole story. "
              "You answer with JSON only.")
    user = f"""Source video length: {duration / 60:.0f} minutes. The transcript has a timestamp in seconds at the start of each line.

Cut this down to about {minutes} minutes ({target} seconds) by choosing the scenes to keep.

Rules:
- The kept scenes together add up to about {target} seconds.
- Each scene is between 20 and 90 seconds long.
- Keep the scenes in the order they appear. Scenes must not overlap.
- Keep what matters: the setup, the key turning points, the best moments and the ending. Cut repetition, filler, small talk, long intros, ads and tangents.
- Start at the beginning of a sentence and end at the end of a sentence, so nothing is cut off mid-thought.
- Use only times that appear in the transcript: start at a line's timestamp or later, and end at the end of a line.

Return only a JSON array with no other text. Each item has: "start" (seconds, a number), "end" (seconds, a number) and "score" (an integer from 1 to 10 for how important the scene is).

Transcript:
{transcript}"""
    reply = ask(api_key, system, user, max_tokens=8000, timeout=900)
    return parse_highlights(reply, words, duration, target)


def parse_highlights(reply, words, duration, target):
    starts = [w["s"] for w in words]
    ends = [w["e"] for w in words]
    picks = []
    for item in _json_list(reply):
        try:
            start, end = float(item["start"]), float(item["end"])
            score = max(1, min(10, int(item.get("score", 5))))
        except (KeyError, TypeError, ValueError):
            continue
        start = round(max(0.0, min(starts, key=lambda x: abs(x - start)) - 0.1), 1)
        end = round(min(duration, min(ends, key=lambda x: abs(x - end)) + 0.25), 1)
        if end - start >= 6:
            picks.append({"start": start, "end": end, "score": score})
    picks.sort(key=lambda p: p["start"])
    kept, last_end = [], 0.0
    for p in picks:                                   # no overlaps: trim the start of a scene that runs into the last one
        start = max(p["start"], last_end)
        if p["end"] - start >= 6:
            kept.append({"start": round(start, 1), "end": p["end"], "score": p["score"]})
            last_end = p["end"]
    while len(kept) > 2 and sum(p["end"] - p["start"] for p in kept) > target * 1.12:
        kept.remove(min(kept, key=lambda p: (p["score"], p["end"] - p["start"])))   # drop the least important
    if len(kept) < 2:
        raise RuntimeError("The AI didn't choose enough scenes. Try again.")
    return [{"start": p["start"], "end": p["end"]} for p in kept]


# --------------------------------------------------------------- captions ---

def romanize_words(api_key, words):
    """Rewrite each spoken word as natural, colloquial Hinglish (Roman letters), keeping every word's
    timing untouched. Falls back to leaving the original words alone if the AI's answer doesn't line up
    one-for-one with what was sent (so captions are never lost or shifted out of time)."""
    if not words:
        return words
    texts = [w["w"] for w in words]
    system = ("You transliterate spoken words into natural, colloquial Hinglish: Hindi or other Indian-language "
              "words written in the Roman/English alphabet the way people actually type them in chat and captions "
              "(for example 'क्या' becomes 'kya', not a formal transliteration like 'kyaa' or 'kyā'; 'है' becomes "
              "'hai'). A word already in Roman letters (English, numbers, punctuation) is left exactly as it is. "
              "You answer with JSON only.")
    user = ("Here is a JSON array of words, in the order they were spoken, possibly mixing scripts. Return a JSON "
            "array of exactly the same length: one Hinglish spelling per word, in the same order, with nothing "
            "merged, split, added or removed, and no other text:\n" + json.dumps(texts, ensure_ascii=False))
    try:
        out = _json_list(ask(api_key, system, user, max_tokens=6000, timeout=180))
    except RuntimeError:
        return words
    if not isinstance(out, list) or len(out) != len(words):
        return words
    return [{"w": (str(o).strip() or w["w"]), "s": w["s"], "e": w["e"]} for o, w in zip(out, words)]


def write_hook(api_key, words, duration, language="auto"):
    """A short, punchy line of on-screen text for the top of a clip, written from what the clip actually
    says, meant to make someone stop scrolling and watch to the end. Falls back to a generic line if there
    is no speech to go on, or if the AI call fails for any reason."""
    text = " ".join(w["w"] for w in words).strip() if words else ""
    system = ("You write short, punchy hook lines: on-screen text pinned to the top of a vertical short or "
              "reel for its whole length, meant to make someone stop scrolling and keep watching to the end. "
              "A hook teases what is coming, raises a question, or states a bold claim from the clip, without "
              "giving the ending away. You answer with plain text only: the hook line itself, nothing else, "
              "no quotation marks, no emojis, no hashtags.")
    if text:
        user = (f"Here is what is said in a {duration:.0f}-second video clip:\n{text}\n\n"
                "Write one hook line, 4 to 12 words, in the same language as the clip, "
                "that would make someone want to watch the whole thing.")
    else:
        user = (f"This is a {duration:.0f}-second video clip with no speech in it. Write one short, generic, "
                "curiosity-driving hook line, 4 to 10 words, suitable as on-screen text for a short video.")
    try:
        reply = ask(api_key, system, user, max_tokens=200, timeout=60)
    except RuntimeError:
        return "Watch till the end..."
    line = reply.strip().strip('"\u201c\u201d\'').splitlines()[0].strip() if reply.strip() else ""
    return line[:140] or "Watch till the end..."
