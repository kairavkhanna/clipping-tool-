# Clipper

A local tool for making short videos. Add a video by uploading a file or pasting a YouTube link,
then clip it, turn it into a captioned vertical short, let AI find the best moments, or make a
video from a script with an AI voiceover. Everything is saved in the `workspace/` folder.

## Set up (once)

1. Install Python 3.9 or newer.
2. Install ffmpeg:
   - macOS: `brew install ffmpeg`
   - Windows: `winget install Gyan.FFmpeg`
   - Linux: `sudo apt install ffmpeg`
3. In this folder, install the Python packages:

   ```
   pip install -r requirements.txt
   ```

## Run

On Windows: unzip the folder (right-click, Extract All), then double-click `start.bat`.
It installs the packages on the first run, starts Clipper and opens your browser.
Don't open `index.html` directly. It only works while Clipper is running.

Or from a terminal:

```
python app.py
```

Open http://127.0.0.1:8765 in your browser. Press Ctrl+C in the terminal to stop.
To use another port: `PORT=9000 python app.py` (on Windows: `set PORT=9000` first).

## Clip a video

- **Add a video:** choose or drop a file, or paste a YouTube link and pick a quality.
  YouTube videos are downloaded first, so you can scrub them instantly.
- **Set the clip:** drag the blue handles, type times (like `1:23.5`), or use the keys
  `I` (start), `O` (end), `P` (play selection), `Space` (play or pause).
- **Formats:** *MP4, exact cut* re-encodes so the clip starts exactly where you set it.
  *Fast copy* is nearly instant but can only cut at keyframes, so it may start a few seconds early.
  *MP3* keeps only the audio.

## Vertical shorts with captions

Choose **Vertical short (9:16) with captions** as the format.

- **Layout:** fill the screen (crops the sides), fit on a blurred background, or split the screen
  with another video from your library (for example gameplay you own the rights to).
- **Captions:** word-by-word highlight, simple, or on a dark box, in the lower third or the middle.
  Speech is turned into text on your computer with Whisper. The first time, it downloads a
  speech model of a few hundred MB, so that first caption job is slower. Hindi and Devanagari
  text are supported.

## Find the best moments (AI)

1. Get an API key. A **Gemini** key is free to start: sign in at aistudio.google.com/app/apikey and click
   **Create API key** (free tier limits apply). Or use an **Anthropic** key from console.anthropic.com
   (pay as you go, separate from a Claude subscription). Either one works. Analyzing a video
   sends text, not video, so it uses very little.
2. Paste the key under **AI settings** in Clipper.
3. Open a video and click **Find moments**. Clipper transcribes it (once, then it's remembered),
   asks the AI for standalone clips, and lists them. Use **Preview**, **Use this range**, or
   **Make short** to render one straight away with your current short settings.

## Script to video

Open the **Script to video** tab. Type a script, or click **Write with AI**. Pick a voice, a
speed, a background (a solid colour or a video from your library) and caption style, then click
**Make video**. Voices come from a free online service, so this needs an internet connection.

## Hook text

A hook is a short line pinned to the top of a short for its whole length, to make people keep watching -
for example "Wait till the end..." or a line pulled from what's actually said in the clip.

- **Clip a video:** type your own hook under "Hook text", or click **Write with AI** to have it written from
  the clip you've selected (you can edit it before creating the clip). Works with or without captions.
- **Batch:** choose **Same hook on every clip**, or **Write a different hook for each clip with AI**, which
  writes one from each clip's own speech as it renders.

## Summary video (long video to a 5 to 30 minute recap)

Open a long video, find the **Summary video** box under "Find the best moments", pick a length and a style, then
click **Make summary video**. The result is a 16:9 (landscape) video with captions burned in.

- **Explained by a narrator:** the AI reads the whole transcript, writes the story scene by scene, and a voice tells it
  while the video's own pictures play. Choose the language, voice and speed. *Original sound* can stay on quietly.
- **Best scenes, original sound:** the AI chooses the most important scenes and joins them in order. The people in the
  video keep speaking, and the captions show what they say.

The first run transcribes the whole video, which is slow (a 2 hour video can take an hour or more on a normal PC).
The transcript is saved, so later summaries of the same video start straight away. Needs an internet connection for the
AI and, for the narrator style, the free voice service. The summary length is a target: the final video can be a bit
shorter or longer. The video must be at least a quarter longer than the summary you ask for. Only use videos you own
or have permission to reuse. Background music (below) works on summaries too.

## Making transcription faster

Clipper now uses more of your CPU cores for transcribing speech than before (used to be a small fixed number, capped at 6 for stability on long videos). If your
faster-whisper is a recent version, Clipper also tries a faster, multi-core "batched" mode automatically;
if that mode isn't available or has a problem, it quietly falls back to the normal mode, so nothing
breaks either way. To update to the latest faster-whisper: `pip install -U faster-whisper`, then restart
Clipper.

You can tune this by setting an environment variable before starting Clipper (`set NAME=value` in
PowerShell, then run `python app.py` in the same window):
- `CLIPPER_WHISPER_THREADS` &mdash; how many CPU cores to use (default: all but one)
- `CLIPPER_WHISPER_BATCH` &mdash; how many chunks of audio the batched mode processes at once (default 8;
  try 16 on a PC with a lot of RAM and many cores for a bit more speed)

## Publish to YouTube

Clipper can upload a finished clip straight to your own YouTube channel. This needs a free Client ID and
Client Secret from your own Google account, which takes about 5 minutes to set up once — see
`YouTube_SETUP.md` for the exact steps. Then, under **Publish to YouTube** on the left, paste them in and
click **Connect**, sign in with Google in the tab that opens, and you're done.

To upload, open **Your clips**, find a clip, and click **Upload to YouTube**. Set a title, description,
tags and privacy (Private, Unlisted or Public), then click **Upload**. Google's free quota allows around
6 uploads a day through this. Only you (and anyone else you added as a test user) can connect, which is
normal for a personal project like this one.

## Caption colours

Every form that has captions (shorts, script videos, batches and summary videos) has a **Caption colour** box next
to **Caption position**. Choose White, Yellow, Orange, Red, Pink, Purple, Blue, Cyan or Green. With *Word by word*
captions the colour is the highlight on the word being spoken and the other words stay white. With *Simple* and
*Dark box* captions, all the text takes the colour. *Default* keeps the original look.

## Background music

Add a music file to the library the same way as a video (use **Choose a file** and pick an MP3, WAV or M4A).
Then, wherever you make a short, a script video or a batch, open **Background music** and pick it.

- **Music volume:** Quiet, Low, Medium or Loud. It plays under the voice or speech, so Low is a good start.
- **Music plays:** *The whole video* (it loops if the track is short and fades out at the end), or
  *Only the first 1, 2, 3, 5 or 10 seconds*, which is handy for a short intro sound.

Use music you own or have permission to use. Music is not added to plain MP4, fast copy or MP3 clips.

## Batch: make 100 videos (or more) at once

Open the **Batch** tab. Batches run in the background one video at a time, so you can leave
Clipper running (overnight is fine). Progress is saved, so a batch survives a restart: click
**Resume**. A video that fails is retried, and a failure never stops the rest. Use **Retry failed**
to run the failed ones again, and **Download zip** to get everything in a batch as one file.

**Voiceover videos from scripts or topics**
- *Topics:* one per line. The AI writes a script for each one, then Clipper makes the video.
  Need ideas? Type a niche and click **Generate topics** to get up to 100 at a time.
- *Finished scripts:* separate them with a blank line.
- Choose a voice (or rotate through several), speed, background and caption style.
  Backgrounds can be a solid colour, one of your videos, or a random video from your library.

**Shorts from your videos**
- Tick the videos in your library and choose how many shorts each should give.
- *Evenly spaced parts* is free. *AI picks the best moments* needs your API key and transcribes
  each video first, which takes a while for long videos.
- Same layouts and captions as single shorts. For split screen, the bottom video can be random.

Examples: 10 long videos × 10 shorts each = 100 shorts, or 100 topics = 100 voiceover videos.
Expect roughly 30 to 60 seconds per video on a typical PC. Files stay in `workspace/clips`.
A batch can hold up to 300 videos.

If you publish these, vary the topics, voices and backgrounds. Platforms can treat large amounts
of near-identical content as spam.

## Put it on a server

To use Clipper from any browser at your own web address, follow `DEPLOY.md`.

## Notes

- Only download and clip videos you own or have permission to reuse. YouTube's terms
  restrict downloading, and the content belongs to its creators. Use backgrounds and music
  you have the rights to.
- If a YouTube download fails, update yt-dlp first: `pip install -U yt-dlp`.
- Files your browser can't play (for example some .mkv or HEVC files) get a small preview
  copy made automatically. Clips are always cut from your original file.
- The captions font (Poppins ExtraBold) is bundled in `fonts/` under the SIL Open Font License.
- On your own PC the server only accepts connections from your own computer (127.0.0.1).
