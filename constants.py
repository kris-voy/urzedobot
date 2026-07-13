"""Site, protocol, and timing constants for the bezkolejki.eu watcher.

Also responsible for loading .env / applicant.env exactly once, at first
import, since env vars (USER_AGENT here, everything else via Config in
config.py) must already be in os.environ before anything reads them.
"""
from __future__ import annotations

import os

from dotenv import load_dotenv

_BASE_DIR = os.path.dirname(os.path.abspath(__file__))
ENV_PATH = os.path.join(_BASE_DIR, ".env")
load_dotenv(ENV_PATH)
# Personal data for auto-fill lives in a separate file so operational config
# (.env) stays free of one person's private details. Loaded second; its FORM_*
# keys override any leftover FORM_* in .env. Optional — absent file is fine.
APPLICANT_ENV_PATH = os.path.join(_BASE_DIR, "applicant.env")
load_dotenv(APPLICANT_ENV_PATH, override=True)

COMPANY_NAME = "ouw"
BASE_URL = "https://uw.bezkolejki.eu/api"
RESERVATION_PAGE_URL = "https://uw.bezkolejki.eu/ouw/Reservation"
RECAPTCHA_SITE_KEY = "6LeCXbUUAAAAALp9bXMEorp7ONUX1cB1LwKoXeUY"

QUEUE_OPERATION_IDS = {
    "A": 3213864,
    "B": 3219596,
    "C": 3219597,
}

USER_AGENT = os.environ.get(
    "USER_AGENT",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
).strip() or (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)

CONFIRM_BUTTON_TIMEOUT_SECONDS = 4 * 60
POST_SUCCESS_KEEPALIVE_SECONDS = 10 * 60
MAX_CONSECUTIVE_FAILURES = 3
RATE_LIMIT_BACKOFF_SECONDS = 5 * 60
CAPTCHA_RETRY_DELAY_SECONDS = 5
# Minimum gap between consecutive reCAPTCHA v3 token mints. Minting several
# tokens back-to-back from the same headless page yields low scores that the
# server rejects with HTTP 400 ("Error while verify captcha"), so we space them
# out generously — reliability matters far more than shaving seconds here.
CAPTCHA_MINT_SPACING_SECONDS = 3.0
# How many times to re-mint + retry a call that 400s on captcha validation
# (in addition to the first attempt), with escalating backoff.
CAPTCHA_MAX_RETRIES = 2
NOTIFY_DEDUPE_SECONDS = 30 * 60

# --- 2Captcha (optional captcha-solving-service provider) --------------------
# NOTE: live-tested against uw.bezkolejki.eu and confirmed NOT to work — the
# reCAPTCHA v3 tokens 2Captcha returns are rejected by this site's server even
# at CAPTCHA_MIN_SCORE=0.9. Kept working (not deleted) since it's a documented,
# config-gated feature and may be useful again if the site's captcha
# requirements change; CAPTCHA_PROVIDER is forced to "browser" in the live
# .env. See README "Captcha reliability" and memory for the test history.
TWOCAPTCHA_SUBMIT_URL = "https://2captcha.com/in.php"
TWOCAPTCHA_RESULT_URL = "https://2captcha.com/res.php"
TWOCAPTCHA_POLL_INTERVAL_SECONDS = 5.0
TWOCAPTCHA_TIMEOUT_SECONDS = 120.0
