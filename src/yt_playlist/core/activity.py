"""Application work admission and draining, shared by every shutdown path."""
import asyncio
import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Callable

from yt_playlist.core.work_diagnostics import work_span
from yt_playlist.core.priority import yield_to_requests

_CURRENT_WORK = ContextVar("tuneconsole_activity", default=None)
_CANCELLATION = ContextVar("tuneconsole_cancellation", default=None)
_DEADLINE = ContextVar("tuneconsole_work_deadline", default=None)


class WorkClosing(RuntimeError):
    pass


class WorkBudgetExceeded(RuntimeError):
    """A disposable calculation used its allotted wall time."""


@dataclass
class _CancellationState:
    should_stop: Callable[[], bool]
    interrupted: bool = False


class Activity:
    def __init__(self):
        self._condition = threading.Condition()
        self._accepting = True
        self.closing_reason = None
        self._active = {}
        self._executor = None

    @property
    def accepting(self):
        with self._condition:
            return self._accepting

    @property
    def active(self):
        with self._condition:
            return tuple(self._active.values())

    def open(self):
        with self._condition:
            if self._active:
                raise RuntimeError("Cannot restart while application work is still draining")
            self._accepting = True
            self.closing_reason = None

    def close_admission(self, reason="shutdown"):
        with self._condition:
            if self._accepting or self.closing_reason == "idle":
                self.closing_reason = reason
            self._accepting = False

    def resume_idle(self):
        with self._condition:
            if self.closing_reason == "idle":
                self._accepting = True
                self.closing_reason = None

    def admit(self, name):
        with self._condition:
            parent = _CURRENT_WORK.get()
            if not self._accepting and not (parent and parent[0] is self and parent[1] in self._active):
                raise WorkClosing("TuneConsole is closing. Please wait.")
            token = object()
            self._active[token] = name

        def release():
            with self._condition:
                self._active.pop(token, None)
                self._condition.notify_all()

        release.token = token
        return release

    @contextmanager
    def entered(self, release):
        context = _CURRENT_WORK.set((self, release.token))
        try:
            yield
        finally:
            _CURRENT_WORK.reset(context)

    def wrap(self, name, target, *args, **kwargs):
        # Reserve before scheduling, so shutdown cannot pass between submission and execution.
        release = self.admit(name)

        def run():
            try:
                with self.entered(release), work_span(name):
                    return target(*args, **kwargs)
            finally:
                release()

        run.release = release
        return run

    def start_thread(self, target, *, args=(), kwargs=None, name=None):
        run = self.wrap(name or target.__name__, target, *args, **(kwargs or {}))
        thread = threading.Thread(target=run, name=name, daemon=True)
        try:
            thread.start()
        except BaseException:
            run.release()
            raise
        return thread

    async def to_thread(self, target, *args, **kwargs):
        run = self.wrap(getattr(target, "__name__", "request work"), target, *args, **kwargs)
        # Cancellation of an awaiting request must not hide a still-running remote mutation.
        task = asyncio.create_task(asyncio.to_thread(run))
        return await asyncio.shield(task)

    def start_executor(self):
        with self._condition:
            if self._executor is not None:
                return
            if not self._accepting:
                raise WorkClosing("Application is closing")
            if self._executor is None:
                self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="radio-apply")

    def submit(self, target, *args, **kwargs):
        run = self.wrap("bridge mutation", target, *args, **kwargs)
        try:
            self.start_executor()
            with self._condition:
                future = self._executor.submit(run)
        except BaseException:
            run.release()
            raise
        future.add_done_callback(lambda f: run.release() if f.cancelled() else None)
        return future

    def stop_executor(self):
        with self._condition:
            executor, self._executor = self._executor, None
        if executor is not None:
            executor.shutdown(wait=False, cancel_futures=True)

    def wait(self, timeout):
        deadline = time.monotonic() + timeout
        with self._condition:
            while self._active:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self._condition.wait(remaining)
        return True


def activity_for(ctx):
    """Also supports small explicitly assembled application contexts."""
    if not hasattr(ctx, "work"):
        ctx.work = Activity()
    return ctx.work


def check_stopping():
    """Cooperative boundary for read-only work; admitted mutations still drain normally."""
    current = _CURRENT_WORK.get()
    cancellation = _CANCELLATION.get()
    def stopped():
        return ((current is not None and current[0].closing_reason == "shutdown")
                or (cancellation is not None and cancellation.should_stop()))

    deadline = _DEADLINE.get()
    yield_to_requests(lambda: stopped() or (deadline is not None and time.monotonic() >= deadline))
    if stopped():
        raise WorkClosing("TuneConsole is shutting down; background work stopped.")
    if deadline is not None and time.monotonic() >= deadline:
        raise WorkBudgetExceeded("Background preparation exhausted its time budget")


@contextmanager
def cancellable_work(should_stop):
    """Allow disposable background calculations to stop, including inside SQLite.

    Cancellation is local to this thread/context. Other admitted work, especially
    remote mutations and their local bookkeeping, keeps draining normally.
    """
    token = _CANCELLATION.set(_CancellationState(should_stop))
    try:
        check_stopping()
        try:
            yield
        except sqlite3.OperationalError:
            check_stopping()
            raise
        check_stopping()
    finally:
        _CANCELLATION.reset(token)


def check_cancellable_work():
    if _CANCELLATION.get() is not None:
        check_stopping()


@contextmanager
def work_budget(seconds):
    """Bound an entire disposable calculation, including its SQLite setup queries."""
    previous = _CANCELLATION.get()
    token = _DEADLINE.set(time.monotonic() + seconds)
    try:
        with cancellable_work(previous.should_stop if previous else lambda: False):
            yield
    finally:
        _DEADLINE.reset(token)


def interrupt_query():
    """SQLite progress callback: interrupt only the current cancellable calculation.

    Fire once per scope so transaction cleanup can execute after interruption.
    Further repository calls encounter check_cancellable_work before executing SQL.
    """
    cancellation = _CANCELLATION.get()
    if cancellation is not None and not cancellation.interrupted:
        try:
            check_stopping()
        except (WorkClosing, WorkBudgetExceeded):
            cancellation.interrupted = True
            return 1
    return 0
