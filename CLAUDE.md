# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A single-file Python bot (`bot.py`) that watches uw.bezkolejki.eu (a Polish
government appointment-reservation site) for free slots across three queues,
and — depending on `MODE` — notifies, blocks, or blocks+fills+confirms a
reservation, coordinating via Telegram. Built for one personal appointment
(single Telegram chat id, single-flight grab pipeline).

## Commands

```powershell
# Install (Windows; python3.14 is this machine's interpreter)
python3.14 -m pip install -r requirements.txt
python3.14 -m playwright install chromium

# One-off check, no Telegram, no booking
python3.14 bot.py --once --dry-run

# Send a test Telegram message / a simulated "slot found" alert
python3.14 bot.py --test-telegram
python3.14 bot.py --test-slot

# End-to-end reliability check (browser+site+captcha+Telegram), prints PASS/FAIL
python3.14 bot.py --healthcheck

# Real run (uses MODE from .env); --dry-run forces notify_only behavior
python3.14 bot.py
python3.14 bot.py --dry-run

# Guided setup for applicant.env (personal data for auto-fill)
python3.14 setup_applicant.py

# Docker
docker compose up --build -d
docker compose logs -f
```

There is no test suite, linter, or build step — this is an unpackaged single
script plus a setup helper. Validate changes with `--once --dry-run` and
`--healthcheck` rather than assuming correctness.

## Configuration files (not committed; do not fabricate values)

- `.env` — operational config, copied from `config.example.env` (Telegram
  token/chat id, `QUEUES`, `MODE`, polling windows, captcha provider).
- `applicant.env` — one person's personal data used only by
  `MODE=block_and_fill`, kept deliberately separate from `.env`. Loaded
  *after* `.env` with `override=True`, so its `FORM_*` keys win. Generate it
  via `setup_applicant.py` or by copying `applicant.example.env`.
- Any `FORM_*` env var becomes a fuzzy-match candidate for a reservation form
  field (see `fuzzy_match_field` / `build_filled_properties` in `bot.py`):
  the key minus the `FORM_` prefix, underscores → spaces, diacritics
  stripped, matched against the site's field labels.

## Architecture (`bot.py`, ~1500 lines, single module)

Everything lives in one file, in this order: env/config loading → `Config`
dataclass → helpers (fuzzy field matching, window/jitter math) →
`BezkolejkiClient` → `build_filled_properties` → `TelegramNotifier` →
`DedupeTracker` → `Watcher` → CLI entry point (`main`).

**`Config`** — a `@dataclass` populated entirely from env vars (`.env` then
`applicant.env`, loaded once at import time via `load_dotenv`). Validates
itself in `__post_init__` (raises on missing token/chat id/queues, invalid
`MODE`/`CAPTCHA_PROVIDER`). This is the single source of truth for behavior;
there's no other config path.

**`BezkolejkiClient`** — owns the Playwright browser/context/page and all
site API calls. Key design points, all load-bearing (don't "simplify" them
away):
- All HTTP calls to the site happen via `page.evaluate()` doing an in-page
  `fetch`, never via a Python HTTP client — the site needs the browser's
  Cloudflare clearance and the auth token stored in the page's
  `localStorage`.
- `_open_fresh_page()` creates a brand-new browser context before each
  polling cycle (and optionally before each queue, via
  `FRESH_PAGE_PER_QUEUE`) because reusing one page across many reCAPTCHA v3
  mints measurably degrades the score until the server starts rejecting
  calls with HTTP 400.
- `_mint_captcha_token` dispatches to either the in-page `grecaptcha.execute`
  method (`browser`) or the 2Captcha solving service (`2captcha`), based on
  `_captcha_provider` (resolved once at construction: `auto` → `2captcha` if
  `TWOCAPTCHA_API_KEY` is set, else `browser`). Datacenter/VPS IPs generally
  need `2captcha`; residential IPs can often use `browser`.
- `_api_call` is the single choke point for every site API request: it
  attaches the auth token + a freshly minted captcha token, retries once or
  twice (with escalating backoff) on HTTP 400 (treated as a captcha
  rejection, always re-minting rather than reusing the token), and raises
  `RateLimitedError` on 429 for the caller to back off on.
- `GetPropertiesForSlot` (`get_properties_for_slot`) was never live-verified
  against a real blocked slot (doing so would require actually blocking a
  real slot). It's deliberately defensive — see the README section "Notes on
  the one endpoint that isn't fully verified" — and logs the full raw
  response the first time it fires for real. If the real field-definition
  shape differs from what's expected, the fix is localized to
  `get_properties_for_slot()` and `build_filled_properties()`.

**`TelegramNotifier`** — wraps `python-telegram-bot`. Every command handler
checks `_is_authorized` (chat id must match `TELEGRAM_CHAT_ID`; everyone else
is silently ignored). `ask_confirm_or_release` implements the
Confirm/Release button flow as a single pending `asyncio.Future` (one
in-flight confirmation at a time — this is what makes the grab pipeline
single-flight), with a 4-minute timeout.

**`Watcher`** — the polling loop and grab pipeline:
- `run_cycle` → `check_all_queues_once` (shuffles queue order per cycle, so
  no queue is systematically starved) → on first queue with availability,
  `handle_available_queue` → (`block`/`block_and_fill` only)
  `_fill_and_confirm` → `_do_confirm`. Each stage that can fail sends a
  Telegram message with the direct reservation link so the user can always
  finish manually — the pipeline is designed to never fail silently.
- Reliability/self-healing: tracks consecutive failed cycles and restarts
  the browser after `MAX_CONSECUTIVE_FAILURES`; `_check_watchdog` sends a
  Telegram alert if there's been no successful check in
  `STALE_ALERT_MINUTES` (otherwise a broken bot looks identical to "no slots
  today" in `notify_only` mode) and an optional periodic heartbeat.
- After a successful `ConfirmReservation`, `self.stopped = True` ends
  polling, but the Telegram connection is kept alive for
  `POST_SUCCESS_KEEPALIVE_SECONDS` to make sure the success message (and any
  follow-up commands) are actually delivered before the process exits.

## Non-obvious constraints when modifying

- Never call the site's API from Python directly (e.g. `requests`/`httpx`) —
  it will be missing Cloudflare clearance and the reCAPTCHA/auth context;
  all calls must go through `page.evaluate()`.
- Don't remove or shortcut the fresh-context-per-cycle behavior or the
  captcha mint spacing (`CAPTCHA_MINT_SPACING_SECONDS`) — both exist
  specifically to keep reCAPTCHA v3 scores high enough to pass.
- Keep the "always notify with the direct link on failure" pattern in any
  new pipeline step — this bot is explicitly designed to degrade to "tell
  the human" rather than fail silently or retry forever.
- `applicant.env`/`FORM_*` values are one real person's personal data (name,
  PESEL, document numbers) — treat with the same care as credentials; never
  log full field values beyond what `fuzzy_match_field` already logs.
