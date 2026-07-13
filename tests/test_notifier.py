import types

from config import Config
from notifier import TelegramNotifier


def make_notifier():
    config = Config(
        telegram_bot_token="t",
        telegram_chat_id="123",
        queues=["A"],
        mode="notify_only",
        form_data={},
    )
    return TelegramNotifier(config)


def test_is_authorized_matching_chat_id():
    notifier = make_notifier()
    update = types.SimpleNamespace(effective_chat=types.SimpleNamespace(id=123))
    assert notifier._is_authorized(update) is True


def test_is_authorized_non_matching_chat_id():
    notifier = make_notifier()
    update = types.SimpleNamespace(effective_chat=types.SimpleNamespace(id=999))
    assert notifier._is_authorized(update) is False


def test_is_authorized_no_effective_chat():
    notifier = make_notifier()
    update = types.SimpleNamespace(effective_chat=None)
    assert notifier._is_authorized(update) is False
