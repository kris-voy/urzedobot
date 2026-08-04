class WatcherError(Exception):
    """Base class for expected watcher failures."""


class ApiError(WatcherError):
    def __init__(self, endpoint: str, status: int | None = None):
        self.endpoint = endpoint
        self.status = status
        suffix = f" (HTTP {status})" if status is not None else ""
        super().__init__(f"API request failed: {endpoint}{suffix}")


class CaptchaError(ApiError):
    """The server rejected a CAPTCHA-protected read request."""


class CaptchaContractError(CaptchaError):
    """The page no longer exposes the CAPTCHA API we know how to drive.

    This means the site changed CAPTCHA vendor or integration and the watcher is
    blind until the code is updated. It is never transient, so it is alerted
    separately and loudly.
    """

    def __init__(self, detail: str, endpoint: str = "captcha", status: int | None = None):
        self.detail = detail
        super().__init__(endpoint, status)
        self.args = (f"CAPTCHA contract changed: {detail}",)

    def __str__(self) -> str:
        return f"CAPTCHA contract changed: {self.detail}"


class CaptchaChallengeError(CaptchaError):
    """CAPTCHA served an interactive challenge instead of a passive token.

    Usually a reputation signal (IP, request volume, fingerprint) rather than a
    code defect, so it drives adaptive backoff and the optional solver fallback.
    """

    def __init__(self, detail: str, endpoint: str = "captcha", status: int | None = None):
        self.detail = detail
        super().__init__(endpoint, status)
        self.args = (f"CAPTCHA challenge required: {detail}",)

    def __str__(self) -> str:
        return f"CAPTCHA challenge required: {self.detail}"


class CaptchaSolverError(CaptchaError):
    """The configured third-party CAPTCHA solver could not produce a token."""

    def __init__(self, detail: str):
        self.detail = detail
        super().__init__("captcha-solver", None)
        self.args = (f"CAPTCHA solver failed: {detail}",)

    def __str__(self) -> str:
        return f"CAPTCHA solver failed: {self.detail}"


class RateLimitedError(ApiError):
    def __init__(self, endpoint: str, retry_after: float):
        self.retry_after = retry_after
        super().__init__(endpoint, 429)


class SchemaError(WatcherError):
    """A public API response no longer matches the verified contract."""
