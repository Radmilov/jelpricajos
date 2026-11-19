# jelpricajos

A local web dashboard and background service that watches the
[`@ParlamentSrbija`](https://www.youtube.com/@ParlamentSrbija/live) YouTube live stream,
runs Serbian Cyrillic OCR on the speaker caption, and shows whether **Aleksandar Vučić** is
speaking. When he stops, it fires confetti and a synthesized fanfare.

```
yt-dlp (live HLS URL) ──► ffmpeg (1 frame / 1.5 s, 1280×720) ──► ROI crop ──► EasyOCR (rs_cyrillic)
      ▲  poll every 30 s                                                      │
      │                                         size filter + fuzzy match ◄───┘
      └──── OFFLINE ◄──── state machine (debounce) ──► SQLite ──► FastAPI ──► SSE ──► dashboard
```

## How detection works

The broadcast shows a persistent white caption at the bottom centre with two left-aligned
lines: the speaker's name, then their parliamentary group. Vučić's group is called
*"Александар Вучић – Србија побеђује"*. The app classifies a frame from that **structure**:

| What is on screen | State |
|---|---|
| A line that is *just* "Александар Вучић", as the **top** line of a caption, with a line directly below it | `VUCIC_SPEAKING` |
| His name only inside the group line ("… – Србија побеђује") | `PARTY_MEMBER_SPEAKING` |
| Neither | `OTHER_OR_NONE` |

It deliberately does **not** count matches. The large animated chyron repeats the caption, so a
party member's group line can be on screen twice. A real case with Ana Brnabić on 18 Nov 2026 shows
why: counting matches would have produced a false "Vučić speaking". A bare name with no caption
line next to it is ambiguous and marked *uncertain*. Uncertain frames, like occluded ones, never
change the state.

* **Break screen ("pauza"):** during breaks the broadcast shows a large blue oval with the Assembly
  building and a gold rim. Measured on the live stream, the oval is ~44 % blue (normal frames ≤ 4 %)
  and the rim ~40 % gold (≤ 5 %). After 3 consecutive such frames the site shows **"Pauza u toku"**
  with the break's running time. OCR is skipped during breaks.
* **Offline screen:** "Prenos uživo nije aktivan", a dictionary-style definition of *logoreja*, and
  a countdown to the next session. The channel doesn't schedule streams in advance, so set the
  time yourself:
  `sudo bash /opt/jelpricajos/deploy/next-session.sh "2026-11-19 10:00"` (Serbian time),
  or `… clear`. A stream scheduled on YouTube, if one ever appears, takes precedence.
* **ROI:** `x 0.00–0.82 W`, `y 0.80–1.00 H`, the bottom strip where the caption appears, calibrated
  against the live stream. The broadcast has placed the caption both at ~0.28 W and flush with the
  bottom-left corner, so the crop starts at the left edge. The group line sits at ~0.93–0.99 H. The top edge also covers the large animated
  chyron, which the occlusion check needs.
* **Size filter:** only boxes between **3.0 % and 6.8 % of H** count. On the real stream the
  caption lines measure 5.6–6.2 %. The large animated "embossed" chyron measures 7.2–8.5 % and is
  rejected on purpose.
* **Occlusion hold:** while that animated chyron covers the caption, frames are treated as
  inconclusive and can't change the state. Without this hold, a speech could be ended too early.
* **Fuzzy matching:** text is normalised (case, Latin look-alike glyphs, Serbian Latin to Cyrillic,
  `ђ`/`ћ` folding), then scored with RapidFuzz (threshold 0.80). The surname also has to be
  present, so "Александар Мартиновић" never counts.
* **Debounce:** it takes 2 consecutive frames to enter a speaking state and 3 non-Vučić frames
  to leave `VUCIC_SPEAKING`. A speech's end time is the last frame where he was seen.
* **Speech merging:** if he is detected again within 60 s (`SPEECH_MERGE_GAP_SEC`), the previous
  speech continues. This covers long camera cuts, stream reconnects and server restarts.

## Performance

Measured on the live stream:

| | Before optimization | Now |
|---|---|---|
| CPU | ~383 % (4 cores busy) | **~38 %** of one core on average |
| RAM | ~700 MB | ~675 MB (mostly PyTorch) |
| Download | 720p, ~2.5 Mbit/s | 480p, ~1.3 Mbit/s |

What changed:
* A tight crop around the caption.
* 480p frames every 2 s.
* PyTorch limited to 2 threads (`OCR_THREADS`).
* A cheap "did the caption change?" check. It compares white-pixel masks and reuses the last
  result, so full OCR runs on only about 30–40 % of frames, and it skips frames with no text at all.
  OCR is still forced at least every 20 s.

### Viewers

The video plays straight from YouTube in each viewer's browser, so it never passes through
this server. Per viewer the server only sends:

| | Size (gzip) |
|---|---|
| Page (once) | ~14 KB |
| `/api/v1/status` (polling, every 2 s) | ~0.4 KB |
| Full state (only on changes, or every 30 s) | ~0.7 KB |

That works out to about 2 kbit/s per viewer when polling.

Capacity:
* **Polling:** one worker handled about 1,000 requests per second (p95 25 ms), which is roughly
  2,000 polling viewers. Polling is what you get behind a quick tunnel.
* **SSE (named tunnel or reverse proxy):** viewers only receive changes plus a 15 s keep-alive.
  These are capped by `SSE_MAX_TOTAL` (5,000) and haven't been load-tested.
* **Shared IPs:** limits are per IP and generous (`RATE_LIMIT_PER_MIN` 1200, `SSE_MAX_PER_CLIENT`
  50), so viewers behind mobile CGNAT, offices or universities aren't blocked.
* **Debugging:** `?poll=1` forces polling mode.

It fits a 1-core / 1 GB VPS. On Oracle Cloud Always Free (Ampere ARM) it runs with plenty of headroom.
On datacenter IP addresses, YouTube may require cookies (`YTDLP_COOKIES_FILE`).

All thresholds are in `config.py`, and each one can be overridden with an environment variable
of the same name.

## Requirements

* Python **3.10+**
* **ffmpeg**. If it isn't on your `PATH`, the app uses the copy bundled with `imageio-ffmpeg`, so
  installing ffmpeg yourself is optional but recommended.
* About 2 GB of disk for PyTorch and the EasyOCR models. The models (~100 MB) download on first start.

## Setup and run

### Windows (PowerShell)

```powershell
winget install --id Gyan.FFmpeg -e        # optional (or: scoop install ffmpeg / choco install ffmpeg)
winget install --id DenoLand.Deno -e      # recommended: JS runtime yt-dlp uses for YouTube
cd path\to\jelpricajos
py -3.11 -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
python app.py
```

### macOS

```bash
brew install ffmpeg deno
cd path/to/jelpricajos
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python app.py
```

### Linux (Debian/Ubuntu)

```bash
sudo apt update && sudo apt install -y ffmpeg python3-venv
curl -fsSL https://deno.land/install.sh | sh     # recommended for yt-dlp
cd path/to/jelpricajos
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python app.py
```

Then open **http://127.0.0.1:8000**. To use a different port, set `PORT` first, e.g.
`$env:PORT=8765` on PowerShell or `PORT=8765 python app.py` on macOS/Linux.

The first start takes a little while because EasyOCR downloads its models. Until they're ready, the
Stream card shows `easyocr loading…`.

### NVIDIA GPU (optional, much faster OCR)

On CPU, EasyOCR takes about 1.5–3 s per frame. That's fine, because the grabber always processes the
newest frame and never builds up lag. With a CUDA GPU you can get it under 200 ms:

```bash
pip install --force-reinstall torch torchvision --index-url https://download.pytorch.org/whl/cu124
# then run with OCR_GPU=1  (PowerShell: $env:OCR_GPU=1; python app.py)
```

## Recommended setup: home worker + public relay

```
YouTube ──► home PC  MODE=worker   (yt-dlp + ffmpeg + OCR; outgoing HTTPS only)
                │  POST /api/ingest   Authorization: Bearer <RELAY_TOKEN>
                ▼
            VPS      MODE=relay    (website + API only, ~100 MB RAM, no OCR)
                ▲
     visitors ──┘
```

* **Nothing at home accepts connections.** There's no tunnel or open port on the home PC, so
  visitors have no path to it.
* **YouTube sees a home IP,** so there's no "confirm you're not a bot" and no cookies.
* **The relay is tiny** and holds no personal data. The cheapest VPS works, including Oracle's
  free AMD micro instance.
* **If the home PC is off, the site stays up.** It says "monitor offline" with the time of the
  last report, keeps the player, and stops auto-muting.
* **Admin actions work from the relay.** `next-session.sh` and `/api/simulate` on the relay are
  queued and handed to the worker in the reply to its next push, within about 5 s.
* **Security.** Pushes must carry the token, compared in constant time. The worker refuses to send
  it over plain `http://` except to localhost. Bodies are size-limited.
* **Error details** from the worker (paths, yt-dlp messages) are still hidden from the public.

Set it up:

1. **Relay on the VPS:**
   `.\deploy\upload.ps1 -Server root@<IP> -Key <key> -Role relay [-TunnelToken <TOKEN>]`.
   The installer skips PyTorch, ffmpeg and the OCR models, then prints a `RELAY_TOKEN`.
2. **Worker at home:**
   `.\deploy\install-worker.ps1 -RelayUrl https://<relay-url> -Token <RELAY_TOKEN>`.
   This writes `local.env` (git-ignored, readable only by you) and registers a Task Scheduler
   task that starts the worker at logon and restarts it if it crashes. Remove it with
   `-Uninstall`.
3. **Stop the quick tunnel on the home PC.** It's no longer needed. The local dashboard stays on
   http://127.0.0.1:8000, and `/api/v1/health` there shows the last successful push.

Modes (`MODE` in the environment or `local.env`): `standalone` (default, everything on one
machine), `worker`, `relay`. Related settings: `RELAY_URL`, `RELAY_TOKEN`,
`RELAY_HEARTBEAT_SEC` (5), `RELAY_FRAME_PUSH_SEC` (10, debug image), `RELAY_STALE_SEC` (30).

## Deploying to a VPS

The `deploy/` folder holds everything needed. It targets Ubuntu 22.04/24.04 on either x86_64 or
ARM64, with at least 1 GB of RAM. On small machines the installer adds swap.

| File | Purpose |
|---|---|
| `deploy/upload.ps1` | Run on Windows: packs the project, uploads it over SSH, runs the installer |
| `deploy/install.sh` | Run on the server: idempotent install or update |
| `deploy/jelpricajos.service` | systemd unit (auto-start, restart on crash, sandboxed) |
| `deploy/jelpricajos-tunnel.service` | Temporary `trycloudflare.com` tunnel, used when no token is given |
| `deploy/jelpricajos.env.example` | Server settings, installed as `/etc/jelpricajos.env` |
| `deploy/status.sh` | Health overview: services, stream and speaker, tunnel URL, CPU/RAM |
| `deploy/next-session.sh` | Set or clear the next-session countdown (Serbian time) |

### 1. Create the server (Oracle Cloud Always Free)

1. Sign up at cloud.oracle.com. It asks for a card for verification only; Always Free resources
   are not charged. Pick a home region close to Serbia, e.g. Frankfurt.
2. Go to **Compute → Instances → Create instance**:
   * Image: **Ubuntu 24.04**
   * Shape: **Ampere → VM.Standard.A1.Flex**, 2 OCPU / 8 GB. That's within the free 4 OCPU / 24 GB.
     If A1 capacity is unavailable, retry later or use another availability domain.
   * SSH keys: **Generate a key pair** and download the private key, e.g. to `C:\Users\you\.ssh\oracle.key`.
3. Note the instance's **public IP**. The default user is `ubuntu`. No ports need opening apart
   from SSH, because the app is reached through the Cloudflare tunnel.

Any other Ubuntu VPS works the same way (Hetzner, DigitalOcean, …).

### 2. Deploy from Windows

```powershell
cd path\to\jelpricajos
# quick tunnel (random URL):
powershell -ExecutionPolicy Bypass -File .\deploy\upload.ps1 -Server ubuntu@<IP> -Key $HOME\.ssh\oracle.key
# or with a permanent tunnel on your domain:
powershell -ExecutionPolicy Bypass -File .\deploy\upload.ps1 -Server ubuntu@<IP> -Key $HOME\.ssh\oracle.key -TunnelToken <TOKEN>
```

The first run takes 5–15 minutes: PyTorch, EasyOCR models, ffmpeg, cloudflared. The script prints
the public URL at the end. Your local speech history is copied over on the first install.
Re-run the same command to push updates; the server keeps its own data.

Options:
* `-Cookies C:\path\cookies.txt`: YouTube cookies, if the VPS gets "Sign in to confirm you're not a bot".
* `-NoData`: start with an empty history.

### 3. Permanent URL (recommended)

1. Add your domain to Cloudflare.
2. Go to **Zero Trust → Networks → Tunnels → Create a tunnel → Cloudflared** and copy the token.
3. Under **Public hostname**, set `jelpricajos.yourdomain.rs` → `HTTP` → `localhost:8000`.
4. Re-run `upload.ps1` with `-TunnelToken <TOKEN>`. This replaces the quick tunnel. SSE works on
   named tunnels.

### Security

* The app listens on `127.0.0.1` only. The internet reaches it only through the Cloudflare tunnel,
  so no ports are opened except SSH.
* The public API is read-only. `/api/simulate` refuses anything that arrives through the tunnel.
  Error details (paths, yt-dlp/ffmpeg messages) are shown only to admins; the public gets a
  generic status.
* The installer turns on automatic security updates and **fail2ban** for SSH (5 failures, 1 h ban).
* It switches SSH to **key-only** login, but only if the account running it already has an
  `authorized_keys` file, so a password setup can't be locked out. Opt out with `HARDEN=0`.
* The service runs as an unprivileged `jelpricajos` user under systemd sandboxing. It can write only
  to `/var/lib/jelpricajos`.
* **YouTube cookies:** if you need them, export them from a **separate, throwaway Google account**.
  Anyone who gets the server also gets that account's session.

### Day-to-day

```bash
ssh -i ~/.ssh/oracle.key ubuntu@<IP>
sudo bash /opt/jelpricajos/deploy/status.sh     # health + URL
journalctl -u jelpricajos -f                    # live logs
sudo nano /etc/jelpricajos.env && sudo systemctl restart jelpricajos
```

Paths on the server:
* Database: `/var/lib/jelpricajos/speeches.db`
* App: `/opt/jelpricajos`

After moving to the VPS, shut down the local copy, and point the Chrome extension at the new URL.

## Using the dashboard

* **Watch the Assembly:** the live session is embedded through the YouTube IFrame API, with three switches:
  * **Mute when Vučić speaks** (on by default)
  * **Blur the picture when Vučić speaks.** The picture is fully blurred, with a "Show anyway"
    button that lasts until the speaker changes.
  * **Same when an MP from his party speaks.** Muting and blurring then also apply to
    "Aleksandar Vučić – Srbija pobeđuje".

  Sound comes back by itself afterwards. If you unmute by hand during a speech, it stays unmuted
  until the speaker changes. Settings are saved in the browser. The player gets a red frame while
  it is auto-muted.
* **SR / EN** switch in the header. The UI is available in Serbian (Latin script) and English.
  It defaults to the browser's language, and your choice is remembered.
* The design is minimal: vanilla CSS with no UI framework, a 720 px column, Inter and JetBrains Mono,
  and hairline borders. It follows the system light or dark theme.
* **🔊 Enable Celebration Sound.** Browsers block audio until you interact with the page, so click
  this once. The setting is remembered. After a reload, any click on the page re-arms the sound.
* **🎉 Test Celebration** previews the confetti and fanfare locally.
* **OCR Live Debug** (collapsible): shows the cropped ROI with boxes, refreshed every 2 s.
  Green means a matched line, yellow means text within the size window, and red means it was
  rejected by the height filter. It also lists the raw OCR text, box heights (as % of H) and
  match scores. It has buttons to simulate backend states too.

## Public API (v1)

Read-only, CORS-enabled (`*`), rate-limited (`RATE_LIMIT_PER_MIN`, default 240/min per client).
Interactive docs live at **`/docs`**.

| Endpoint | Description |
|---|---|
| `GET /api/v1/status` | `{vucic_speaking, party_member_speaking, speaker_state, stream_status, video_id, current_session_started_at, current_session_duration_sec, last_spoke_timestamp, simulated, updated_at}` |
| `GET /api/v1/events` | SSE: `status` (on every change) and `speech_ended`. Prefer this over polling. Max 4 streams per client. |
| `GET /api/v1/stats` | Today and all-time totals, speech count |
| `GET /api/v1/speeches?limit=20&since=ISO` | Recorded speeches, newest first |
| `GET /api/v1/health` | Liveness check |

```js
const es = new EventSource('https://your-host/api/v1/events');
es.addEventListener('status', (e) => console.log(JSON.parse(e.data).vucic_speaking));
```

**Exposing it to others:**
* **LAN:** run with `HOST=0.0.0.0`.
* **Internet without opening ports:** use `cloudflared tunnel --url http://localhost:8000`, which
  gives a random `*.trycloudflare.com` URL that changes on every restart.
  **Quick tunnels do not pass SSE through.** The dashboard notices this and switches to polling
  every 2 s by itself. API users should poll `/api/v1/status` behind a quick tunnel. For a permanent
  hostname with working SSE, use a named tunnel (free Cloudflare account plus your own domain) or a
  reverse proxy.
* **Admin endpoint:** `/api/simulate` only works from this machine. Requests that arrive through
  a proxy or tunnel are refused unless they send `X-Admin-Token` matching `ADMIN_TOKEN`.

## Chrome extension: "jelpricajos"

The extension in `extension/` mutes YouTube tabs, and can optionally blur the video, while Vučić is speaking.

1. Open `chrome://extensions`, turn on **Developer mode**, click **Load unpacked**, and select the
   `extension/` folder.
2. Click the extension icon and set:
   * **Mute YouTube while Vučić speaks.** This is the main switch.
   * **Blur the video while Vučić speaks.** Only the picture is blurred; the page, comments and
     controls stay usable. Off by default.
   * **Same for MPs of his party group** ("Aleksandar Vučić – Srbija pobeđuje"). Applies to both
     mute and blur. Off by default.
   * **Which tabs:** all YouTube tabs, or only the Parliament stream.
   * **Monitor API URL.** The default is `http://127.0.0.1:8000`. For a remote or public monitor,
     Chrome asks for permission for that host.
3. Badge: **V** (red) means Vučić is speaking, **P** (blue) means a party member is speaking, and
   **!** means the monitor can't be reached.

How it behaves:
* It checks `/api/v1/status` every 2 s.
* It only unmutes tabs that it muted itself. Tabs you muted by hand stay muted.
* If the monitor is unreachable for 20 s, it restores sound.
* The UI is in Serbian or English, following Chrome's language.

## Internal API

| Endpoint | Description |
|---|---|
| `GET /` | Dashboard |
| `GET /api/state` | Full state JSON (status, speaker state, timers, stats, recent speeches, OCR debug) |
| `GET /api/events` | Server-Sent Events: `state` (full snapshot on change and every 15 s) and `speech_ended` (`{duration_sec, start, end, simulated}`) |
| `GET /api/debug/frame.jpg` | Latest annotated ROI |
| `POST /api/simulate` | `{"state": "VUCIC_SPEAKING" \| "PARTY_MEMBER_SPEAKING" \| "OTHER_OR_NONE" \| "RELEASE", "persist": false}` |

```bash
curl -X POST localhost:8000/api/simulate -H "Content-Type: application/json" -d '{"state":"VUCIC_SPEAKING"}'
curl -X POST localhost:8000/api/simulate -H "Content-Type: application/json" -d '{"state":"OTHER_OR_NONE"}'   # celebration
```

A simulated state holds for `SIMULATION_HOLD_SEC` (120 s) before OCR takes over again, or until you
send `RELEASE`. Simulated speeches are not saved unless you pass `"persist": true`.

## Data

Speeches are stored in `data/speeches.db` (SQLite). A speech in progress is checkpointed every
10 s, so a crash or restart loses at most a few seconds. "Today" uses the machine's local time.

## Calibration tips

1. Open **OCR Live Debug** while an MP is speaking.
2. If caption lines show as red because they're too big or too small, adjust
   `MIN_BOX_HEIGHT_RATIO` and `MAX_BOX_HEIGHT_RATIO`. Keep `MAX` below the animated chyron's height.
3. If the caption moves on screen, adjust `ROI_*`.
4. If the text is misread, try `PREPROCESS_MODE=threshold`, or change `OCR_UPSCALE` (default 1.5).

## Troubleshooting

* **Always OFFLINE with "Sign in to confirm you're not a bot".** Export YouTube cookies to a
  Netscape-format file and set `YTDLP_COOKIES_FILE=path/to/cookies.txt`.
* **"Requested format is not available".** Update yt-dlp (`pip install -U yt-dlp`) and install
  Deno. YouTube changes often.
* **Stuck at CONNECTING.** Run with `LOG_LEVEL=DEBUG` to see ffmpeg's stderr.
* **Using Tesseract instead.** Install Tesseract with the `srp` language,
  `pip install pytesseract`, then set `OCR_BACKEND=tesseract`.

## Tests

```bash
pip install pytest pillow
python -m pytest tests -q          # unit tests plus OCR on synthetic chyron frames (loads EasyOCR)
```

## Files

```
config.py           tunable settings (env-overridable)
ocr_engine.py       ROI crop, preprocessing, EasyOCR/Tesseract wrapper, size filter, fuzzy matching, debug drawing
stream_monitor.py   yt-dlp discovery, ffmpeg grabber, debounce state machine, SQLite, SSE broadcaster
app.py              FastAPI server
static/index.html   dashboard (Tailwind, canvas-confetti, Web Audio fanfare, SSE client)
tests/              unit + synthetic OCR tests
deploy/             VPS installer (full or relay), systemd units, Windows upload + worker scripts
common.py           dependency-free shared bits (the relay imports only this)
relay.py            RelayHub (public side) and RelayPublisher (home worker)
extension/          Chrome extension (MV3) that mutes YouTube while he speaks
```
