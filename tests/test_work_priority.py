"""Interactive work gets maintenance's next cooperative timeslice without deadlocking SQL."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
import threading

import pytest
from threadpoolctl import threadpool_info

from yt_playlist.core.activity import WorkClosing, cancellable_work, check_stopping
from yt_playlist.core.numerics import limit_native_threads
from yt_playlist.core.priority import RequestPriority, ServingLock
from yt_playlist.web.request_priority import RequestPriorityMiddleware


def test_maintenance_waits_for_request_and_resumes_after_response():
    priority = RequestPriority()
    entered, continued, cancelled = threading.Event(), threading.Event(), threading.Event()
    release = priority.enter()

    def calculate():
        entered.set()
        with priority.background(check_stopping), cancellable_work(cancelled.is_set):
            check_stopping()
            continued.set()

    with ThreadPoolExecutor(1) as pool:
        task = pool.submit(calculate)
        try:
            assert entered.wait(2)
            assert not continued.wait(0.05)
            release()
            task.result(2)
            assert continued.is_set()
        finally:
            cancelled.set()
            release()


def test_shutdown_interrupts_a_maintenance_task_waiting_for_request():
    priority = RequestPriority()
    stop = threading.Event()
    release = priority.enter()

    def calculate():
        with priority.background(check_stopping), cancellable_work(stop.is_set):
            pytest.fail("cancelled maintenance ran")

    with ThreadPoolExecutor(1) as pool:
        task = pool.submit(calculate)
        try:
            stop.set()
            with pytest.raises(WorkClosing):
                task.result(2)
        finally:
            release()


def test_checkpoint_inside_serving_transaction_cannot_deadlock_foreground():
    priority = RequestPriority()
    lock = ServingLock(priority)
    owned, foreground, transaction_done, stop = (threading.Event() for _ in range(4))

    def publish():
        with priority.background(check_stopping), cancellable_work(stop.is_set), lock:
            owned.set()
            assert foreground.wait(2)
            # Nested repository calls and SQLite progress callbacks must finish the transaction
            # even after a waiting request announces priority.
            with lock:
                check_stopping()
            transaction_done.set()

    with ThreadPoolExecutor(1) as pool:
        task = pool.submit(publish)
        try:
            assert owned.wait(2)
            release = priority.enter()
            try:
                foreground.set()
                assert transaction_done.wait(2)
                with lock:
                    pass
            finally:
                release()
            task.result(2)
        finally:
            foreground.set()
            stop.set()


def test_response_headers_release_priority_even_for_long_lived_stream():
    priority = RequestPriority()
    proceeded = threading.Event()
    stop = threading.Event()

    def maintenance():
        with priority.background(check_stopping), cancellable_work(stop.is_set):
            check_stopping()
            proceeded.set()

    async def app(scope, receive, send):
        assert priority._foreground == 1
        await send({"type": "http.response.start", "status": 200, "headers": []})
        with ThreadPoolExecutor(1) as pool:
            try:
                pool.submit(maintenance).result(2)
            finally:
                stop.set()
        assert proceeded.is_set()
        await send({"type": "http.response.body", "body": b"event", "more_body": True})

    async def send(_):
        pass

    asyncio.run(RequestPriorityMiddleware(app, priority)({"type": "http"}, None, send))
    assert priority._foreground == 0


def test_numerical_limit_applies_to_numpy_and_scipy_in_worker_thread():
    def inspect_limits():
        limit_native_threads()
        return threadpool_info()

    with ThreadPoolExecutor(1) as pool:
        libraries = pool.submit(inspect_limits).result(5)
    assert any(library["user_api"] == "blas" for library in libraries)
    assert all(library["num_threads"] == 1 for library in libraries)
