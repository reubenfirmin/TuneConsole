"""Cooperative maintenance scheduling: interactive work goes first.

Checkpoint waits never run while holding the serving connection's lock. A pause at that point
would deadlock the very request we are trying to prioritize. Long calculations belong on detached
readers, whose SQLite progress callbacks can safely yield.
"""
from contextlib import contextmanager
from contextvars import ContextVar
import threading
import time

from yt_playlist.core.work_errors import WorkBudgetExceeded

from yt_playlist.core.lock_diagnostics import (
    LockWaitDiagnostics, cooperative_lock_yield, database_lock_wait,
)


_BACKGROUND = ContextVar("tuneconsole_background_priority", default=None)
_REQUEST = ContextVar("tuneconsole_request_priority", default=None)


class _RequestClaim:
    """One HTTP owner's priority, suspended while it awaits tracked worker work."""
    def __init__(self, priority):
        self.priority = priority
        self._release = priority.enter()
        self._closed = False
        self._waiting = 0

    def close(self):
        with self.priority._condition:
            self._closed = True
            if self._release is not None:
                self._release()
                self._release = None

    @contextmanager
    def waiting(self):
        with self.priority._condition:
            self._waiting += 1
            if self._release is not None:
                self._release()
                self._release = None
        try:
            yield
        finally:
            with self.priority._condition:
                self._waiting -= 1
                if not self._waiting and not self._closed:
                    self._release = self.priority.enter()


class RequestPriority:
    def __init__(self):
        self._condition = threading.Condition()
        self._foreground = 0
        self._held = threading.local()

    @property
    def is_background(self):
        background = _BACKGROUND.get()
        return background is not None and background[0] is self

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

    @contextmanager
    def request(self):
        claim = _RequestClaim(self)
        token = _REQUEST.set(claim)
        try:
            yield claim.close
        finally:
            claim.close()
            _REQUEST.reset(token)

    def checkpoint(self, should_stop):
        if getattr(self._held, "depth", 0):
            return
        cpu = time.thread_time()
        resume = getattr(self._held, 'resume_cpu_until', 0)
        if self._foreground:
            # A previously quiet worker may still have most of its longer
            # slice. New traffic gets the shorter allowance immediately.
            resume = min(resume, cpu + 0.005)
            self._held.resume_cpu_until = resume
        if cpu < resume:
            return
        # Continuing foreground traffic must still leave workers bounded opportunities to run.
        deadline = time.monotonic() + 0.1
        with self._condition:
            while self._foreground and not should_stop():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                with cooperative_lock_yield():
                    self._condition.wait(min(0.025, remaining))
            # Progress callbacks run every 1,000 VM instructions. Yielding on
            # each callback starves even short queries under continuing traffic.
            # Under interactive load, several workers sharing a 50 ms interval
            # can consume most of a response's CPU budget. Keep their progress
            # bounded to a shorter interval while a request is active, and
            # restore the normal interval immediately after traffic clears.
            # Descheduling and other threads' work must not consume this
            # worker's allowance and force another pause before useful work.
            # Cancellation and the enclosing work budget still use wall time.
            self._held.resume_cpu_until = time.thread_time() + (0.005 if self._foreground else 0.05)

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


@contextmanager
def waiting_for_worker():
    """Relinquish only the awaiting request's claim; other owners keep priority."""
    claim = _REQUEST.get()
    if claim is None:
        yield
    else:
        with claim.waiting():
            yield


class ServingLock:
    """Reentrant database lock that makes maintenance yield to foreground waiters."""
    def __init__(self, priority):
        self._priority = priority
        self._lock = threading.RLock()
        self._local = threading.local()
        self._diagnostics = LockWaitDiagnostics()
        self._foreground_waiters = 0

    @property
    def held_by_current_thread(self):
        return bool(getattr(self._local, 'releases', ()))

    @contextmanager
    def nonblocking_admission(self):
        """Require each acquisition in this thread's scope to succeed immediately.

        This sets admission policy only; it does not hold the connection across
        work between acquisitions, such as durable evidence-file publication.
        Nested repository/context-manager entries inherit the same policy.
        """
        previous = getattr(self._local, 'nonblocking_depth', 0)
        self._local.nonblocking_depth = previous + 1
        try:
            yield
        finally:
            self._local.nonblocking_depth = previous

    def acquire(self, blocking=True, timeout=-1):
        if getattr(self._local, 'nonblocking_depth', 0):
            blocking, timeout = False, -1
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
                    # A queued request gets this connection before another
                    # background batch. The priority timeslice alone cannot
                    # provide that handoff after a slow transaction commits.
                    with priority._condition:
                        if (not self._foreground_waiters or self.held_by_current_thread) and self._lock.acquire(False):
                            break
                    if not blocking or (deadline is not None and time.monotonic() >= deadline):
                        return False
                    time.sleep(0.005)
            else:
                with priority._condition:
                    self._foreground_waiters += 1
                try:
                    if not self._lock.acquire(blocking, timeout):
                        release()
                        return False
                finally:
                    with priority._condition:
                        self._foreground_waiters -= 1
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
        if not self.acquire():
            raise WorkBudgetExceeded('Serving connection is busy')
        return self

    def __exit__(self, *_):
        self.release()
