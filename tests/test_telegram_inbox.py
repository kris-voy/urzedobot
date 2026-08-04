import asyncio

from config import Config
from state import StateStore
from telegram_inbox import TelegramInbox


CATALOG = {
    prefix: {
        "id": operation_id,
        "prefix": prefix,
        "name": f"Wydawanie dokumentów (karty pobytu, zaproszenia) {prefix}",
        "isReservationActive": True,
    }
    for prefix, operation_id in {"A": 3213864, "B": 3219596, "C": 3219597}.items()
}


def setup(tmp_path, fetcher, responses, info=None, **config_overrides):
    store = StateStore(str(tmp_path / "sv.db"))
    store.initialize()
    store.start_runtime()
    store.save_catalog(CATALOG)
    for prefix in CATALOG:
        store.record_attempt(prefix)
        store.record_success(prefix, 0, None)
    store.finish_cycle(True)
    options = dict(
        mode="notify_only",
        auto_confirm=False,
        telegram_bot_token="token",
        telegram_chat_id="123",
        queues=["A", "B", "C"],
        database_path=store.path,
        telegram_poll_timeout_seconds=1,
        telegram_recheck_wait_seconds=2,
    )
    options.update(config_overrides)
    config = Config(**options)
    inbox = TelegramInbox(
        config,
        store,
        response_writer=lambda kind, key, text: responses.append((kind, key, text)) or True,
        fetcher=fetcher,
        info_provider=(lambda: info) if info is not None else None,
    )
    return store, inbox


def update(update_id, chat_id, text):
    return {"update_id": update_id, "message": {"chat": {"id": chat_id}, "text": text}}


def test_unauthorized_commands_are_ignored(tmp_path):
    responses = []
    store, inbox = setup(tmp_path, lambda offset, timeout: [update(1, 999, "/status")], responses)
    assert asyncio.run(inbox.poll_once()) == 1
    assert responses == []
    assert store.telegram_offset() == 1
    assert any(event["kind"] == "telegram.command.unauthorized" for event in store.events())


def test_status_and_stats_commands_enqueue_replies(tmp_path):
    responses = []
    updates = [update(2, 123, "/status"), update(3, 123, "/stats")]
    store, inbox = setup(tmp_path, lambda offset, timeout: updates, responses)
    assert asyncio.run(inbox.poll_once()) == 2
    assert len(responses) == 2
    assert responses[0][0] == "command.reply"
    assert "Status: running" in responses[0][2]
    assert "A healthy" in responses[0][2]
    assert "Attempts:" in responses[1][2]
    assert "DB:" in responses[1][2]
    assert store.telegram_offset() == 3


def test_recheck_command_uses_command_queue_and_waits(tmp_path):
    responses = []
    store, inbox = setup(tmp_path, lambda offset, timeout: [update(4, 123, "/recheck")], responses)

    async def watcher_side():
        for _ in range(20):
            commands = store.claim_commands()
            if commands:
                store.complete_command(
                    commands[0]["id"],
                    {
                        prefix: {"status": "healthy", "available_days": 0}
                        for prefix in ("A", "B", "C")
                    },
                )
                return
            await asyncio.sleep(0.1)

    async def run_both():
        await asyncio.gather(inbox.poll_once(), watcher_side())

    asyncio.run(run_both())
    assert responses
    assert "/recheck done" in responses[0][2]


def test_polling_uses_persisted_offset(tmp_path):
    responses = []
    calls = []

    def fetcher(offset, timeout):
        calls.append(offset)
        if len(calls) == 1:
            return [update(11, 123, "/status")]
        return []

    store, inbox = setup(tmp_path, fetcher, responses)
    store.set_telegram_offset(10)
    assert asyncio.run(inbox.poll_once()) == 1
    assert asyncio.run(inbox.poll_once()) == 0
    assert calls == [11, 12]


def test_status_reports_node_and_captcha_backoff(tmp_path):
    responses = []
    info = {
        "captcha_failure_streak": 4,
        "cooldown_remaining": 900.0,
        "next_interval": 1800.0,
        "solver": {"enabled": False},
    }
    store, inbox = setup(
        tmp_path,
        lambda offset, timeout: [update(1, "123", "/status")],
        responses,
        info=info,
        node_name="rpi-home2",
    )
    asyncio.run(inbox.poll_once())
    text = responses[0][2]
    assert "Node: rpi-home2" in text
    assert "Cool-down: 900s remaining" in text
    assert "CAPTCHA streak: 4" in text


def test_stats_reports_solver_usage(tmp_path):
    responses = []
    info = {
        "solver": {
            "enabled": True,
            "provider": "capsolver",
            "total_solved": 3,
            "total_calls": 5,
            "calls_last_hour": 5,
            "max_per_hour": 60,
        }
    }
    store, inbox = setup(
        tmp_path,
        lambda offset, timeout: [update(2, "123", "/stats")],
        responses,
        info=info,
    )
    asyncio.run(inbox.poll_once())
    text = responses[0][2]
    assert "Solver: capsolver solved=3/5 hour=5/60" in text


def test_stats_reports_solver_disabled_by_default(tmp_path):
    responses = []
    store, inbox = setup(
        tmp_path, lambda offset, timeout: [update(3, "123", "/stats")], responses
    )
    asyncio.run(inbox.poll_once())
    assert "Solver: disabled" in responses[0][2]
