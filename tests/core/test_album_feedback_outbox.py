"""Real retained-format feedback survives evidence contention and publication interruption."""
from contextlib import closing, contextmanager
from concurrent.futures import ThreadPoolExecutor
import logging
import sqlite3
import threading
import time
from types import SimpleNamespace
import uuid

import pytest

from tests.core.test_collection_owner_writes import (
    feedback_writer_templates, feedback_writer_prefix, seed_feedback_writers,
)
from tests.core.test_predecessor_writes import consumer_installed as installed
from tests.store_fixture_support import native_sql_calls, without_query_timing
from yt_playlist.core import album_feedback_outbox as outbox, album_feedback_storage, schema_inspection
from yt_playlist.core.activity import Activity, WorkBudgetExceeded, WorkClosing, cancellable_work
from yt_playlist.core.db_writes import atomic_write
from yt_playlist.core.store import Store
from yt_playlist.rec import album_feedback, group_roots
from yt_playlist.rec.rec_worker import RecWorker


pytestmark = pytest.mark.parametrize('installed', [79, 93, 107], indirect=True)


@pytest.fixture(autouse=True)
def disable_periodic_library_sync():
    """Feedback replay workers run directly without an HTTP lifespan."""


def test_outbox_schema_input_keeps_numbered_history_and_private_revision_guards(installed):
    store, version = installed
    source = store._fixture_installed_template.source
    assert source.conn.execute('PRAGMA query_only').fetchone()[0] == 1
    assert store.conn.execute('SELECT max(version) FROM schema_migrations').fetchone()[0] == version
    assert store._snapshot_revisions.conn is store.conn
    assert not store.conn.execute('PRAGMA foreign_key_check').fetchone()


def test_feedback_prefix_keeps_three_releases_and_the_original_response(installed, feedback_writer_prefix):
    store, _ = installed
    assert set(album_feedback_storage.original(store.conn)) == {'baseline-release'}
    events = store.rec_ledger.evidence().records('response')
    assert len(events) == 1
    assert events[0]['opportunity_id'] == 'baseline-release'
    assert feedback_writer_prefix.source.conn.execute('PRAGMA query_only').fetchone()[0] == 1


@pytest.fixture
def pair(installed, without_query_timing):
    store, _ = installed
    # The outbox owns actual overlapping stores, not a copied intermediate
    # fixture. Seed the same three releases and baseline response in place.
    seed_feedback_writers(store)
    other = Store(store._db_path)
    try:
        other.conn.execute('PRAGMA synchronous=OFF')
        with schema_inspection.reuse_tables(other.conn):
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


@pytest.mark.usefixtures('native_sql_calls')
def test_busy_publication_retains_exact_feedback_past_old_deadline(pair, monkeypatch):
    store, other = pair
    elapsed = 0
    monotonic, wall_time = time.monotonic, time.time
    monkeypatch.setattr(time, 'monotonic', lambda: monotonic() + elapsed)
    monkeypatch.setattr(time, 'time', lambda: wall_time() + elapsed)
    with group_roots.admission(store):
        album_feedback.set_feedback(other, 'first-release', 'more', 3, 'first-release')
        intent, = pending(other)
        original = album_feedback_storage.original(other.conn)['first-release']
        assert original['kind'] == 'more' and original['action_id'] == intent['action_id']
        assert not responses(other)
        # Cross the old retry deadline without spending a second idle. Both
        # clocks advance; the judgment's original observation stays unchanged.
        elapsed = 1.05
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


@pytest.mark.usefixtures('native_sql_calls')
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
        reopened.conn.execute('PRAGMA synchronous=OFF')
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


@pytest.mark.parametrize('stop', ['admission', 'worker'])
def test_shutdown_during_feedback_sql_keeps_intent_for_restart(pair, monkeypatch, caplog, stop):
    store, other = pair
    with group_roots.admission(store):
        album_feedback.set_feedback(other, 'first-release', 'more', 3, 'first-release')
    retained = pending(other)
    worker = RecWorker(SimpleNamespace(store=other, work=Activity(),
        logger=logging.getLogger(__name__), now_fn=lambda: 10))
    interruptions, waits = [], []

    def stop_during_query():
        assert worker.work.active == ('album feedback responses',)
        interruptions.append(stop)
        if stop == 'admission':
            worker.work.close_admission()
            assert not worker._stop.is_set()
        else:
            worker.shutdown()
        return 0

    other.conn.create_function('stop_feedback', 0, stop_during_query)

    def interrupted_read(conn):
        # Exercise the Store's actual progress handler during an admitted read.
        conn.execute('WITH RECURSIVE n(x) AS (VALUES(stop_feedback()) UNION ALL '
            'SELECT x+1 FROM n WHERE x<100000000) SELECT SUM(x) FROM n').fetchone()
        pytest.fail('Shutdown did not interrupt feedback SQL')

    def bounded_wait(timeout):
        waits.append(timeout)
        return len(waits) > 1

    with monkeypatch.context() as shutdown:
        shutdown.setattr(outbox, 'available', interrupted_read)
        shutdown.setattr(worker._stop, 'wait', bounded_wait)
        with caplog.at_level(logging.WARNING):
            worker._maintenance_loop()
    worker.shutdown()
    assert interruptions == [stop] and waits == [0.5]
    assert not [record for record in caplog.records if record.name in {
        __name__, 'yt_playlist.rec.album_feedback'} and record.levelno >= logging.WARNING]
    assert worker.work.wait(0) and not other.conn.in_transaction
    assert pending(other) == retained and not responses(other)
    other.conn.close()
    reopened = Store(store._db_path)
    with closing(reopened.conn):
        reopened.init_schema(defer_metadata=True)
        resumed = RecWorker(SimpleNamespace(store=reopened, work=Activity(),
            logger=logging.getLogger(__name__), now_fn=lambda: 10))
        try:
            assert resumed._poll_album_feedback() == 1
            assert resumed._poll_album_feedback() == 0
            assert not pending(reopened)
            response, = responses(reopened)
            assert (response['id'], response['observed_at'], response['details']) == (
                retained[0]['action_id'], 3, {'kind': 'more'})
        finally:
            resumed.shutdown()


@pytest.mark.parametrize('failure', ['interrupted', 'missing-table-during-shutdown'])
def test_unexpected_feedback_sql_failure_keeps_warning(pair, monkeypatch, caplog, failure):
    _, other = pair
    worker = RecWorker(SimpleNamespace(store=other, work=Activity(),
        logger=logging.getLogger(__name__), now_fn=lambda: 10))
    other.conn.create_function('interrupt_feedback', 0, lambda: (other.conn.interrupt(), 0)[1])

    def failed_read(conn):
        if failure == 'interrupted':
            conn.execute('SELECT interrupt_feedback()').fetchone()
        else:
            worker.work.close_admission()
            conn.execute('SELECT * FROM missing_feedback_table').fetchall()
        pytest.fail('Controlled SQL failure was not raised')

    try:
        with monkeypatch.context() as failed:
            failed.setattr(outbox, 'available', failed_read)
            with caplog.at_level(logging.WARNING, logger='yt_playlist.rec.album_feedback'):
                if failure == 'interrupted':
                    assert worker._poll_album_feedback() == 0
                else:
                    with pytest.raises(WorkClosing):
                        worker._poll_album_feedback()
        warning, = [record for record in caplog.records
                    if record.message == 'Retained album feedback awaits evidence publication']
        assert isinstance(warning.exc_info[1], sqlite3.OperationalError)
        assert warning.exc_info[1].sqlite_errorcode == (
            sqlite3.SQLITE_INTERRUPT if failure == 'interrupted' else sqlite3.SQLITE_ERROR)
        assert worker.work.wait(0)
    finally:
        worker.shutdown()


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
        name = 'mix_projection_facts' if boundary == 'projection' else 'evidence'
        owner = other.rec_ledger.repository if boundary == 'projection' else other.rec_ledger
        operation = getattr(owner, name)

        def read(*args, **kwargs):
            calls.append(True)
            if len(calls) == (2 if boundary == 'readback' else 1):
                owners.append(workers.submit(hold_serving_lock))
                assert held.wait(2)
            return operation(*args, **kwargs)

        controlled.setattr(owner, name, read)
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


@pytest.mark.usefixtures('native_sql_calls')
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
