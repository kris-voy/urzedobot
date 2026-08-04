import asyncio
from datetime import datetime, timezone

from config import Config
from notifier import TelegramOutbox
from state import StateStore


def setup(tmp_path, sender):
    store = StateStore(str(tmp_path / "sv.db"))
    store.initialize()
    config = Config(
        mode="notify_only",
        auto_confirm=False,
        telegram_bot_token="token",
        telegram_chat_id="1",
        queues=["A", "B", "C"],
        database_path=store.path,
    )
    return store, TelegramOutbox(config, store, sender=sender)


def make_due(store):
    with store.connect() as db:
        db.execute(
            "UPDATE telegram_outbox SET next_attempt_at=? WHERE status='pending'",
            (datetime.now(timezone.utc).isoformat(),),
        )


def test_empty_outbox_sends_nothing(tmp_path):
    calls = []
    store, notifier = setup(tmp_path, lambda text: calls.append(text) or True)
    assert asyncio.run(notifier.deliver_once()) is False
    assert calls == []


def test_one_deduplicated_availability_alert(tmp_path):
    calls = []
    store, notifier = setup(tmp_path, lambda text: calls.append(text) or True)
    assert notifier.enqueue("availability.found", "A:slot:1", "found")
    assert not notifier.enqueue("availability.found", "A:slot:1", "found")
    assert asyncio.run(notifier.deliver_once())
    assert calls == ["found"]
    assert store.snapshot()["outbox"]["pending"] == 0


def test_delivery_retries_then_becomes_undelivered(tmp_path):
    store, notifier = setup(tmp_path, lambda text: False)
    notifier.enqueue("availability.found", "A:slot:1", "found")
    for _ in range(4):
        assert asyncio.run(notifier.deliver_once())
        make_due(store)
    assert store.snapshot()["outbox"]["undelivered"] == 1
    assert any(event["kind"] == "notification.delivery_failed" for event in store.events())
