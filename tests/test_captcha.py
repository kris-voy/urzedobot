import asyncio

import pytest

from captcha import BrowserCaptchaProvider, TwoCaptchaProvider, resolve_captcha_provider
from config import Config
from errors import GrabPipelineError


def make_config(**overrides):
    defaults = dict(
        telegram_bot_token="test-token",
        telegram_chat_id="12345",
        queues=["A"],
        mode="notify_only",
        form_data={},
    )
    defaults.update(overrides)
    return Config(**defaults)


def test_auto_with_key_resolves_to_twocaptcha():
    cfg = make_config(captcha_provider="auto", twocaptcha_api_key="abc123")
    provider = resolve_captcha_provider(cfg)
    assert isinstance(provider, TwoCaptchaProvider)


def test_auto_without_key_resolves_to_browser():
    cfg = make_config(captcha_provider="auto", twocaptcha_api_key="")
    provider = resolve_captcha_provider(cfg)
    assert isinstance(provider, BrowserCaptchaProvider)


def test_explicit_browser_ignores_key():
    cfg = make_config(captcha_provider="browser", twocaptcha_api_key="abc123")
    provider = resolve_captcha_provider(cfg)
    assert isinstance(provider, BrowserCaptchaProvider)


def test_explicit_twocaptcha_resolves_to_twocaptcha():
    cfg = make_config(captcha_provider="2captcha", twocaptcha_api_key="abc123")
    provider = resolve_captcha_provider(cfg)
    assert isinstance(provider, TwoCaptchaProvider)


def test_explicit_twocaptcha_with_no_key_still_resolves_to_twocaptcha():
    # Misconfiguration (explicit "2captcha" but no key set) is deferred to
    # mint()-time, not resolution-time -- matches the original code, which
    # only ever raised when a mint was actually attempted.
    cfg = make_config(captcha_provider="2captcha", twocaptcha_api_key="")
    provider = resolve_captcha_provider(cfg)
    assert isinstance(provider, TwoCaptchaProvider)


def test_twocaptcha_mint_raises_without_network_when_key_empty():
    provider = TwoCaptchaProvider(api_key="", min_score=0.3)

    async def run():
        await provider.mint(page=None, action="grab")

    with pytest.raises(GrabPipelineError, match="TWOCAPTCHA_API_KEY is empty"):
        asyncio.run(run())
