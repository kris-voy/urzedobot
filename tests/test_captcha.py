import asyncio

import pytest

from captcha_solver import CaptchaSolver
from client import BezkolejkiClient, is_challenge_failure
from config import Config
from constants import HCAPTCHA_SITE_KEY
from errors import (
    CaptchaChallengeError,
    CaptchaContractError,
    CaptchaSolverError,
)


def config(**overrides):
    base = dict(
        mode="notify_only",
        auto_confirm=False,
        telegram_bot_token="token",
        telegram_chat_id="1",
        queues=["A"],
        database_path=":memory:",
    )
    base.update(overrides)
    return Config(**base)


class FakePage:
    def __init__(self, evaluate=None, wait=None):
        self._evaluate = evaluate or (lambda script, arg=None: None)
        self._wait = wait
        self.evaluated = []

    async def evaluate(self, script, arg=None):
        self.evaluated.append((script, arg))
        result = self._evaluate(script, arg)
        if isinstance(result, Exception):
            raise result
        return result

    async def wait_for_function(self, script, timeout=None):
        if isinstance(self._wait, Exception):
            raise self._wait
        return None


def client_with_page(page, **overrides):
    client = BezkolejkiClient(config(**overrides))
    client._page = page
    return client


def test_is_challenge_failure_detects_hcaptcha_markers():
    assert is_challenge_failure("challenge-expired")
    assert is_challenge_failure("Error: rate-limited")
    assert not is_challenge_failure("some unrelated failure")


def test_sitekey_discovered_from_dom():
    key = "11111111-2222-3333-4444-555555555555"

    def evaluate(script, arg=None):
        if "data-sitekey" in script:
            return [key]
        return ""

    client = client_with_page(FakePage(evaluate=evaluate))
    assert asyncio.run(client._discover_sitekey()) == key


def test_sitekey_discovered_from_config_js():
    key = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"

    def evaluate(script, arg=None):
        if "data-sitekey" in script:
            return []
        return f"window.cfg={{hcaptcha:'{key}'}};"

    client = client_with_page(FakePage(evaluate=evaluate))
    assert asyncio.run(client._discover_sitekey()) == key


def test_sitekey_falls_back_to_pinned_constant():
    client = client_with_page(FakePage(evaluate=lambda script, arg=None: []))
    assert asyncio.run(client._discover_sitekey()) == HCAPTCHA_SITE_KEY


def test_missing_hcaptcha_api_raises_contract_error():
    page = FakePage(evaluate=lambda script, arg=None: False, wait=TimeoutError())
    client = client_with_page(page)
    with pytest.raises(CaptchaContractError):
        asyncio.run(client._ensure_hcaptcha())


def test_grecaptcha_only_page_reports_vendor_swap():
    page = FakePage(evaluate=lambda script, arg=None: True, wait=TimeoutError())
    client = client_with_page(page)
    with pytest.raises(CaptchaContractError) as excinfo:
        asyncio.run(client._ensure_hcaptcha())
    assert "grecaptcha" in str(excinfo.value)


def _mint_page(execute_result):
    def evaluate(script, arg=None):
        if "data-sitekey" in script:
            return []
        if "hcaptcha.render" in script:
            return "widget-1"
        if "hcaptcha.execute" in script:
            return execute_result
        if "hcaptcha.reset" in script:
            return None
        return ""

    return FakePage(evaluate=evaluate)


def test_native_mint_returns_token():
    client = client_with_page(_mint_page({"ok": True, "token": "hc-token"}))
    assert asyncio.run(client._mint_native()) == "hc-token"


def test_challenge_expired_raises_challenge_error():
    client = client_with_page(_mint_page({"ok": False, "error": "challenge-expired"}))
    with pytest.raises(CaptchaChallengeError):
        asyncio.run(client._mint_native())


def test_unknown_execute_error_raises_contract_error():
    client = client_with_page(_mint_page({"ok": False, "error": "render is not a function"}))
    with pytest.raises(CaptchaContractError):
        asyncio.run(client._mint_native())


def test_empty_token_is_treated_as_challenge():
    client = client_with_page(_mint_page({"ok": True, "token": ""}))
    with pytest.raises(CaptchaChallengeError):
        asyncio.run(client._mint_native())


def test_captcha_without_solver_propagates_challenge():
    client = client_with_page(_mint_page({"ok": False, "error": "challenge-expired"}))
    with pytest.raises(CaptchaChallengeError):
        asyncio.run(client._captcha("action"))
    assert client.solver_engaged is False


def test_captcha_falls_back_to_solver_when_enabled():
    class StubSolver:
        enabled = True

        def __init__(self):
            self.calls = []

        async def solve(self, sitekey, page_url):
            self.calls.append((sitekey, page_url))
            return "solved-token"

        def stats(self):
            return {"enabled": True}

    solver = StubSolver()
    client = BezkolejkiClient(config(), solver=solver)
    client._page = _mint_page({"ok": False, "error": "challenge-expired"})
    client._last_captcha_mint = 0.0
    assert asyncio.run(client._captcha("action")) == "solved-token"
    assert client.solver_engaged is True
    assert solver.calls


def test_captcha_reports_solver_failure_as_challenge():
    class FailingSolver:
        enabled = True

        async def solve(self, sitekey, page_url):
            raise CaptchaSolverError("no funds")

    client = BezkolejkiClient(config(), solver=FailingSolver())
    client._page = _mint_page({"ok": False, "error": "challenge-expired"})
    with pytest.raises(CaptchaChallengeError) as excinfo:
        asyncio.run(client._captcha("action"))
    assert "no funds" in str(excinfo.value)


def test_solver_disabled_by_default():
    solver = CaptchaSolver("none", "")
    assert solver.enabled is False
    with pytest.raises(CaptchaSolverError):
        asyncio.run(solver.solve("key", "https://example.com"))


def test_solver_requires_api_key_to_enable():
    assert CaptchaSolver("capsolver", "").enabled is False
    assert CaptchaSolver("capsolver", "abc").enabled is True


def test_solver_rejects_unknown_provider():
    with pytest.raises(ValueError):
        CaptchaSolver("magic", "abc")


def test_solver_enforces_hourly_cap():
    clock = [1000.0]
    solver = CaptchaSolver("capsolver", "key", max_per_hour=2, clock=lambda: clock[0])
    solver._solve_sync = lambda sitekey, page_url: "tok"
    assert asyncio.run(solver.solve("k", "u")) == "tok"
    assert asyncio.run(solver.solve("k", "u")) == "tok"
    with pytest.raises(CaptchaSolverError) as excinfo:
        asyncio.run(solver.solve("k", "u"))
    assert "hourly cap" in str(excinfo.value)
    # The cap is a rolling hour, so capacity returns once the window slides.
    clock[0] += 3601
    assert asyncio.run(solver.solve("k", "u")) == "tok"


def test_solver_stats_track_outcomes():
    solver = CaptchaSolver("capsolver", "key")
    solver._solve_sync = lambda sitekey, page_url: ""
    with pytest.raises(CaptchaSolverError):
        asyncio.run(solver.solve("k", "u"))
    stats = solver.stats()
    assert stats["total_calls"] == 1
    assert stats["total_failed"] == 1
    assert stats["total_solved"] == 0
