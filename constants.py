"""Public site contracts and runtime constants for the notify-only watcher."""

COMPANY_NAME = "ouw"
BASE_URL = "https://uw.bezkolejki.eu/api"
RESERVATION_PAGE_URL = "https://uw.bezkolejki.eu/ouw/Reservation"
SITE_ORIGIN = "https://uw.bezkolejki.eu"
SITE_CONFIG_URL = "https://uw.bezkolejki.eu/config.js"

# The site migrated from reCAPTCHA v3 to hCaptcha. The reCAPTCHA key is kept only
# so old databases/logs remain readable; it is no longer used to mint tokens.
RECAPTCHA_SITE_KEY = "6LeCXbUUAAAAALp9bXMEorp7ONUX1cB1LwKoXeUY"
HCAPTCHA_SITE_KEY = "b3f14452-58e2-4030-82ae-9d4647a77b88"
# The site still sends the hCaptcha token under the legacy reCAPTCHA parameter name.
CAPTCHA_TOKEN_PARAM = "recaptchaToken"
UUID_PATTERN = r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"

QUEUE_PREFIXES = ("A", "B", "C")
SERVICE_NAME_TEMPLATE = "Wydawanie dokumentów (karty pobytu, zaproszenia) {prefix}"
API_TIMEOUT_MS = 30_000
SAFE_GET_RETRIES = 2
CAPTCHA_MINT_SPACING_SECONDS = 3.0
HCAPTCHA_READY_TIMEOUT_MS = 20_000
HCAPTCHA_EXECUTE_TIMEOUT_MS = 120_000
# Substrings hCaptcha reports when it decides an interactive challenge is required
# (or the user/automation failed to solve one) rather than issuing a passive token.
HCAPTCHA_CHALLENGE_MARKERS = (
    "challenge-expired",
    "challenge-closed",
    "challenge-error",
    "rate-limited",
    "network-error",
)
# Outage alert escalation is capped so a long outage cannot spam the chat.
MAX_ALERT_LEVEL = 3
STILL_DOWN_REMINDER_SECONDS = 6 * 3600
# Adaptive backoff applied while captcha challenges keep blocking reads.
CAPTCHA_BACKOFF_MULTIPLIER = 2.0
QUEUE_STAGGER_SECONDS = 5.0
SUPPORTED_CAPTCHA_SOLVERS = ("none", "capsolver", "2captcha")
TELEGRAM_RETRY_SECONDS = (5, 30, 120)
EVENT_RETENTION_DAYS = 7
EVENT_ROW_CAP = 10_000
