"""Real retained-format feedback survives evidence contention and publication interruption."""
from contextlib import closing, contextmanager
from concurrent.futures import ThreadPoolExecutor
import logging
import threading
import time
from types import SimpleNamespace
import uuid

import pytest

from tests.core.test_collection_owner_writes import feedback_writer_templates, feedback_writer_prefix
from tests.core.test_predecessor_writes import installed
from yt_playlist.core import album_feedback_outbox as outbox, album_feedback_storage
from yt_playlist.core.activity import Activity, WorkBudgetExceeded, WorkClosing, cancellable_work
from yt_playlist.core.db_writes import atomic_write
from yt_playlist.core.store import Store
from yt_playlist.rec import album_feedback, group_roots
from yt_playlist.rec.rec_worker import RecWorker


pytestmark = pytest.mark.parametrize('installed', [79, 93, 107], indirect=True)


@pytest.fixture
def pair(installed, feedback_writer_prefix):
    store, _ = installed
    other = Store(store._db_path)
    try:
        other.init_schema(defer_metadata=True)
        assert group_roots.enabled(store) and group_roots.enabled(other)
        yield store, other
    finally:
        other.conn.close()


def pending(store):
    return [dict(row) for row in outbox.pending(store.conn, limit=128)]


def responses(store):
    return [row for row in store.rec_ledger.evidence().records('response')
            if row['opportunity_id'] == 'first-release']


def withdrawal(action):
    return uuid.uuid5(uuid.NAMESPACE_URL, 'album-feedback-withdrawal:' + action).hex


def test_busy_publication_retains_exact_feedback_past_old_deadline(pair, installed):
    store, other = pair
    with group_roots.admission(store):
        album_feedback.set_feedback(other, 'first-release', 'more', 3, 'first-release')
        intent, = pending(other)
        original = album_feedback_storage.original(other.conn)['first-release']
        assert original['kind'] == 'more' and original['action_id'] == intent['action_id']
        assert not responses(other)
        # One real historical arm deliberately exceeds the removed one-second
        # retry window; the other formats need not repeat that elapsed wait.
        if installed[1] == 93:
            time.sleep(1.05)
        assert album_feedback.drain(other) == 0
        assert pending(other) == [intent]
    assert album_feedback.drain(other) == 1
    assert not pending(other)
    response, = responses(other)
    assert (response['id'], response['opportunity_id'], response['kind'], response['option_id'],
            response['at'], response['observed_at'], response['details']) == (
        intent['action_id'], 'first-release', 'feedback', 'first-release', 3, 3, {'kind': 'more'})
    assert album_feedback.drain(other) == 0
    assert responses(other) == [response]


def test_replacement_clear_and_repeated_clear_keep_unique_original_actions(pair):
    store, other = pair
    with group_roots.admission(store):
        album_feedback.set_feedback(other, 'first-release', 'more', 3, 'first-release')
        first = album_feedback_storage.original(other.conn)['first-release']['action_id']
        album_feedback.set_feedback(other, 'first-release', 'known', 4, 'first-release')
        second = album_feedback_storage.original(other.conn)['first-release']['action_id']
        album_feedback.set_feedback(other, 'first-release', 'clear', 5)
        album_feedback.set_feedback(other, 'first-release', 'clear', 6)
        assert 'first-release' not in album_feedback_storage.original(other.conn)
        intents = {row['action_id']: row for row in pending(other)}
        assert set(intents) == {first, second, withdrawal(first), withdrawal(second)}
        assert intents[withdrawal(first)]['observed_at'] == 4
        assert intents[withdrawal(second)]['observed_at'] == 5
        assert not responses(other)
    assert album_feedback.drain(other) == 4
    actual = {row['id']: row for row in responses(other)}
    assert set(actual) == set(intents)
    assert actual[first]['details'] == {'kind': 'more'}
    assert actual[second]['details'] == {'kind': 'known'}
    for action, at in ((first, 4), (second, 5)):
        event = actual[withdrawal(action)]
        assert event['kind'] == 'withdraw' and event['details'] == {'action_id': action}
        assert event['at'] == event['observed_at'] == at
    assert not pending(other)
    assert album_feedback.drain(other) == 0
    assert {row['id']: row for row in responses(other)} == actual
    album_feedback.set_feedback(other, 'first-release', 'clear', 7)
    assert not pending(other)
    assert {row['id']: row for row in responses(other)} == actual


def test_publish_before_acknowledgement_failure_is_deduplicated_after_reopen(pair, monkeypatch):
    store, other = pair
    with monkeypatch.context() as interrupted:
        def fail_acknowledgement(*args):
            raise OSError('controlled failure after actual file publication')
        interrupted.setattr(outbox, 'acknowledge', fail_acknowledgement)
        album_feedback.set_feedback(other, 'first-release', 'more', 3, 'first-release')
    intent, = pending(other)
    response, = responses(other)
    assert response['id'] == intent['action_id']
    other.conn.close()
    reopened = Store(store._db_path)
    with closing(reopened.conn):
        reopened.init_schema(defer_metadata=True)
        assert pending(reopened) == [intent]
        assert album_feedback.drain(reopened) == 1
        assert not pending(reopened)
        assert responses(reopened) == [response]


def test_sql_writer_contention_after_publication_retains_intent_for_deduplicated_ack(pair):
    store, other = pair
    with group_roots.admission(store):
        album_feedback.set_feedback(other, 'first-release', 'more', 3, 'first-release')
    retained = pending(other)
    with atomic_write(store):
        assert album_feedback.try_drain(other) == 0
        assert pending(other) == retained
        response, = responses(other)
    assert album_feedback.drain(other) == 1
    assert not pending(other) and responses(other) == [response]


def test_pause_cancellation_and_shutdown_keep_intents_for_next_worker(pair):
    store, other = pair
    with group_roots.admission(store):
        album_feedback.set_feedback(other, 'first-release', 'more', 3, 'first-release')
    retained = pending(other)
    other.activity_paused = True
    try:
        assert album_feedback.drain(other) == 0
    finally:
        other.activity_paused = False
    stopped = False
    with cancellable_work(lambda: stopped):
        stopped = True
        with pytest.raises(WorkClosing):
            album_feedback.drain(other)
        stopped = False
    assert pending(other) == retained and not responses(other)
    ctx = SimpleNamespace(store=other, work=Activity(), logger=logging.getLogger(__name__), now_fn=lambda: 10)
    worker = RecWorker(ctx)
    worker.shutdown()
    assert worker._poll_album_feedback() == 0
    assert pending(other) == retained and not responses(other)
    other.conn.close()
    reopened = Store(store._db_path)
    with closing(reopened.conn):
        reopened.init_schema(defer_metadata=True)
        resumed = RecWorker(SimpleNamespace(store=reopened, work=Activity(),
            logger=logging.getLogger(__name__), now_fn=lambda: 10))
        try:
            assert resumed._poll_album_feedback() == 1
            assert not pending(reopened)
            assert len(responses(reopened)) == 1
            assert not resumed.jobs.measurements
        finally:
            resumed.shutdown()


def test_failed_intent_capture_rolls_back_judgment_and_emits_nothing(pair, monkeypatch):
    _, other = pair
    before = album_feedback_storage.original(other.conn)
    def fail_capture(*args, **kwargs):
        raise OSError('controlled intent capture failure')
    with monkeypatch.context() as interrupted:
        interrupted.setattr(outbox, 'enqueue', fail_capture)
        with pytest.raises(OSError, match='controlled intent capture failure'):
            album_feedback.set_feedback(other, 'first-release', 'more', 3, 'first-release')
    assert album_feedback_storage.original(other.conn) == before
    assert not pending(other) and not responses(other)


def test_joined_transaction_does_not_publish_and_blocks_retirement_until_commit(pair):
    store, other = pair
    with atomic_write(other):
        album_feedback.set_feedback(other, 'first-release', 'more', 3, 'first-release')
        assert len(pending(other)) == 1 and not responses(other)
        with pytest.raises(WorkBudgetExceeded):
            with store.album_feedback.protection():
                pytest.fail('Retirement ignored an uncommitted response intent')
    assert album_feedback.drain(other) == 1
    assert len(responses(other)) == 1
    assert not pending(other)


def test_outer_rollback_removes_both_judgment_and_intent(pair):
    _, other = pair
    before = album_feedback_storage.original(other.conn)
    with pytest.raises(RuntimeError, match='controlled outer rollback'):
        with atomic_write(other):
            album_feedback.set_feedback(other, 'first-release', 'more', 3, 'first-release')
            assert len(pending(other)) == 1 and not responses(other)
            raise RuntimeError('controlled outer rollback')
    assert album_feedback_storage.original(other.conn) == before
    assert not pending(other) and not responses(other)


def test_serving_lock_retains_capture_without_inline_publication(pair):
    _, other = pair
    with other._lock:
        album_feedback.set_feedback(other, 'first-release', 'more', 3, 'first-release')
        assert len(pending(other)) == 1 and not responses(other)
        assert album_feedback.drain(other) == 0
    assert album_feedback.drain(other) == 1
    assert len(responses(other)) == 1 and not pending(other)


@pytest.mark.parametrize('boundary', ['response', 'acknowledgement'])
def test_replay_does_not_wait_for_serving_lock_taken_after_snapshot(pair, monkeypatch, boundary):
    from yt_playlist.rec import choice_evidence
    store, other = pair
    with group_roots.admission(store):
        album_feedback.set_feedback(other, 'first-release', 'more', 3, 'first-release')
    retained = pending(other)
    held, release = threading.Event(), threading.Event()

    def hold_serving_lock():
        with other._lock:
            held.set()
            assert release.wait(5), 'Replay did not release its competing test holder'

    with ThreadPoolExecutor(max_workers=2) as workers, monkeypatch.context() as controlled:
        if boundary == 'response':
            operation = choice_evidence.respond

            def respond(*args, **kwargs):
                workers.submit(hold_serving_lock)
                assert held.wait(2)
                return operation(*args, **kwargs)

            controlled.setattr(choice_evidence, 'respond', respond)
        else:
            from yt_playlist.repos import album_feedback as feedback_repo
            operation = feedback_repo.atomic_write

            @contextmanager
            def acknowledge(*args, **kwargs):
                workers.submit(hold_serving_lock)
                assert held.wait(2)
                with operation(*args, **kwargs):
                    yield

            controlled.setattr(feedback_repo, 'atomic_write', acknowledge)
        replay = workers.submit(album_feedback.try_drain, other)
        try:
            assert held.wait(2)
            assert replay.result(timeout=1) == 0
        finally:
            release.set()
    assert pending(other) == retained
    assert len(responses(other)) == (boundary == 'acknowledgement')
    assert album_feedback.drain(other) == 1
    assert not pending(other) and len(responses(other)) == 1


@pytest.mark.parametrize('boundary', ['ledger', 'projection', 'readback'])
def test_replay_defers_late_serving_ownership_inside_actual_evidence_reads(pair, monkeypatch, boundary):
    store, other = pair
    with group_roots.admission(store):
        album_feedback.set_feedback(other, 'first-release', 'more', 3, 'first-release')
    retained = pending(other)
    held, release = threading.Event(), threading.Event()
    owners, calls = [], []

    def hold_serving_lock():
        with other._lock:
            held.set()
            assert release.wait(5), 'Replay waited for a late evidence-read owner'

    with ThreadPoolExecutor(2) as workers, monkeypatch.context() as controlled:
        name = '_mix_projection' if boundary == 'projection' else 'evidence'
        operation = getattr(other.rec_ledger, name)

        def read(*args, **kwargs):
            calls.append(True)
            if len(calls) == (2 if boundary == 'readback' else 1):
                owners.append(workers.submit(hold_serving_lock))
                assert held.wait(2)
            return operation(*args, **kwargs)

        controlled.setattr(other.rec_ledger, name, read)
        replay = workers.submit(album_feedback.try_drain, other)
        try:
            assert held.wait(2)
            assert replay.result(timeout=1) == 0
        finally:
            release.set()
        for owner in owners:
            owner.result(2)
    assert pending(other) == retained
    assert len(responses(other)) == (boundary == 'readback')
    assert album_feedback.drain(other) == 1
    assert not pending(other) and len(responses(other)) == 1


def test_replay_file_reads_and_publication_do_not_own_serving_connection(pair, monkeypatch):
    from yt_playlist.core.evidence_ledger import EvidenceLedger
    store, other = pair
    with group_roots.admission(store):
        album_feedback.set_feedback(other, 'first-release', 'more', 3, 'first-release')
    reads, writes = [], []
    get, put = EvidenceLedger.get, EvidenceLedger.put

    def read(ledger, kind, identity):
        assert not other._lock.held_by_current_thread
        reads.append((kind, identity))
        return get(ledger, kind, identity)

    def publish(ledger, kind, identity, value, **kwargs):
        assert not other._lock.held_by_current_thread
        writes.append((kind, identity))
        return put(ledger, kind, identity, value, **kwargs)

    with monkeypatch.context() as observed:
        observed.setattr(EvidenceLedger, 'get', read)
        observed.setattr(EvidenceLedger, 'put', publish)
        assert album_feedback.drain(other) == 1
    response, = responses(other)
    assert ('opportunity', 'first-release') in reads
    assert reads.count(('response', response['id'])) == 2
    assert writes == [('response', response['id'])]
    assert not pending(other)


def test_retirement_fence_rejects_capture_before_mutation_under_serving_lock(pair):
    store, other = pair
    before = album_feedback_storage.original(other.conn)
    with store.album_feedback.protection(), other._lock:
        with pytest.raises(WorkBudgetExceeded):
            album_feedback.set_feedback(other, 'first-release', 'more', 3, 'first-release')
    assert album_feedback_storage.original(other.conn) == before
    assert not pending(other) and not responses(other)


def test_retention_refreshes_durable_pins_after_its_inventory(pair):
    store, other = pair
    # The collector obtains its final pins after this separate Store captures.
    with group_roots.admission(store):
        album_feedback.set_feedback(other, 'first-release', 'more', 3, 'first-release')
    ledger = store.rec_ledger.evidence()
    ledger.maintain(1000, protected_opportunities={'baseline-release', 'second-release'},
                   pending_protection=store.album_feedback.protection)
    assert ledger.get('opportunity', 'first-release') is not None
    assert len(pending(other)) == 1
    assert album_feedback.drain(other) == 1
    assert len(responses(other)) == 1


def test_feedback_captured_after_retirement_planning_survives_final_deletion(pair):
    store, other = pair
    ledger = store.rec_ledger.evidence()
    captured = []

    @contextmanager
    def capture_before_final_pin_check():
        album_feedback.set_feedback(other, 'first-release', 'more', 3, 'first-release')
        intent, = pending(other)
        captured.append(intent)
        assert not responses(other)
        with store.album_feedback.protection() as pins:
            assert 'first-release' in pins[1]
            yield pins

    with group_roots.admission(store):
        ledger.maintain(1000, protected_opportunities={'baseline-release', 'second-release'},
                       pending_protection=capture_before_final_pin_check)
    assert len(captured) == 1
    assert ledger.get('opportunity', 'first-release') is not None
    assert pending(other) == captured
    assert album_feedback.drain(other) == 1
    response, = responses(other)
    assert response['id'] == captured[0]['action_id']
    assert not pending(other)


def test_replay_batch_is_bounded_and_missing_offer_does_not_starve_next_intent(pair):
    store, other = pair
    with group_roots.admission(store):
        album_feedback.set_feedback(other, 'first-release', 'more', 3, 'not-issued')
        album_feedback.set_feedback(other, 'second-release', 'known', 4, 'second-release')
    assert len(pending(other)) == 2
    assert album_feedback.drain(other, limit=1) == 0
    assert len(pending(other)) == 2
    assert album_feedback.drain(other, limit=1) == 1
    retained, = pending(other)
    assert retained['opportunity_id'] == 'not-issued'
    actual = [row for row in other.rec_ledger.evidence().records('response')
              if row['opportunity_id'] == 'second-release']
    assert len(actual) == 1 and actual[0]['details'] == {'kind': 'known'}


def test_invalid_option_retains_its_intent_without_starving_another_judgment(pair):
    store, other = pair
    with group_roots.admission(store):
        album_feedback.set_feedback(other, 'first-release', 'more', 3, 'second-release')
        album_feedback.set_feedback(other, 'second-release', 'known', 4, 'second-release')
    assert album_feedback.try_drain(other, limit=1) == 0
    assert len(pending(other)) == 2
    assert album_feedback.drain(other, limit=1) == 1
    retained, = pending(other)
    assert retained['option_id'] == 'first-release'
    actual = [row for row in other.rec_ledger.evidence().records('response')
              if row['opportunity_id'] == 'second-release']
    assert len(actual) == 1 and actual[0]['details'] == {'kind': 'known'}
