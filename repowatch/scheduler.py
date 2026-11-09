"""Collection loop for installations without an external cron service."""

from __future__ import annotations

import logging
import signal
from threading import Event
from typing import Callable

log = logging.getLogger("repowatch.scheduler")


def collection_loop(collect_once: Callable[[], int], interval: int, stop: Event) -> int:
    """Collect immediately, then wait after each run; failures don't disable future runs."""
    if interval < 60:
        raise ValueError("COLLECT_INTERVAL_SECONDS must be at least 60")
    while not stop.is_set():
        try:
            status = collect_once()
            if status:
                log.warning("collection finished with exit code %s; next attempt in %ss", status, interval)
        except Exception:
            log.exception("collection failed; next attempt in %ss", interval)
        stop.wait(interval)
    return 0


def run(collect_once: Callable[[], int], interval: int) -> int:
    stop = Event()
    previous = {}
    for sig in (signal.SIGTERM, signal.SIGINT):
        previous[sig] = signal.signal(sig, lambda *_: stop.set())
    try:
        return collection_loop(collect_once, interval, stop)
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)
