"""Shared exception types. Kept dependency-free so both client.py and
captcha.py can import them without creating a circular import."""


class GrabPipelineError(Exception):
    """Raised when a step in the block/fill/confirm pipeline fails."""


class RateLimitedError(Exception):
    """Raised on HTTP 429."""
