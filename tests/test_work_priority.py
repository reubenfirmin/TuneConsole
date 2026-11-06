"""Interactive work gets maintenance's next cooperative timeslice without deadlocking SQL."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
import json
import threading
import time
from types import SimpleNamespace

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


def test_background_sql_finishes_under_continuing_foreground_traffic(file_backed_store, monkeypatch):
    from yt_playlist.core.activity import work_budget
    from yt_playlist.core import priority as scheduler

    store = file_backed_store
    release = store.priority.enter()
    try:
        with store.priority.background(check_stopping), work_budget(2), store.read_view() as reader:
            total = reader.conn.execute('WITH RECURSIVE n(x) AS (VALUES(0) UNION ALL '
                'SELECT x+1 FROM n WHERE x<10000) SELECT SUM(x) FROM n').fetchone()[0]
            assert total == 50005000
            assert store.priority._foreground == 1, 'The foreground request must remain active'
    finally:
        release()

    # Descheduling or reader I/O must not spend the CPU allowance granted after
    # a yield. Still yield again once that short allowance has actually run.
    wall, cpu, waits = [100.0], [10.0], []
    priority = RequestPriority()

    def wait(seconds):
        waits.append(seconds)
        wall[0] += seconds

    with monkeypatch.context() as patch:
        patch.setattr(scheduler, 'time', SimpleNamespace(
            monotonic=lambda: wall[0], thread_time=lambda: cpu[0]))
        patch.setattr(priority._condition, 'wait', wait)
        release = priority.enter()
        try:
            priority.checkpoint(lambda: False)
            initial_waits = len(waits)
            assert initial_waits > 0
            wall[0] += .5
            priority.checkpoint(lambda: False)
            assert len(waits) == initial_waits, 'No CPU ran during the scheduling delay'
            cpu[0] += .006
            priority.checkpoint(lambda: False)
            assert len(waits) > initial_waits, 'Foreground work still gets the next timeslice'
        finally:
            release()


def test_descheduling_does_not_consume_a_background_cpu_slice(monkeypatch):
    from yt_playlist.core import priority as scheduling

    clock = {'wall': 100., 'cpu': 10.}
    waits = []
    priority = RequestPriority()

    def wait(seconds):
        waits.append(seconds)
        clock['wall'] += seconds

    monkeypatch.setattr(scheduling, 'time', SimpleNamespace(
        monotonic=lambda: clock['wall'], thread_time=lambda: clock['cpu']))
    monkeypatch.setattr(priority._condition, 'wait', wait)
    release = priority.enter()
    try:
        priority.checkpoint(lambda: False)
        first_wait = sum(waits)
        assert .099 <= first_wait <= .101
        # Other threads used this wall time. This worker has done only 1 ms of
        # its 5 ms slice, so another mandatory 100 ms pause would starve it.
        clock['wall'] += .3
        clock['cpu'] += .001
        priority.checkpoint(lambda: False)
        assert sum(waits) == first_wait
        clock['cpu'] += .005
        priority.checkpoint(lambda: False)
        assert .199 <= sum(waits) <= .201
    finally:
        release()


def test_cpu_slice_does_not_extend_the_wall_deadline(monkeypatch):
    from yt_playlist.core import activity, priority as scheduling

    clock = {'wall': 100., 'cpu': 10.}
    priority = RequestPriority()

    def wait(seconds):
        clock['wall'] += seconds

    monkeypatch.setattr(scheduling, 'time', SimpleNamespace(
        monotonic=lambda: clock['wall'], thread_time=lambda: clock['cpu']))
    monkeypatch.setattr(activity, 'time', SimpleNamespace(monotonic=lambda: clock['wall']))
    monkeypatch.setattr(priority._condition, 'wait', wait)
    release = priority.enter()
    try:
        with pytest.raises(activity.WorkBudgetExceeded):
            with priority.background(check_stopping), activity.work_budget(2):
                clock['wall'] += 2.01
                check_stopping()
    finally:
        release()


def test_new_foreground_traffic_caps_an_unspent_idle_cpu_slice(monkeypatch):
    from yt_playlist.core import priority as scheduling

    clock = {'wall': 100., 'cpu': 10.}
    waits = []
    priority = RequestPriority()

    def wait(seconds):
        waits.append(seconds)
        clock['wall'] += seconds

    monkeypatch.setattr(scheduling, 'time', SimpleNamespace(
        monotonic=lambda: clock['wall'], thread_time=lambda: clock['cpu']))
    monkeypatch.setattr(priority._condition, 'wait', wait)
    priority.checkpoint(lambda: False)
    release = priority.enter()
    try:
        clock['wall'] += .001
        clock['cpu'] += .001
        priority.checkpoint(lambda: False)
        clock['wall'] += .006
        clock['cpu'] += .006
        priority.checkpoint(lambda: False)
        assert .099 <= sum(waits) <= .101
    finally:
        release()


@pytest.mark.parametrize('competitors', [0, 2, 4])
def test_background_sql_progress_with_foreground_reads_and_competing_work(
        file_backed_store, record_property, competitors):
    from yt_playlist.core.activity import work_budget

    store = file_backed_store
    stop = threading.Event()
    traffic, calculations = [], []
    document = json.dumps([{'title': f'Other local work {i}', 'values': list(range(40))}
                           for i in range(64)])

    def compete():
        while not stop.is_set():
            assert len(json.loads(json.dumps(json.loads(document)))) == 64
            calculations.append(1)
            stop.wait(.001)

    def foreground():
        while not stop.is_set():
            assert store.settings.get_setting('priority-progress-probe') == 'available'
            traffic.append(1)
            stop.wait(.002)

    store.settings.set_setting('priority-progress-probe', 'available')
    release = store.priority.enter()
    with ThreadPoolExecutor(competitors + 1) as workers:
        futures = [*(workers.submit(compete) for _ in range(competitors)), workers.submit(foreground)]
        try:
            started, cpu = time.monotonic(), time.thread_time()
            with store.priority.background(check_stopping), work_budget(2), store.read_view() as reader:
                total = reader.conn.execute('WITH RECURSIVE n(x) AS (VALUES(0) UNION ALL '
                    'SELECT x+1 FROM n WHERE x<10000) SELECT SUM(x) FROM n').fetchone()[0]
                assert total == 50005000
                assert store.priority._foreground >= 1
            record_property('wall_seconds', time.monotonic() - started)
            record_property('thread_cpu_seconds', time.thread_time() - cpu)
            assert traffic
            assert calculations or not competitors
        finally:
            stop.set()
            release()
            for future in futures:
                future.result(3)
    assert store.priority._foreground == 0
    assert not store.conn.in_transaction
    # Interruption must return the admission slot and leave the writer usable.
    with store.read_view() as reader:
        assert reader.settings.get_setting('priority-progress-probe') == 'available'


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


@pytest.mark.parametrize('path,query,headers', [
    ('/home/growth', b'', []),
    ('/home/cards', b'prepare_attempt=1', []),
    ('/home/cards', b'prepare_attempt=', []),
    ('/home/cards', b'prepare_attempt=1&other=%FF', []),
    ('/', b'', [(b'x-home-refresh', b'sync')]),
])
def test_automatic_home_poll_yields_to_interactive_work_without_holding_priority(path, query, headers):
    priority = RequestPriority()
    elapsed = []
    release = priority.enter()

    async def app(scope, receive, send):
        assert priority._foreground == 1
        started = time.monotonic()
        with ThreadPoolExecutor(1) as pool:
            # Context propagation matches the request thread pool used by ASGI.
            from contextvars import copy_context
            pool.submit(copy_context().run, check_stopping).result(2)
        elapsed.append(time.monotonic() - started)

    try:
        asyncio.run(RequestPriorityMiddleware(app, priority)(
            {'type': 'http', 'method': 'GET', 'path': path,
             'query_string': query, 'headers': headers}, None, None))
        assert elapsed[0] >= .09
        assert priority._foreground == 1
    finally:
        release()
    assert priority._foreground == 0


@pytest.mark.parametrize('path,query', [
    ('/home/cards', b''),
    ('/home/cards', b'other=prepare_attempt'),
    ('/home/cards', b'other=%FF'),
    ('/', b''),
])
def test_explicit_home_navigation_keeps_foreground_priority(path, query):
    priority = RequestPriority()

    async def app(scope, receive, send):
        assert priority._foreground == 1

    asyncio.run(RequestPriorityMiddleware(app, priority)(
        {'type': 'http', 'method': 'GET', 'path': path, 'query_string': query}, None, None))
    assert priority._foreground == 0


def test_numerical_limit_applies_to_numpy_and_scipy_in_worker_thread():
    def inspect_limits():
        limit_native_threads()
        return threadpool_info()

    with ThreadPoolExecutor(1) as pool:
        libraries = pool.submit(inspect_limits).result(5)
    assert any(library["user_api"] == "blas" for library in libraries)
    assert all(library["num_threads"] == 1 for library in libraries)
