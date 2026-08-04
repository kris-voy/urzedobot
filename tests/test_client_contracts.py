import asyncio
import json
from pathlib import Path

import pytest

from client import (
    BezkolejkiClient,
    parse_availability,
    parse_catalog,
    parse_retry_after,
    parse_slots,
)
from config import Config
from errors import (
    ApiError,
    CaptchaChallengeError,
    CaptchaContractError,
    CaptchaError,
    RateLimitedError,
    SchemaError,
)

FIXTURES = Path(__file__).parent / "fixtures"


def load(name):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def config():
    return Config(
        mode="notify_only",
        auto_confirm=False,
        telegram_bot_token="token",
        telegram_chat_id="1",
        queues=["A", "B", "C"],
        database_path="unused.db",
    )


def test_catalog_discovers_verified_abc():
    catalog = parse_catalog(load("catalog.json"), ["A", "B", "C"])
    assert {key: value["id"] for key, value in catalog.items()} == {
        "A": 3213864,
        "B": 3219596,
        "C": 3219597,
    }


@pytest.mark.parametrize("field,value", [
    ("prefix", "Z"),
    ("name", "Wrong service"),
    ("isReservationActive", False),
])
def test_catalog_rejects_missing_or_mismatched_contract(field, value):
    payload = load("catalog.json")
    payload[0]["queues"][0]["operations"][0][field] = value
    with pytest.raises(SchemaError):
        parse_catalog(payload, ["A", "B", "C"])


def test_empty_and_populated_availability():
    assert parse_availability(load("empty_availability.json"), 3213864) == []
    assert len(parse_availability(load("populated_availability.json"), 3213864)) == 2


def test_wrong_operation_and_malformed_availability_are_failures():
    payload = load("empty_availability.json")
    with pytest.raises(SchemaError, match="operationId"):
        parse_availability(payload, 999)
    del payload["disabledDays"]
    with pytest.raises(SchemaError, match="captured schema"):
        parse_availability(payload, 3213864)


@pytest.mark.parametrize("payload", [
    [{"id": 1, "dateTime": "2026-07-18T08:00:00+02:00"}],
    {"availableSlots": [{"id": 1, "dateTime": "2026-07-18T08:00:00+02:00"}]},
    {"slots": [{"id": 1, "dateTime": "2026-07-18T08:00:00+02:00"}]},
    {"times": [{"id": 1, "dateTime": "2026-07-18T08:00:00+02:00"}]},
])
def test_supported_slot_envelopes(payload):
    assert parse_slots(payload)[0]["id"] == 1


@pytest.mark.parametrize("payload", [
    {},
    {"slots": "not-an-array"},
    [{"id": 1}],
    {"slots": [], "times": []},
])
def test_slot_schema_rejection(payload):
    with pytest.raises(SchemaError):
        parse_slots(payload)


def test_safe_get_obeys_retry_after(monkeypatch):
    client = BezkolejkiClient(config())
    calls = 0
    sleeps = []

    async def request(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RateLimitedError("/test", 7)
        return {"ok": True}

    async def sleep(seconds):
        sleeps.append(seconds)

    monkeypatch.setattr(client, "_request_json", request)
    monkeypatch.setattr("client.asyncio.sleep", sleep)
    assert asyncio.run(client._safe_get("/test")) == {"ok": True}
    assert calls == 2
    assert sleeps == [7]


def test_retry_after_accepts_http_date():
    assert parse_retry_after("Wed, 21 Oct 2099 07:28:00 GMT") > 0
    assert parse_retry_after("invalid") == 5


@pytest.mark.parametrize(
    "error,expected_calls",
    [
        (ApiError("/test"), None),
        # Captcha failures are never retried: a rejected or challenged token is not
        # transient and retrying multiplies the request volume that got us blocked.
        (CaptchaError("/test", 400), 1),
        (CaptchaChallengeError("challenge-expired"), 1),
        (CaptchaContractError("no hcaptcha"), 1),
    ],
)
def test_safe_get_retries_then_surfaces_error(monkeypatch, error, expected_calls):
    client = BezkolejkiClient(config())
    calls = 0

    async def request(*args, **kwargs):
        nonlocal calls
        calls += 1
        raise error

    async def sleep(_):
        return None

    monkeypatch.setattr(client, "_request_json", request)
    monkeypatch.setattr("client.asyncio.sleep", sleep)
    with pytest.raises(type(error)):
        asyncio.run(client._safe_get("/test"))
    from constants import SAFE_GET_RETRIES
    assert calls == (expected_calls or SAFE_GET_RETRIES + 1)
