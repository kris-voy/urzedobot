from datetime import time as dtime

import pytest

from config import Config, is_within_fast_window, jittered, parse_fast_window


def make_config(**overrides):
    values = {
        "mode": "notify_only",
        "auto_confirm": False,
        "telegram_bot_token": "test-token",
        "telegram_chat_id": "123",
        "queues": ["A", "B", "C"],
        "database_path": "test.db",
    }
    values.update(overrides)
    return Config(**values)


def test_notify_only_is_the_only_mode():
    assert make_config().mode == "notify_only"
    for mode in ("block", "block_and_fill"):
        with pytest.raises(ValueError, match="notify_only"):
            make_config(mode=mode)


def test_auto_confirm_is_rejected():
    with pytest.raises(ValueError, match="AUTO_CONFIRM"):
        make_config(auto_confirm=True)


def test_telegram_and_queue_validation():
    with pytest.raises(ValueError, match="TELEGRAM_BOT_TOKEN"):
        make_config(telegram_bot_token="")
    with pytest.raises(ValueError, match="QUEUES"):
        make_config(queues=["A", "Z"])


def test_window_and_jitter_helpers():
    assert parse_fast_window("05:45-08:45") == (dtime(5, 45), dtime(8, 45))
    assert is_within_fast_window("22:00-02:00", dtime(1, 0))
    with pytest.raises(ValueError):
        parse_fast_window("25:00-08:00")
    for _ in range(100):
        assert 65 <= jittered(100, 0.35) <= 135


def test_telegram_polling_settings_are_validated():
    with pytest.raises(ValueError, match="TELEGRAM_POLL_TIMEOUT_SECONDS"):
        make_config(telegram_poll_timeout_seconds=0)
    with pytest.raises(ValueError, match="TELEGRAM_POLL_BACKOFF_SECONDS"):
        make_config(telegram_poll_backoff_seconds=0)
    with pytest.raises(ValueError, match="TELEGRAM_RECHECK_WAIT_SECONDS"):
        make_config(telegram_recheck_wait_seconds=0)
