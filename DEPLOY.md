# Put Clipper on a server

This runs Clipper at your own web address (like https://clip.yourdomain.com) with HTTPS and
a login, so you can use it from any browser without installing anything.

You need:
- A small Linux server (VPS) running Ubuntu 22.04 or 24.04, with 4 GB of RAM and 40 GB of
  disk. Any provider works. With 2 GB it still runs if you set `CLIPPER_WHISPER_MODEL=base`
  in `.env`, but captions are less accurate.
- A domain or subdomain you can point at the server, such as `clip.yourdomain.com`.

Regular shared hosting (cPanel) can't run this. It needs ffmpeg and a long-running Python
program, which shared hosting doesn't allow.

## 1. Point your domain at the server

In your domain's DNS settings (in cPanel that's **Zone Editor**), add an **A record**:
name `clip` (or whatever you like), value = the server's IP address. It can take a few
minutes to start working.

## 2. Log in to the server

From Windows PowerShell:

```
ssh root@YOUR_SERVER_IP
```

## 3. Install Docker (on the server)

```
curl -fsSL https://get.docker.com | sh
```

## 4. Copy Clipper to the server

On your PC, unzip `clipper.zip`. Then in PowerShell, from the folder that contains the
`clipper` folder:

```
scp -r clipper root@YOUR_SERVER_IP:~/
```

## 5. Set your domain and password (on the server)

```
cd ~/clipper
cp .env.example .env
nano .env
```

Set `DOMAIN` to your address (without https://), pick `CLIPPER_USER`, and set
`CLIPPER_PASSWORD` to a long random password. Save with Ctrl+O, Enter, then exit with Ctrl+X.

## 6. Open the firewall

```
ufw allow 22/tcp
ufw allow 80/tcp
ufw allow 443/tcp
ufw --force enable
```

## 7. Start it

```
docker compose up -d --build
```

The first build takes a few minutes. Then open `https://YOUR_DOMAIN` and sign in.
The HTTPS certificate is set up automatically. If the page doesn't load, check that the
DNS record from step 1 is working, then look at the logs (below).

## Everyday commands (run inside ~/clipper)

| What | Command |
|---|---|
| See logs | `docker compose logs -f clipper` |
| Restart | `docker compose restart` |
| Stop | `docker compose down` |
| Update yt-dlp (do this when YouTube downloads stop working) | `docker compose build --no-cache clipper && docker compose up -d` |
| See disk use | `df -h` |

## Good to know

- **First captions run:** the speech model (a few hundred MB) downloads once and is kept on the
  server. The first caption job is slower because of that.
- **AI features** need your own Gemini key (free tier available) or Anthropic key (pay as you go).
  Paste it in the app under AI settings, or put `GEMINI_API_KEY` or `ANTHROPIC_API_KEY` in `.env`.

- **Everyone shares one login and one library.** Anyone with the password can see and
  delete everything, and use the server's disk and bandwidth. Keep the password private.
- **Files are deleted automatically** after `RETENTION_HOURS` (48 by default) so the disk
  doesn't fill up. A batch of 100 videos takes up a few GB, so download each batch's zip before
  then, or set it to `0` to keep everything (and watch your disk space).
- **Batches keep running** in the background even if you close the browser, and they continue
  after a server restart once you click Resume.
- **Big uploads** are limited by `MAX_UPLOAD_MB` (2048 by default).
- **Only download and clip videos you own or have permission to reuse.**

## If YouTube blocks the server

YouTube often refuses downloads from data-center addresses with a "Sign in to confirm you're
not a bot" message. You can try giving Clipper a cookies file:

1. On your PC, log in to YouTube in a browser with a spare account, not your main one.
   Using an account's cookies for automated downloads can get that account restricted.
2. Export the cookies as `cookies.txt` with a browser extension such as "Get cookies.txt LOCALLY".
3. Copy it to the server: `scp cookies.txt root@YOUR_SERVER_IP:~/clipper/`
4. In `docker-compose.yml`, remove the `#` in front of the `./cookies.txt` line.
5. Run `docker compose up -d`.

This isn't guaranteed to work, and cookies expire, so you may need to repeat it. Uploaded
files are never affected by any of this.
