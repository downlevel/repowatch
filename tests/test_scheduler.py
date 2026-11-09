from threading import Event

import pytest

from repowatch.scheduler import collection_loop


def test_immediate_collection_and_clean_stop():
    stop = Event()
    calls = []

    def collect():
        calls.append("collected")
        stop.set()
        return 0

    assert collection_loop(collect, 21600, stop) == 0
    assert calls == ["collected"]


def test_failed_runs_are_retried_after_interval(caplog):
    class Clock:
        waits = []

        def is_set(self):
            return len(self.waits) == 3

        def wait(self, seconds):
            self.waits.append(seconds)

    clock = Clock()
    calls = []

    def collect():
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("temporary outage")
        return 2 if len(calls) == 2 else 0

    assert collection_loop(collect, 300, clock) == 0
    assert clock.waits == [300, 300, 300]
    assert len(calls) == 3
    assert "collection failed" in caplog.text
    assert "exit code 2" in caplog.text


def test_invalid_interval_does_not_collect():
    with pytest.raises(ValueError, match="at least 60"):
        collection_loop(lambda: pytest.fail("unexpected collection"), 59, Event())
