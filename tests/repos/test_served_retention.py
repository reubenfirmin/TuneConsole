"""Serving-ledger maintenance stays bounded without discarding attribution policy."""
from concurrent.futures import ThreadPoolExecutor
import json
import threading

from yt_playlist.core.store import Store
from yt_playlist.repos.rec_ledger import RETENTION_BATCH_SIZE
from yt_playlist.rec import scorecard


def _impressions(store, requests):
    with store.conn:
        store.conn.executemany(
            'INSERT INTO rec_served_impressions(request_id,surface,lane,identity_key,rank,model_version,provenance,served_at) '
            "VALUES (?,'home','comfort','song',1,?,?,?)", requests)


def test_retention_batches_converge_and_preserve_cutoff_and_both_policies(store):
    count = RETENTION_BATCH_SIZE * 2 + 17
    for policy in ('expired-model', 'keep-model', 'keep-pool', 'orphan'):
        store.rec_ledger.record_serving_policy(policy, {'policy': policy}, 1)
    _impressions(store, [(f'old-{n}', 'expired-model', '{}', 10) for n in range(count)] +
                 [('boundary', 'keep-model', '{"pool_policy":"keep-pool"}', 50)])
    with store.conn:
        store.conn.executemany(
            "INSERT INTO rec_response_events(event_id,kind,identity_key,at) VALUES (?,'open','song',?)",
            [(f'old-{n}', 10) for n in range(count)] + [('boundary', 50)])

    def prune():
        store.rec_ledger.record_served_impressions('prune', 'home', [], 100, prune_before=50)

    prune()
    assert store.conn.execute('SELECT COUNT(*) FROM rec_served_impressions').fetchone()[0] == count + 1 - RETENTION_BATCH_SIZE
    assert store.conn.execute('SELECT COUNT(*) FROM rec_response_events').fetchone()[0] == count + 1 - RETENTION_BATCH_SIZE
    assert store.conn.execute("SELECT 1 FROM rec_serving_policies WHERE model_version='expired-model'").fetchone()
    for _ in range(4):
        prune()
    assert [r[0] for r in store.conn.execute('SELECT request_id FROM rec_served_impressions')] == ['boundary']
    assert [r[0] for r in store.conn.execute('SELECT event_id FROM rec_response_events')] == ['boundary']
    assert {r[0] for r in store.conn.execute('SELECT model_version FROM rec_serving_policies')} == {'keep-model', 'keep-pool'}
    impression = store.conn.execute('SELECT id FROM rec_served_impressions').fetchone()[0]
    retained = scorecard.served_recommendation(store, impression)
    assert retained['configuration'] == {'policy': 'keep-model'}
    assert retained['pool_configuration'] == {'policy': 'keep-pool'}


def test_policy_scan_passes_referenced_batch_and_resumes_after_restart(tmp_path):
    path = tmp_path / 'retention.db'
    store = Store(path)
    store.init_schema()
    with store.conn:
        store.conn.executemany('INSERT INTO rec_serving_policies VALUES (?, ?, 1)',
            [(f'keep-{n:04}', '{}') for n in range(RETENTION_BATCH_SIZE)] + [('zzz-orphan', '{}')])
    _impressions(store, [(f'kept-{n}', f'keep-{n:04}', '{}', 100) for n in range(RETENTION_BATCH_SIZE)])
    store.rec_ledger.record_served_impressions('prune', 'home', [], 101, prune_before=50)
    assert store.conn.execute("SELECT 1 FROM rec_serving_policies WHERE model_version='zzz-orphan'").fetchone()
    store.conn.close()
    reopened = Store(path)
    reopened.init_schema()
    try:
        reopened.rec_ledger.record_served_impressions('prune-again', 'home', [], 102, prune_before=50)
        assert reopened.conn.execute("SELECT 1 FROM rec_serving_policies WHERE model_version='zzz-orphan'").fetchone() is None
        assert reopened.conn.execute('SELECT COUNT(*) FROM rec_serving_policies').fetchone()[0] == RETENTION_BATCH_SIZE
    finally:
        reopened.conn.close()


def test_numeric_pool_reference_keeps_original_text_comparison_semantics(store):
    store.rec_ledger.record_serving_policy('42', {'pool': True}, 1)
    _impressions(store, [('kept', 'model', json.dumps({'pool_policy': 42}), 100)])
    store.rec_ledger.record_served_impressions('prune', 'home', [], 101, prune_before=50)
    assert store.conn.execute("SELECT 1 FROM rec_serving_policies WHERE model_version='42'").fetchone()


def test_insertion_releases_serving_lock_before_cleanup(store, monkeypatch):
    entered, release = threading.Event(), threading.Event()
    original = store.rec_ledger._prune_impression_batch

    def paused(before):
        entered.set()
        assert release.wait(3)
        original(before)

    monkeypatch.setattr(store.rec_ledger, '_prune_impression_batch', paused)
    with ThreadPoolExecutor(2) as pool:
        maintenance = pool.submit(store.rec_ledger.record_served_impressions,
                                  'request', 'home', [{'key': 'song', 'rank': 1}], 100, 50)
        try:
            assert entered.wait(2)
            assert pool.submit(store.settings.get_setting, 'ui-check').result(1) is None
            assert store.conn.execute('SELECT request_id FROM rec_served_impressions').fetchone()[0] == 'request'
        finally:
            release.set()
            maintenance.result(3)


def test_referenced_policy_checks_do_not_scan_the_impression_ledger(store):
    store.rec_ledger.record_serving_policy('pool', {}, 1)
    _impressions(store, [('pool-reference', 'other', '{"pool_policy":"pool"}', 100)])

    def work():
        steps = [0]
        def progress():
            steps[0] += 100
            return 0
        store.conn.set_progress_handler(progress, 100)
        try:
            store.rec_ledger.record_served_impressions('prune', 'home', [], 101, prune_before=50)
        finally:
            store.conn.set_progress_handler(None, 0)
        return steps[0]

    small = work()
    _impressions(store, [(f'unrelated-{n}', 'other', '{}', 100) for n in range(3000)])
    large = work()
    assert large <= small * 2 + 500
    assert store.conn.execute("SELECT 1 FROM rec_serving_policies WHERE model_version='pool'").fetchone()
