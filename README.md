# bezkolejki.eu appointment slot watcher + Telegram bot

Watches the Polish government reservation site
[uw.bezkolejki.eu/ouw/Reservation](https://uw.bezkolejki.eu/ouw/Reservation)
for free slots in three queues, and when one appears can automatically block
it, fill the reservation form, and (on your confirmation via a Telegram
button) submit it. Built for a single personal appointment - it polls
politely and only talks to one Telegram chat id.

## What it does

1. Runs a headless Chromium browser (via Playwright) that loads the
   reservation page once and keeps it open, so it has a valid session/token
   and can mint real Google reCAPTCHA v3 tokens (required by every API call
   on this site).
2. On a schedule (fast polling during a configurable morning window, slow
   polling otherwise), checks each configured queue (A/B/C) for available
   days.
3. When a queue has an available day, depending on `MODE`:
   - `notify_only` - sends a Telegram message with the details and a link.
   - `block` - calls the site's `BlockSlot` API to hold the slot, then
     notifies you to finish manually within the hold window.
   - `block_and_fill` (default) - blocks the slot, fetches the dynamic
     reservation form fields, fills them from your configured personal data,
     and either submits immediately (`AUTO_CONFIRM=true`) or sends a
     Telegram message with **Confirm** / **Release** buttons and waits up to
     4 minutes for your decision.
4. After a successful confirmation it tells you to check your email (the
   site requires clicking a confirmation link - "two-step email
   confirmation") and stops polling.
5. If anything in the block/fill/confirm pipeline fails partway, it still
   sends you the availability info and the reservation link so you can
   finish manually - it never just silently gives up.
6. Never crashes on transient errors: captcha hiccups are retried once,
   rate limiting (`429`) triggers a 5-minute backoff, and if the browser
   session dies or too many check cycles fail in a row, it restarts the
   browser and keeps going.

## Requirements

- Python 3.14 (this machine's interpreter is `python3.14`)
- Playwright with the Chromium browser installed
- python-telegram-bot v22+
- python-dotenv

Install (Windows, from this directory):

```powershell
python3.14 -m pip install -r requirements.txt
python3.14 -m playwright install chromium
```

For running the test suite (`config`, `dedupe`, `formfill`, `captcha`,
`notifier` — pure logic only, no browser/Telegram/network involved), install
the dev extras instead and run pytest:

```powershell
python3.14 -m pip install -r requirements-dev.txt
python3.14 -m pytest -q
```

## Setting up the Telegram bot

1. **Create the bot.** In Telegram, message **@BotFather**, send `/newbot`,
   and follow the prompts (pick a display name and a unique username ending
   in `bot`). BotFather replies with a token that looks like
   `123456789:AAExampleTokenHere` - this is your `TELEGRAM_BOT_TOKEN`.

2. **Get your chat id.** Open a chat with your new bot in Telegram and send
   it any message (e.g. `/start`). Then, in a browser or with `curl`, visit:

   ```
   https://api.telegram.org/bot<YOUR_TOKEN>/getUpdates
   ```

   Look for `"chat":{"id":123456789, ...}` in the JSON response - that
   number is your `TELEGRAM_CHAT_ID`. (If you see an empty `"result":[]`,
   make sure you actually sent the bot a message first, then reload.)

   Alternatively, once the bot is running with a placeholder chat id, run it
   and use `/status` from any chat - unauthorized senders are simply
   ignored, so this only really works once you already have the right id
   from `getUpdates`.

3. Put both values into your `.env` file (see below).

## Configuration

Copy the example env file and edit it:

```powershell
Copy-Item config.example.env .env
notepad .env
```

Key settings (see `config.example.env` for full documentation of every
key):

| Key | Meaning |
|---|---|
| `TELEGRAM_BOT_TOKEN` | Token from BotFather |
| `TELEGRAM_CHAT_ID` | Your numeric chat id (only this id is obeyed) |
| `QUEUES` | Which of `A,B,C` to watch |
| `MODE` | `notify_only`, `block`, or `block_and_fill` |
| `AUTO_CONFIRM` | `true` to submit immediately after filling; `false` to wait for a Telegram button |
| `FAST_WINDOW` | Local (Europe/Warsaw) time range for fast polling, e.g. `05:45-08:45` |
| `FAST_INTERVAL_SECONDS` / `SLOW_INTERVAL_SECONDS` | Poll intervals (±20% jitter applied) |
| `FORM_*` | Applicant data for auto-fill. Kept in a **separate `applicant.env`** file, not `.env` — see "Applicant data" below. Any `FORM_XXX_YYY` key becomes a fuzzy-matched candidate value for a reservation form field labeled roughly "xxx yyy" |
| `CAPTCHA_PROVIDER` | `auto` (default), `browser`, or `2captcha` -- see "Captcha reliability" below |
| `TWOCAPTCHA_API_KEY` | Your 2captcha.com API key (optional; enables the `2captcha` provider) |
| `CAPTCHA_MIN_SCORE` | Minimum reCAPTCHA v3 score requested from 2captcha, default `0.3` |
| `HEADLESS` | Whether Chromium launches headless, default `true` |

## Applicant data (auto-fill)

Only needed for `MODE=block_and_fill`. The applicant's personal details live in
a **separate file, `applicant.env`**, kept apart from `.env` so operational
settings and private data don't mix. Two ways to create it:

- **Guided wizard (recommended):** `python3.14 setup_applicant.py` — asks for
  each field (name, surname, DOB, email, phone, case number, PESEL, document
  numbers), shows current values as defaults on re-run, and writes the file for
  you. Runs anywhere, no server needed.
- **By hand:** copy `applicant.example.env` to `applicant.env` and edit.

The bot loads `applicant.env` automatically on start (its `FORM_*` values
override any left in `.env`). The file is optional — without it, `notify_only`
and `block` modes still work fully; only auto-fill needs it.

Note: the site only reveals the exact form fields once a real slot is blocked,
so fill in everything you can — unmatched values are ignored and unmatched
fields are left blank for you to finish from the Telegram link. The first real
grab logs the site's true field names so the list can be tightened.

## Captcha reliability

Every `/Slot/*` API call on this site requires a Google reCAPTCHA v3 token.
reCAPTCHA v3 doesn't show a challenge -- it silently scores how "human" the
request looks (0.0-1.0) and the server rejects low-scoring tokens with
HTTP 400 ("Error while verify captcha"). The bot already retries once or
twice on a 400 with backoff, but the deeper problem is the score itself:

- Tokens minted in-page by an automated headless browser tend to score low,
  and it's **much worse on a datacenter/VPS IP** than on a home/residential
  IP, because reCAPTCHA also factors in IP reputation. If you're running
  this on a VPS (mikr.us, Hetzner, etc.), you should expect the in-page
  browser method (`CAPTCHA_PROVIDER=browser`) to fail unpredictably.
- The fix is `CAPTCHA_PROVIDER=2captcha`: this mints the token via the
  [2captcha.com](https://2captcha.com) solving service instead, which
  reliably returns high-scoring tokens (it runs the challenge through real
  browser farms / residential exit nodes on their end). Cost is roughly
  **$1-3 per 1000 solves** -- sign up, add a few dollars of balance, and set
  `TWOCAPTCHA_API_KEY` in your `.env`. With `CAPTCHA_PROVIDER=auto` (the
  default), the bot automatically uses 2captcha once a key is present and
  falls back to the browser method otherwise, so on a VPS you almost
  certainly want to set that key.
- The Playwright browser is **still required** even with `2captcha` set --
  it holds the Cloudflare clearance and auth token and performs the actual
  in-page `fetch` for every API call. 2captcha only replaces how the
  reCAPTCHA token itself is minted.
- `HEADLESS` (default `true`) controls whether Chromium launches headless.
  If you're stuck on the browser method (no 2captcha key) on a Linux VPS,
  running headful (`HEADLESS=false`) under a virtual display can sometimes
  improve scores -- wrap the process with `xvfb-run` (e.g.
  `xvfb-run python3.14 bot.py`, or in `docker-compose.yml` wrap the
  container's entrypoint/command with `xvfb-run`). This is not needed with
  the default `HEADLESS=true`, and not needed at all if you're using
  2captcha.

## Running locally on Windows

```powershell
# One-off test: single check cycle, prints results, no Telegram, no booking
python3.14 bot.py --once --dry-run

# Send a test Telegram message and exit
python3.14 bot.py --test-telegram

# Real run (uses MODE from .env)
python3.14 bot.py

# Real run but force-safe (never blocks/books, acts as notify_only)
python3.14 bot.py --dry-run
```

Leave the terminal window open (or run it under Task Scheduler / as a
background process) - the script keeps a browser and the Telegram bot alive
continuously. Python 3.8+ uses the Proactor event loop by default on
Windows, which is what Playwright's subprocess-based browser launch needs,
so no extra event loop policy configuration is required.

### Telegram commands while running

- `/status` - last check time and last cycle's per-queue result summary
- `/pause` - stop polling (bot stays connected, just skips cycles)
- `/resume` - resume polling
- `/test` - confirms the bot is alive and listening

## Running in Docker

Build and run with docker-compose (uses the `.env` file you created above):

```powershell
docker compose up --build -d
docker compose logs -f
```

The `Dockerfile` is based on
`mcr.microsoft.com/playwright/python:v1.61.0-jammy` to match the Playwright
version installed locally (`playwright==1.61.0`). **Before deploying,
double check that tag actually exists** (Microsoft doesn't publish an image
for every single pip release):

```powershell
docker manifest inspect mcr.microsoft.com/playwright/python:v1.61.0-jammy
```

If it doesn't exist, pick the closest published tag at or below your
installed version from
https://mcr.microsoft.com/en-us/product/playwright/python/tags and pin the
matching `playwright==` version in `requirements.txt` so pip and the base
image agree (mismatches between the pip package and the browser binaries
baked into the image can break Playwright).

### Deployment sizing

Headless Chromium under Playwright typically uses **~500-600MB RAM** for
this kind of light single-page workload, plus overhead for Python and the
Telegram long-poll connection. Recommendations:

- **mikr.us**: pick a **2GB RAM tier or higher**. Their smallest tiers
  (under 1GB) are too tight once you add OS + Docker + Chromium overhead
  and will risk OOM kills during polling.
- **Fallback**: **Hetzner CX22** (2 vCPU / 4GB RAM) is comfortably enough
  headroom for this workload if mikr.us doesn't fit or isn't available.

`docker-compose.yml` sets `mem_limit: 1g` and `shm_size: 1gb` (Chromium
needs a reasonably sized `/dev/shm` or it can crash) - adjust upward if you
add more concurrency, but this bot only ever runs one browser page at a
time so 1GB is comfortable headroom.

## Notes on the one endpoint that isn't fully verified

`GetPropertiesForSlot` (called right after a successful `BlockSlot`, to
fetch the dynamic reservation form field definitions) was not live-tested
end-to-end against a real blocked slot before building this bot - live
testing it would have meant actually blocking a real slot, which this build
was told not to do. It's implemented defensively:

- Called with `companyName` + `slotId` + `recaptchaToken` query params
  (matching the pattern every other `Slot` GET endpoint uses).
- The full raw response is logged at INFO level the first time it's
  called for real, so you can see the exact shape it returns.
- `build_filled_properties()` in `bot.py` tries several common field-name
  keys (`name`, `label`, `propertyName`, `displayName`) and several
  common value keys (`value`, `propertyValue`, `fieldValue`) defensively,
  and logs every fuzzy match it makes.

If the real shape turns out to be different, the fix is localized to
`get_properties_for_slot()` and `build_filled_properties()` in `bot.py` -
watch the logs the first time a slot actually gets blocked in
`block_and_fill` mode and adjust based on what's logged.

## Safety notes

- The bot is single-flight: while waiting for your Confirm/Release button
  press (up to 4 minutes) it does **not** keep polling other queues, so it
  won't try to block a second slot while you're deciding on the first.
- `--dry-run` and `MODE=notify_only` never call `BlockSlot` or
  `ConfirmReservation` - safe for testing.
- After a successful confirmation, the bot stops polling, but keeps the
  Telegram connection alive for 10 minutes to make sure the success message
  (and any follow-up commands) get delivered, then exits.
