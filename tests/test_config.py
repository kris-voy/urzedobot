from datetime import time as dtime

import pytest

from config import Config, is_within_fast_window, jittered, parse_fast_window


def make_config(**overrides):
    defaults = dict(
        telegram_bot_token="test-token",
        telegram_chat_id="12345",
        queues=["A"],
        mode="notify_only",
        captcha_provider="browser",
        form_data={},
    )
    defaults.update(overrides)
    return Config(**defaults)


def test_parse_fast_window_valid():
    start, end = parse_fast_window("05:45-08:45")
    assert start == dtime(5, 45)
    assert end == dtime(8, 45)


@pytest.mark.parametrize("bad", ["", "not-a-window", "25:00-08:00", "05:45"])
def test_parse_fast_window_invalid(bad):
    with pytest.raises(ValueError):
        parse_fast_window(bad)


def test_is_within_fast_window_simple_range():
    window = "05:45-08:45"
    assert is_within_fast_window(window, now=dtime(6, 0)) is True
    assert is_within_fast_window(window, now=dtime(5, 45)) is True
    assert is_within_fast_window(window, now=dtime(8, 45)) is True
    assert is_within_fast_window(window, now=dtime(9, 0)) is False
    assert is_within_fast_window(window, now=dtime(4, 0)) is False


def test_is_within_fast_window_wraps_midnight():
    window = "22:00-02:00"
    assert is_within_fast_window(window, now=dtime(23, 0)) is True
    assert is_within_fast_window(window, now=dtime(1, 0)) is True
    assert is_within_fast_window(window, now=dtime(12, 0)) is False


def test_jittered_stays_within_bounds():
    for _ in range(200):
        value = jittered(100.0, pct=0.35)
        assert 65.0 <= value <= 135.0


def test_jittered_floor_is_one_second():
    # A tiny base with large pct could go negative without the floor.
    for _ in range(200):
        assert jittered(1.0, pct=0.9) >= 1.0


def test_config_valid_minimal():
    cfg = make_config()
    assert cfg.queues == ["A"]
    assert cfg.mode == "notify_only"


def test_config_missing_token_raises():
    with pytest.raises(ValueError, match="TELEGRAM_BOT_TOKEN"):
        make_config(telegram_bot_token="")


def test_config_missing_chat_id_raises():
    with pytest.raises(ValueError, match="TELEGRAM_CHAT_ID"):
        make_config(telegram_chat_id="")


def test_config_empty_queues_raises():
    with pytest.raises(ValueError, match="QUEUES is empty"):
        make_config(queues=[])


def test_config_invalid_queue_letter_raises():
    with pytest.raises(ValueError, match="Invalid queue letters"):
        make_config(queues=["A", "Z"])


def test_config_invalid_mode_raises():
    with pytest.raises(ValueError, match="Invalid MODE"):
        make_config(mode="delete_everything")


def test_config_invalid_captcha_provider_raises():
    with pytest.raises(ValueError, match="Invalid CAPTCHA_PROVIDER"):
        make_config(captcha_provider="magic")


def test_sentry_disabled_by_default():
    cfg = make_config()
    assert cfg.sentry_dsn == ""
    assert cfg.sentry_environment == "production"


def test_sentry_dsn_and_environment_are_configurable():
    cfg = make_config(sentry_dsn="https://example@o0.ingest.sentry.io/1", sentry_environment="proxmox")
    assert cfg.sentry_dsn == "https://example@o0.ingest.sentry.io/1"
    assert cfg.sentry_environment == "proxmox"
