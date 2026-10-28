"""Cooperative maintenance scheduling: interactive work goes first.

Checkpoint waits never run while holding the serving connection's lock. A pause at that point
would deadlock the very request we are trying to prioritize. Long calculations belong on detached
readers, whose SQLite progress callbacks can safely yield.
"""
from contextlib import contextmanager
from contextvars import ContextVar
import threading
import time

from yt_playlist.core.db_diagnostics import (
    LockWaitDiagnostics, cooperative_lock_yield, database_lock_wait,
)


_BACKGROUND = ContextVar("tuneconsole_background_priority", default=None)


class RequestPriority:
    def __init__(self):
        self._condition = threading.Condition()
        self._foreground = 0
        self._held = threading.local()

    def enter(self):
        with self._condition:
            self._foreground += 1
        released = False

        def release():
            nonlocal released
            with self._condition:
                if not released:
                    released = True
                    self._foreground -= 1
                    self._condition.notify_all()

        return release

    def checkpoint(self, should_stop):
        if getattr(self._held, "depth", 0):
            return
        # A request can itself await a model/source mutation. Yield a bounded timeslice rather
        # than waiting for that response indefinitely (which would introduce a dependency cycle).
        deadline = time.monotonic() + 0.1
        with self._condition:
            while self._foreground and not should_stop():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return
                with cooperative_lock_yield():
                    self._condition.wait(min(0.025, remaining))

    @contextmanager
    def background(self, checkpoint):
        token = _BACKGROUND.set((self, checkpoint))
        try:
            yield
        finally:
            _BACKGROUND.reset(token)


def yield_to_requests(should_stop):
    background = _BACKGROUND.get()
    if background is not None:
        background[0].checkpoint(should_stop)


class ServingLock:
    """Reentrant database lock that makes maintenance yield to foreground waiters."""
    def __init__(self, priority):
        self._priority = priority
        self._lock = threading.RLock()
        self._local = threading.local()
        self._diagnostics = LockWaitDiagnostics()

    @property
    def held_by_current_thread(self):
        return bool(getattr(self._local, 'releases', ()))

    def acquire(self, blocking=True, timeout=-1):
        background = _BACKGROUND.get()
        return database_lock_wait(self._acquire, blocking, timeout,
            background=background is not None and background[0] is self._priority,
            diagnostics=self._diagnostics)

    def _acquire(self, blocking, timeout):
        priority = self._priority
        # Foreground waiters register before waiting for the connection, preventing a maintenance
        # loop from taking it repeatedly while a request is queued.
        background = _BACKGROUND.get()
        foreground = background is None or background[0] is not priority
        release = priority.enter() if foreground else None
        try:
            if not foreground:
                # Cancellation is checked by the caller's cancellable boundary. Avoid an
                # uninterruptible wait here: retry the lock through those boundaries instead.
                deadline = None if timeout < 0 else time.monotonic() + timeout
                while True:
                    background[1]()
                    if self._lock.acquire(False):
                        break
                    if not blocking or (deadline is not None and time.monotonic() >= deadline):
                        return False
                    time.sleep(0.005)
            elif not self._lock.acquire(blocking, timeout):
                release()
                return False
        except BaseException:
            if release:
                release()
            raise
        stack = getattr(self._local, "releases", [])
        self._local.releases = [*stack, release]
        priority._held.depth = getattr(priority._held, "depth", 0) + 1
        return True

    def release(self):
        stack = self._local.releases
        release = stack.pop()
        self._priority._held.depth -= 1
        self._lock.release()
        if release:
            release()

    def __enter__(self):
        self.acquire()
        return self

    def __exit__(self, *_):
        self.release()
