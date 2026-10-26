"""Captions and ffmpeg arguments for vertical 9:16 videos.

Words look like {"w": "hello", "s": 1.2, "e": 1.6} with times in seconds.
"""

WIDTH, HEIGHT = 1080, 1920
FONT_NAME = "Poppins ExtraBold"   # bundled in ./fonts, supports Latin and Devanagari
SENTENCE_END = ".?!।…"
HIGHLIGHT = "&H0000D4FF&"         # warm yellow (ASS colours are written blue-green-red)
WHITE = "&H00FFFFFF&"

# Caption colours people can pick. "default" keeps the original look: white text, and for word-by-word captions
# a warm yellow highlight on the word being spoken. For word-by-word captions the chosen colour is the highlight;
# for the other styles it is the colour of the text.
COLORS = {"default": None, "white": "FFFFFF", "yellow": "FFD400", "orange": "FF8A00", "red": "FF3B3B",
          "pink": "FF4FA3", "purple": "B45CFF", "blue": "3D8BFF", "cyan": "22E5FF", "green": "39E75F"}


def ass_color(rgb):
    """'FFD400' (red, green, blue) as an ASS colour, which is written blue-green-red."""
    return f"&H00{rgb[4:6]}{rgb[2:4]}{rgb[0:2]}&"

ENCODE = ["-c:v", "libx264", "-preset", "veryfast", "-crf", "21", "-pix_fmt", "yuv420p",
          "-c:a", "aac", "-b:a", "160k", "-movflags", "+faststart"]


def ass_time(t):
    cs = int(round(max(0.0, t) * 100))
    h, rest = divmod(cs, 360000)
    m, rest = divmod(rest, 6000)
    s, cs = divmod(rest, 100)
    return f"{h}:{m:02d}:{s:02d}.{cs:02d}"


def clean(text):
    return text.replace("{", "(").replace("}", ")").replace("\\", "").replace("\n", " ").strip()


def normalize(words):
    """Sort words, drop empty ones, and make sure every word has a positive length."""
    out = []
    for w in sorted(words, key=lambda x: x["s"]):
        text = clean(w["w"])
        if not text:
            continue
        start = max(0.0, float(w["s"]))
        end = max(float(w["e"]), start + 0.05)
        out.append({"w": text, "s": start, "e": end})
    return out


def group_words(words, max_words=3, max_chars=15, max_gap=0.7):
    """Split words into short on-screen lines, breaking at pauses and sentence ends."""
    groups, current = [], []
    for w in words:
        if current:
            chars = sum(len(x["w"]) for x in current) + len(current) + len(w["w"])
            last = current[-1]["w"][-1:]
            if (len(current) >= max_words or chars > max_chars or w["s"] - current[-1]["e"] > max_gap
                    or last in SENTENCE_END or (last in ",;:" and len(current) >= 2)):
                groups.append(current)
                current = []
        current.append(w)
    if current:
        groups.append(current)
    return groups


def _dialogue(start, end, text, style_name="Default"):
    return f"Dialogue: 0,{ass_time(start)},{ass_time(end)},{style_name},,0,0,0,,{text}"


def wrap_hook(text, width):
    """Split a hook line onto a few centred lines so it never runs off a 9:16 or 16:9 frame."""
    chars_per_line = 17 if width <= 1080 else 32
    out, line = [], []
    for word in text.split():
        line.append(word)
        if len(" ".join(line)) >= chars_per_line:
            out.append(" ".join(line)); line = []
    if line:
        out.append(" ".join(line))
    return "\\N".join(out[:3]) or text


def build_ass(words, style="pop", position="lower", size=None, color="default", hook=None, duration=None):
    """Return the text of an .ass subtitle file. Vertical 1080x1920 unless size=(width, height) is given,
    for example (1280, 720) for a landscape video. `style` can be "off" to skip the word-by-word captions
    entirely (used when only a hook banner is wanted). `hook` is a short line of text pinned to the top of
    the frame for the whole clip; `duration` is how long to show it for (seconds), needed whenever `words`
    is empty (captions off) since there are then no word timings to measure the clip's length from."""
    width, height = size or (WIDTH, HEIGHT)
    words = normalize(words)
    if size:
        box_font, pop_font, box_pad, outline, shadow, side, low = 46, 50, 12, 5, 2, 60, 48
        groups = group_words(words, max_words=6, max_chars=32)
    else:
        box_font, pop_font, box_pad, outline, shadow, side, low = 112, 118, 26, 12, 4, 70, 430
        groups = group_words(words)
    alignment, margin_v = (5, 0) if position == "center" else (2, low)
    hook_font = int(height * (0.032 if size else 0.038))
    hook_style = (f"Style: Hook,{FONT_NAME},{hook_font},{WHITE},{WHITE},&H00000000,&HB4000000,"
                 f"0,0,0,0,100,100,0,0,3,{max(6, box_pad // 2)},0,8,{side},{side},{int(height * 0.035)},1")
    hook_events = []
    if hook:
        end = duration if duration is not None else (words[-1]["e"] + 0.3 if words else 5.0)
        hook_events.append(_dialogue(0.0, end, wrap_hook(hook.strip(), width), style_name="Hook"))
    if style == "off":
        header = "\n".join([
            "[Script Info]", "ScriptType: v4.00+", f"PlayResX: {width}", f"PlayResY: {height}",
            "WrapStyle: 0", "ScaledBorderAndShadow: yes", "",
            "[V4+ Styles]",
            "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, "
            "Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, "
            "Shadow, Alignment, MarginL, MarginR, MarginV, Encoding",
            hook_style, "",
            "[Events]",
            "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text",
        ])
        return header + "\n" + "\n".join(hook_events) + "\n"
    chosen = ass_color(COLORS[color]) if COLORS.get(color) else None
    if style == "pop":                       # word-by-word: white text, the spoken word in the chosen colour
        text_color, highlight = WHITE, chosen or HIGHLIGHT
    else:                                    # simple and box: all the text in the chosen colour
        text_color, highlight = chosen or WHITE, HIGHLIGHT
    if style == "box":
        style_line = (f"Style: Default,{FONT_NAME},{box_font},{text_color},{text_color},&HB4000000,&H00000000,"
                      f"0,0,0,0,100,100,0,0,3,{box_pad},0,{alignment},{side},{side},{margin_v},1")
    else:
        style_line = (f"Style: Default,{FONT_NAME},{pop_font},{text_color},{text_color},&H00000000,&H80000000,"
                      f"0,0,0,0,100,100,0,0,1,{outline},{shadow},{alignment},{side},{side},{margin_v},1")
    header = "\n".join([
        "[Script Info]",
        "ScriptType: v4.00+",
        f"PlayResX: {width}",
        f"PlayResY: {height}",
        "WrapStyle: 0",
        "ScaledBorderAndShadow: yes",
        "",
        "[V4+ Styles]",
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, "
        "Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, "
        "Shadow, Alignment, MarginL, MarginR, MarginV, Encoding",
        style_line,
        *([hook_style] if hook else []),
        "",
        "[Events]",
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text",
    ])

    events = []
    for gi, group in enumerate(groups):
        group_end = group[-1]["e"] + 0.15
        if gi + 1 < len(groups):
            group_end = min(group_end, groups[gi + 1][0]["s"])
        group_end = max(group_end, group[0]["s"] + 0.05)
        if style == "pop":
            for i, word in enumerate(group):
                start = word["s"]
                end = group[i + 1]["s"] if i + 1 < len(group) else group_end
                end = max(end, start + 0.05)
                parts = []
                for j, other in enumerate(group):
                    text = other["w"].upper()
                    if i == j:
                        text = (f"{{\\c{highlight}\\fscx112\\fscy112}}{text}"
                                f"{{\\c{WHITE}\\fscx100\\fscy100}}")
                    parts.append(text)
                events.append(_dialogue(start, end, " ".join(parts)))
        else:
            events.append(_dialogue(group[0]["s"], group_end, " ".join(w["w"] for w in group)))
    return header + "\n" + "\n".join(events + hook_events) + "\n"


# ---------------------------------------------------------------- layouts ---
# ffmpeg runs with the job's temp folder as its working directory, so the captions file and
# fonts folder are referenced by short relative names. That avoids Windows path escaping problems.

_LAYOUTS = {
    "fill": (f"[0:v]scale={WIDTH}:{HEIGHT}:force_original_aspect_ratio=increase,"
             f"crop={WIDTH}:{HEIGHT},setsar=1[v0]"),
    "blur": (f"[0:v]split=2[a][b];"
             f"[a]scale={WIDTH}:{HEIGHT}:force_original_aspect_ratio=increase,crop={WIDTH}:{HEIGHT},"
             f"scale=216:384,boxblur=6:2,scale={WIDTH}:{HEIGHT},setsar=1[bg];"
             f"[b]scale={WIDTH}:{HEIGHT}:force_original_aspect_ratio=decrease,setsar=1[fg];"
             f"[bg][fg]overlay=(W-w)/2:(H-h)/2[v0]"),
    "split": (f"[0:v]scale={WIDTH}:{HEIGHT // 2}:force_original_aspect_ratio=increase,"
              f"crop={WIDTH}:{HEIGHT // 2},setsar=1[t];"
              f"[1:v]scale={WIDTH}:{HEIGHT // 2}:force_original_aspect_ratio=increase,"
              f"crop={WIDTH}:{HEIGHT // 2},setsar=1[b];"
              f"[t][b]vstack=inputs=2[v0]"),
}


def _finish(captions_file):
    if captions_file:
        return f"[v0]ass={captions_file}:fontsdir=fonts[v]"
    return "[v0]null[v]"


def short_args(source, start, length, layout, bg=None, bg_start=0.0, captions_file=None, out="short.mp4"):
    inputs = ["-ss", f"{start:.3f}", "-i", str(source)]
    if layout == "split":
        inputs += ["-stream_loop", "-1", "-ss", f"{bg_start:.3f}", "-i", str(bg)]
    graph = _LAYOUTS[layout] + ";" + _finish(captions_file)
    return ["-y", *inputs, "-t", f"{length:.3f}", "-filter_complex", graph,
            "-map", "[v]", "-map", "0:a?", *ENCODE, "-r", "30", str(out)]


def script_args(audio, duration, bg=None, bg_start=0.0, color="#101320", captions_file=None, out="script.mp4"):
    """Voiceover video: a looping background video (or a solid colour) with the narration on top."""
    if bg:
        inputs = ["-stream_loop", "-1", "-ss", f"{bg_start:.3f}", "-i", str(bg)]
    else:
        inputs = ["-f", "lavfi", "-i", f"color=c=0x{color.lstrip('#')}:s={WIDTH}x{HEIGHT}:r=30"]
    inputs += ["-i", str(audio)]
    graph = ("[0:v]scale={w}:{h}:force_original_aspect_ratio=increase,crop={w}:{h},setsar=1,fps=30[v0];"
             .format(w=WIDTH, h=HEIGHT)) + _finish(captions_file)
    return ["-y", *inputs, "-t", f"{duration:.3f}", "-filter_complex", graph,
            "-map", "[v]", "-map", "1:a", *ENCODE, str(out)]
