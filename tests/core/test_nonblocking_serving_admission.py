"""Best-effort work never turns a later serving acquisition into an unbounded wait."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import nullcontext
import threading

import pytest

from yt_playlist.core.activity import WorkBudgetExceeded, check_stopping
from yt_playlist.core.priority import RequestPriority, ServingLock


@pytest.mark.parametrize('background', [False, True])
def test_late_serving_owner_rejects_each_acquisition_without_owning_connection(background):
    priority = RequestPriority()
    lock = ServingLock(priority)
    held, release = threading.Event(), threading.Event()

    def holder():
        with lock:
            held.set()
            assert release.wait(5), 'Best-effort admission waited for its competing owner'

    with ThreadPoolExecutor(1) as workers:
        with lock.nonblocking_admission():
            # Merely entering the policy must leave the connection available.
            owner = workers.submit(holder)
            try:
                assert held.wait(2)
                with priority.background(check_stopping) if background else nullcontext():
                    assert not lock.acquire(timeout=2)
                    with pytest.raises(WorkBudgetExceeded), lock:
                        pytest.fail('A busy connection was admitted')
                    assert not lock.held_by_current_thread
            finally:
                release.set()
            owner.result(2)
            with lock:
                assert lock.held_by_current_thread
    assert not lock.held_by_current_thread and priority._foreground == 0


def test_policy_is_thread_local_and_a_failed_context_does_not_release_another_owner():
    lock = ServingLock(RequestPriority())
    held, release, waiting, acquired = (threading.Event() for _ in range(4))

    def holder():
        with lock:
            held.set()
            assert release.wait(5), 'Competing owner was not released'

    def ordinary_waiter():
        waiting.set()
        with lock:
            acquired.set()

    with ThreadPoolExecutor(2) as workers:
        owner = workers.submit(holder)
        try:
            assert held.wait(2)
            with pytest.raises(WorkBudgetExceeded), lock.nonblocking_admission():
                waiter = workers.submit(ordinary_waiter)
                assert waiting.wait(2)
                assert not acquired.wait(.05)
                with lock:
                    pytest.fail('A busy connection was admitted')
            assert not acquired.is_set() and not waiter.done()
        finally:
            release.set()
        owner.result(2)
        waiter.result(2)
    assert acquired.is_set() and not lock.held_by_current_thread


def test_nested_policy_restores_admission_and_preserves_reentrant_ownership():
    lock = ServingLock(RequestPriority())
    held, release, waiting = threading.Event(), threading.Event(), threading.Event()

    def holder():
        with lock:
            held.set()
            assert release.wait(5), 'Competing owner was not released'

    def resume_ordinary_admission():
        with lock.nonblocking_admission():
            with pytest.raises(ValueError):
                with lock.nonblocking_admission():
                    raise ValueError('controlled nested exit')
            with pytest.raises(WorkBudgetExceeded), lock:
                pytest.fail('Nested exit cleared the outer admission policy')
        waiting.set()
        with lock:
            return lock.held_by_current_thread

    with lock.nonblocking_admission(), lock:
        with pytest.raises(ValueError, match='controlled nested exit'):
            with lock.nonblocking_admission(), lock:
                assert lock.held_by_current_thread
                raise ValueError('controlled nested exit')
        assert lock.held_by_current_thread
    assert not lock.held_by_current_thread
    with ThreadPoolExecutor(2) as workers:
        owner = workers.submit(holder)
        try:
            assert held.wait(2)
            resumed = workers.submit(resume_ordinary_admission)
            assert waiting.wait(2)
            # Outside the policy, an ordinary context must wait and acquire.
            assert not resumed.done()
        finally:
            release.set()
        owner.result(2)
        assert resumed.result(2)
    assert not lock.held_by_current_thread
