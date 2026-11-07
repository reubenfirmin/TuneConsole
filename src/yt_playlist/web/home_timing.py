"""Bounded server-side attribution for Home opening, including failed opens."""
from contextlib import contextmanager
import logging
import threading
import time

from yt_playlist.core.work_diagnostics import work_span

logger = logging.getLogger('yt_playlist.home')


class HomeTiming:
    def __init__(self):
        self.stages = {}

    @contextmanager
    def measure(self, name):
        started = time.monotonic(), time.thread_time()
        try:
            with work_span('Home: ' + name):
                yield
        finally:
            self.stages[name] = {
                'wall_s': round(time.monotonic() - started[0], 6),
                'cpu_s': round(time.thread_time() - started[1], 6)}


@contextmanager
def opening():
    timing, outcome = HomeTiming(), 'done'
    try:
        with timing.measure('total'):
            yield timing
    except BaseException:
        outcome = 'failed'
        raise
    finally:
        logger.log(logging.INFO if timing.stages['total']['wall_s'] >= .25 else logging.DEBUG,
                   'Home opening: tid=%d outcome=%s stages=%s',
                   threading.get_native_id(), outcome, timing.stages)
