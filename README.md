# SV notify-only appointment watcher

This service discovers the current A/B/C reservation operations from the
official uw.bezkolejki.eu catalog, checks availability for all three queues
every cycle (each queue gets its own fresh browser context so reCAPTCHA tokens
never bleed between them), sends urgent outbound Telegram alerts, and accepts
Telegram chat commands from the configured admin chat.
It cannot block, fill, or confirm an appointment.

The only supported mode is MODE=notify_only. Startup rejects block,
block_and_fill, and AUTO_CONFIRM=true.

## Deploy — option A: LXC / VM with real desktop browser (recommended)

Running a real visible Chromium scores highest on reCAPTCHA v3. Use this on
any Proxmox LXC or VM that has a desktop session (Xorg + openbox or xfce4).

1. Install system deps and Playwright:

       apt-get install -y python3 python3-pip xorg openbox
       pip3 install -r requirements.txt
       playwright install chromium
       playwright install-deps chromium

2. Copy config.example.env to .env and fill in TELEGRAM_BOT_TOKEN and
   TELEGRAM_CHAT_ID. Set:

       HEADLESS=false

3. Create a systemd user service (e.g. `~/.config/systemd/user/sv.service`):

       [Unit]
       Description=SV slot watcher
       After=graphical-session.target

       [Service]
       Environment=DISPLAY=:0
       WorkingDirectory=/opt/sv
       ExecStart=/usr/bin/python3 bot.py
       Restart=always
       RestartSec=10

       [Install]
       WantedBy=default.target

4. Enable and start:

       systemctl --user enable sv
       systemctl --user start sv

State lives at DATABASE_PATH (default `/app/data/sv.db`; override in .env for
a non-container path, e.g. `/home/user/sv.db`).

## Deploy — option B: Docker with headless Xvfb (no local desktop needed)

The docker-compose.yml starts an Xvfb virtual display inside the container
before launching the watcher, giving Chromium a real DISPLAY without requiring
a host desktop. This is more reliable than fully headless mode.

1. Copy config.example.env to .env.
2. Set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID. Keep `HEADLESS=false` (the
   compose file already sets `DISPLAY=:99` for Xvfb).
3. Revoke any previously configured 2Captcha API key in the 2Captcha account;
   this project no longer reads or supports it.
4. Start the watcher:

       docker compose up -d --build

State is stored in SQLite at /app/data/sv.db on the named sv-data volume.
SQLite uses WAL mode and a five-second busy timeout. The container runs as the
image's unprivileged pwuser.

Playwright is pinned to 1.61.0 in both requirements.txt and the
v1.61.0-noble image. Keep those versions identical when upgrading.

## Administration over SSH

Run all administration through the existing watcher container:

    docker compose exec watcher python svctl.py status
    docker compose exec watcher python svctl.py status --json
    docker compose exec watcher python svctl.py events --level ERROR
    docker compose exec watcher python svctl.py events --follow
    docker compose exec watcher python svctl.py doctor
    docker compose exec watcher python svctl.py check --wait 120
    docker compose exec watcher python svctl.py pause
    docker compose exec watcher python svctl.py resume

check, pause, and resume enqueue commands in SQLite. They never start a second
browser. Paused state, incidents, availability deduplication, events, commands,
and pending Telegram delivery all survive a restart.

### Telegram admin commands

The watcher polls Telegram `getUpdates` with long-polling and persists the last
processed update offset in SQLite, so commands are not replayed after restart.
Only commands from `TELEGRAM_CHAT_ID` are accepted; all other chats are ignored
and logged.

Supported commands:

- `/status` – runtime state (running/paused), heartbeat age, last cycle status/error, and A/B/C operation status with attempt/success/failure counters.
- `/recheck` – enqueue an immediate `check` command via the existing command queue; waits briefly for completion and returns summary, otherwise returns queued state.
- `/stats` – operation attempt summary plus system snapshot (DB size, disk free/total, PID/uptime, Python/runtime info, and memory info when available).

Exit codes:

- 0: healthy or successful
- 2: degraded or a partial A/B/C check
- 3: intentionally paused
- 4: watcher unavailable or command timed out

doctor checks configuration, database access, process heartbeat, catalog
freshness, and per-operation freshness. The Docker healthcheck treats a paused
watcher as alive and degraded/unavailable state as unhealthy.

## Alert policy

Telegram messages are limited to:

- verified availability, with the official manual-booking link;
- sustained operation/service outage and material escalation;
- recovery after an outage that was alerted;
- CAPTCHA contract change (the site changed CAPTCHA vendor — the watcher is
  blind until the code is updated);
- CAPTCHA cool-down (polling paused because the site is refusing us tokens);
- once-daily digest at `DAILY_DIGEST_HOUR`;
- admin command replies for `/status`, `/recheck`, and `/stats`.

Every message is prefixed with `[NODE_NAME]` so multiple vantage points are
distinguishable in one chat.

An availability alert is deduplicated by operation and day/slot identity. It
re-alerts only for an earlier result or after two successful empty cycles and a
return. Telegram delivery retries after 5, 30, and 120 seconds. Exhausted
messages remain undelivered in status and events.

Outage escalation is capped at level 3; beyond that a single "still down"
reminder is sent at most every 6 hours, so a multi-hour outage cannot spam the
chat.

No startup or heartbeat Telegram messages are sent. Logs and events contain no
tokens, cookies, CAPTCHA values, applicant data, or raw API bodies. Redacted
events are retained for seven days and capped at 10,000 rows.

## CAPTCHA

The site uses **hCaptcha** (it migrated away from reCAPTCHA v3; the page loads
`js.hcaptcha.com/1/api.js?recaptchacompat=off`, so `window.grecaptcha` never
exists). The token is still sent under the legacy `recaptchaToken` query
parameter, which is what the site's own UI does.

The watcher drives the page's own `window.hcaptcha`, rendering one invisible
widget per browser context and calling `hcaptcha.execute(...)`. The sitekey is
discovered at runtime from the DOM and `config.js`, falling back to the pinned
`HCAPTCHA_SITE_KEY`, so a sitekey rotation does not cause an outage.

Failures are classified so they are actionable:

| Error | Meaning | Response |
| --- | --- | --- |
| `CaptchaContractError` | The page no longer exposes a usable hCaptcha API | One loud alert; needs a code change |
| `CaptchaChallengeError` | hCaptcha demanded an interactive challenge | Exponential backoff, then cool-down |
| `CaptchaError` | Server rejected the token (HTTP 400) | Same as above; reason text included |

CAPTCHA failures are never retried inside a cycle: a rejected or challenged
token is not transient, and retrying multiplies the request volume that damaged
the IP's reputation in the first place.

### Backoff and cool-down

While every queue in a cycle is blocked, the poll interval doubles per blocked
cycle up to `CAPTCHA_BACKOFF_MAX_SECONDS`. After
`CAPTCHA_COOLDOWN_FAILURES` consecutive blocked cycles all polling pauses for
`CAPTCHA_COOLDOWN_SECONDS` and one alert is sent. Any successful queue resets
both immediately.

### Optional paid solver

If the site refuses passive tokens even at low volume from a clean IP, enable a
solver. It is **off by default** and only engaged after a native mint is
refused, so it costs nothing while things work.

    CAPTCHA_SOLVER_PROVIDER=capsolver   # or 2captcha
    CAPTCHA_SOLVER_API_KEY=...
    CAPTCHA_SOLVER_MAX_PER_HOUR=60      # hard spend cap

Usage and spend are visible in `/stats` and the daily digest.

## Running a second vantage point (Raspberry Pi)

Detection latency halves — at no extra load per IP — by running a second
independent node whose schedule is offset by half an interval. Both nodes alert
to the same chat, labelled by `NODE_NAME`. The nodes share nothing, so either
one dying is invisible.

On a 64-bit Raspberry Pi:

    sudo bash install-rpi.sh /path/to/sv-source

Playwright has no arm64 Linux Chromium build, so the installer uses the distro
`chromium` and points the watcher at it via
`PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH`. Then set in `/opt/sv/.env`:

    NODE_NAME=rpi-home2
    SCHEDULE_OFFSET_SECONDS=150      # ~half of SLOW_INTERVAL_SECONDS

Occasional duplicate availability alerts are accepted deliberately: for slot
hunting a duplicate is strictly better than a miss, and dedupe would couple the
nodes and reintroduce a single point of failure.

## Verified read-only contracts

The watcher validates that catalog entries have matching A/B/C prefixes, exact
service names, and isReservationActive=true, then persists the snapshot.
Availability must match the captured schema and return the requested
operationId; malformed data is an operation failure, never an empty result.
Slot details may be a JSON array or use availableSlots, slots, or times, and
every entry must contain id and dateTime.

Raw HAR captures are deliberately ignored and removed after sanitized fixtures
are derived. Never commit HARs: they may contain tokens and cookies.

## Test

    python -m pytest -q

Docker smoke tests require Docker on the host:

    docker compose build
    docker compose up -d
    docker compose exec watcher python svctl.py doctor --json

The live acceptance check is:

    docker compose exec watcher python svctl.py check --wait 120 --json

Accept only healthy A/B/C rows in SQLite. With empty availability arrays,
Telegram must remain silent. The runtime contains GET endpoints only.

### Current live verification

On 2026-07-14, all three operation IDs (A, B, C) were validated and persisted.
Each queue now runs in its own fresh browser context, so every reCAPTCHA mint
is a first-mint — B and C no longer fail due to CAPTCHA exhaustion from a
shared context. All three queues return healthy empty availability arrays and
Telegram remains silent when no slots are open.
