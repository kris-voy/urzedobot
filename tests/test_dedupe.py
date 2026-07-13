from dedupe import DedupeTracker


def test_first_sighting_notifies():
    tracker = DedupeTracker(window_seconds=1800)
    assert tracker.should_notify("A", "2026-07-14") is True


def test_repeat_within_window_is_suppressed(monkeypatch):
    times = iter([100.0, 100.5, 200.0])
    monkeypatch.setattr("dedupe.time.monotonic", lambda: next(times))
    tracker = DedupeTracker(window_seconds=1800)
    assert tracker.should_notify("A", "2026-07-14") is True   # t=100.0, records it
    assert tracker.should_notify("A", "2026-07-14") is False  # t=100.5, within window
    assert tracker.should_notify("A", "2026-07-14") is False  # t=200.0, still within window


def test_repeat_after_window_notifies_again(monkeypatch):
    times = iter([100.0, 2000.0])
    monkeypatch.setattr("dedupe.time.monotonic", lambda: next(times))
    tracker = DedupeTracker(window_seconds=1800)
    assert tracker.should_notify("A", "2026-07-14") is True  # t=100.0
    assert tracker.should_notify("A", "2026-07-14") is True  # t=2000.0, 1900s later > window


def test_different_keys_are_independent():
    tracker = DedupeTracker(window_seconds=1800)
    assert tracker.should_notify("A", "2026-07-14") is True
    assert tracker.should_notify("B", "2026-07-14") is True
    assert tracker.should_notify("A", "2026-07-15") is True
