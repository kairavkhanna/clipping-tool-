"""YouTube helper for Clipper: connect a channel with Google OAuth and upload clips.

Uses only the Python standard library, so nothing extra needs to be installed.
app.py imports this as `youtube as yt_api` and calls:
    auth_url, exchange_code, refresh_access_token, channel_info, revoke,
    upload_video, PRIVACY_STATUSES, CATEGORIES

All errors are raised as RuntimeError with a message that is safe to show the user.
"""
import json
import mimetypes
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
REVOKE_URL = "https://oauth2.googleapis.com/revoke"
CHANNELS_URL = "https://www.googleapis.com/youtube/v3/channels"
UPLOAD_URL = "https://www.googleapis.com/upload/youtube/v3/videos"

SCOPES = (
    "https://www.googleapis.com/auth/youtube.upload "
    "https://www.googleapis.com/auth/youtube.readonly"
)

PRIVACY_STATUSES = ("private", "unlisted", "public")

# YouTube category ids -> names
CATEGORIES = {
    "1": "Film & Animation",
    "2": "Autos & Vehicles",
    "10": "Music",
    "15": "Pets & Animals",
    "17": "Sports",
    "19": "Travel & Events",
    "20": "Gaming",
    "22": "People & Blogs",
    "23": "Comedy",
    "24": "Entertainment",
    "25": "News & Politics",
    "26": "Howto & Style",
    "27": "Education",
    "28": "Science & Technology",
    "29": "Nonprofits & Activism",
}

CHUNK_SIZE = 8 * 1024 * 1024          # must be a multiple of 256 KiB
TIMEOUT = 60


def _error_text(exc):
    """Pull a readable message out of an HTTP error from Google."""
    try:
        body = json.loads(exc.read().decode("utf-8", "replace"))
    except Exception:
        return f"HTTP {exc.code}"
    err = body.get("error")
    if isinstance(err, dict):
        reason = err.get("message") or ""
        for item in err.get("errors", []):
            if item.get("reason") == "quotaExceeded":
                return "YouTube's daily upload quota is used up. Try again tomorrow."
            if item.get("reason") == "youtubeSignupRequired":
                return "That Google account has no YouTube channel."
        return reason or f"HTTP {exc.code}"
    if isinstance(err, str):
        return body.get("error_description") or err
    return f"HTTP {exc.code}"


def _request(url, data=None, headers=None, method=None, timeout=TIMEOUT):
    req = urllib.request.Request(url, data=data, headers=headers or {}, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
            return resp.status, dict(resp.headers), raw
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"YouTube error: {_error_text(exc)}") from None
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise RuntimeError(f"Couldn't reach Google: {exc}") from None


def _post_form(url, fields):
    data = urllib.parse.urlencode(fields).encode()
    _, _, raw = _request(url, data=data, method="POST",
                         headers={"Content-Type": "application/x-www-form-urlencoded"})
    return json.loads(raw.decode("utf-8"))


def auth_url(client_id, redirect_uri, state):
    """The Google page the user opens to approve access."""
    params = {
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": SCOPES,
        "access_type": "offline",     # gives us a refresh token
        "prompt": "consent",          # make sure the refresh token is returned every time
        "state": state,
        "include_granted_scopes": "true",
    }
    return f"{AUTH_URL}?{urllib.parse.urlencode(params)}"


def exchange_code(client_id, client_secret, redirect_uri, code):
    """Trade the code from Google for (refresh_token, access_token)."""
    tokens = _post_form(TOKEN_URL, {
        "code": code,
        "client_id": client_id,
        "client_secret": client_secret,
        "redirect_uri": redirect_uri,
        "grant_type": "authorization_code",
    })
    refresh = tokens.get("refresh_token")
    access = tokens.get("access_token")
    if not access:
        raise RuntimeError("Google didn't return an access token. Try Connect again.")
    if not refresh:
        raise RuntimeError("Google didn't return a refresh token. Remove Clipper from "
                           "https://myaccount.google.com/permissions and click Connect again.")
    return refresh, access


def refresh_access_token(client_id, client_secret, refresh_token):
    """A fresh short-lived access token."""
    tokens = _post_form(TOKEN_URL, {
        "client_id": client_id,
        "client_secret": client_secret,
        "refresh_token": refresh_token,
        "grant_type": "refresh_token",
    })
    access = tokens.get("access_token")
    if not access:
        raise RuntimeError("Couldn't refresh the YouTube sign-in. Disconnect and connect again.")
    return access


def channel_info(access_token):
    """Basic details of the signed-in user's channel: {'id', 'title', 'thumbnail'}."""
    query = urllib.parse.urlencode({"part": "snippet", "mine": "true"})
    _, _, raw = _request(f"{CHANNELS_URL}?{query}",
                         headers={"Authorization": f"Bearer {access_token}"})
    items = json.loads(raw.decode("utf-8")).get("items") or []
    if not items:
        raise RuntimeError("That Google account has no YouTube channel.")
    snippet = items[0].get("snippet", {})
    thumbs = snippet.get("thumbnails", {})
    thumb = (thumbs.get("default") or thumbs.get("medium") or {}).get("url", "")
    return {"id": items[0].get("id", ""), "title": snippet.get("title", "Your channel"),
            "thumbnail": thumb}


def revoke(token):
    """Tell Google to forget this connection. Never raises: disconnecting must always work."""
    try:
        _post_form(REVOKE_URL, {"token": token})
    except Exception:
        pass


def upload_video(access_token, path, title, description, tags, privacy, category,
                 made_for_kids=False, on_progress=None):
    """Upload a video file with YouTube's resumable upload. Returns the new video id."""
    path = Path(path)
    size = path.stat().st_size
    if size == 0:
        raise RuntimeError("That clip's file is empty.")
    mime = mimetypes.guess_type(path.name)[0] or "video/mp4"

    metadata = {
        "snippet": {
            "title": title[:100],
            "description": description[:5000],
            "tags": list(tags),
            "categoryId": str(category),
        },
        "status": {
            "privacyStatus": privacy,
            "selfDeclaredMadeForKids": bool(made_for_kids),
        },
    }
    start_headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json; charset=UTF-8",
        "X-Upload-Content-Length": str(size),
        "X-Upload-Content-Type": mime,
    }
    query = urllib.parse.urlencode({"uploadType": "resumable", "part": "snippet,status"})
    _, headers, _ = _request(f"{UPLOAD_URL}?{query}",
                             data=json.dumps(metadata).encode("utf-8"),
                             headers=start_headers, method="POST")
    session_url = next((v for k, v in headers.items() if k.lower() == "location"), None)
    if not session_url:
        raise RuntimeError("YouTube didn't start the upload. Try again.")

    sent = 0
    failures = 0
    with open(path, "rb") as handle:
        while True:
            handle.seek(sent)
            chunk = handle.read(CHUNK_SIZE)
            end = sent + len(chunk) - 1
            req = urllib.request.Request(
                session_url, data=chunk, method="PUT",
                headers={"Content-Length": str(len(chunk)),
                         "Content-Range": f"bytes {sent}-{end}/{size}"})
            try:
                with urllib.request.urlopen(req, timeout=300) as resp:
                    body = json.loads(resp.read().decode("utf-8"))
                    if on_progress:
                        on_progress(1.0)
                    return body["id"]
            except urllib.error.HTTPError as exc:
                if exc.code == 308:                       # chunk accepted, send the next one
                    failures = 0
                    rng = exc.headers.get("Range")
                    sent = int(rng.split("-")[-1]) + 1 if rng else sent + len(chunk)
                    if on_progress:
                        on_progress(min(sent / size, 0.99))
                    continue
                if exc.code >= 500 and failures < 5:
                    failures += 1
                    time.sleep(2 ** failures)
                    sent = _resume_position(session_url, size, sent)
                    continue
                raise RuntimeError(f"YouTube error: {_error_text(exc)}") from None
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                failures += 1
                if failures > 5:
                    raise RuntimeError(f"Upload interrupted: {exc}") from None
                time.sleep(2 ** failures)
                sent = _resume_position(session_url, size, sent)


def _resume_position(session_url, size, fallback):
    """Ask YouTube how many bytes it already has, so a dropped upload can continue."""
    req = urllib.request.Request(session_url, data=b"", method="PUT",
                                 headers={"Content-Length": "0",
                                          "Content-Range": f"bytes */{size}"})
    try:
        urllib.request.urlopen(req, timeout=TIMEOUT)
    except urllib.error.HTTPError as exc:
        rng = exc.headers.get("Range") if exc.code == 308 else None
        if exc.code == 308:
            return int(rng.split("-")[-1]) + 1 if rng else 0
    except Exception:
        pass
    return fallback
