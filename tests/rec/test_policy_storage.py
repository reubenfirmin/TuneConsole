"""Storage growth, retention races and exact attribution for prepared policies."""
import json
import sqlite3
from types import SimpleNamespace

import pytest

from yt_playlist.core import db_space, policy_config
from yt_playlist.core.store import Store
from yt_playlist.rec import candidate_refresh, measurement, mode_surfaces, scorecard
from yt_playlist.repos import policy_storage


def _publish(store, now=1, rankers=('first', 'second'), body=None):
    config = body or {'weights': {'genre:rock': 1.4}, 'ranker': 'lane'}
    base = policy_config.base_record(config, now)
    rows = [policy_config.variant_record(base[0], {'ranker': ranker}, now) for ranker in rankers]
    store.rec_ledger.record_prepared_policies(rows, [base])
    return rows, base


def test_pool_variants_share_large_body_and_preserve_exact_configuration(store, monkeypatch):
    body = {'weights': {'genre:rock': 1.23456789}, 'ranker': 'lane', 'evidence': 'x' * 48000}
    monkeypatch.setattr(measurement, 'serving_policy', lambda *args: ('base', body))
    policies = measurement.PoolPolicies(store, 100)
    items = [policies.stamp([{'key': 'song'}], f'vibe:lane:family-{n}')[0] for n in range(1008)]
    store.rec_ledger.record_prepared_policies(policies.rows.values(), policies.bases.values())
    assert len(policies.bases) == 1
    assert len(policies.rows) == 1008
    assert len({item['pool_policy'] for item in items}) == 1008
    size = store.conn.execute('SELECT sum(length(configuration)) FROM rec_serving_policies').fetchone()[0]
    size += store.conn.execute('SELECT sum(byte_size) FROM rec_policy_bases').fetchone()[0]
    assert size < 220000  # Formerly approximately 48 MB for this one build.
    assert store.rec_ledger.policy_configuration(items[-1]['pool_policy']) == {
        **body, 'ranker': 'vibe:lane:family-1007'}


def test_repeated_advancing_time_preparation_without_serving_has_bounded_bytes(store, monkeypatch):
    monkeypatch.setattr(measurement, 'serving_policy', lambda reader, now: ('base', {
        'ranker': 'lane', 'weights': {'decayed': 1 + 1 / now}, 'evidence': 'x' * 48000}))
    monkeypatch.setattr(candidate_refresh, 'prepared_settings', lambda *args: {})
    def calculate(reader, now, policies):
        return {'pools': [policies.stamp([{'key': 'song'}], f'vibe:lane:family-{n}')[0]
                          for n in range(1000)]}, []
    monkeypatch.setattr(mode_surfaces, '_calculate_bundles', calculate)
    for tick in range(80):
        assert mode_surfaces.prepare_bundles(store, 10000 + 120 * tick) is not None
    assert store.conn.execute('SELECT count(*) FROM rec_served_impressions').fetchone()[0] == 0
    assert store.conn.execute('SELECT count(*) FROM rec_serving_policies').fetchone()[0] <= 32000
    assert store.conn.execute('SELECT count(*) FROM rec_policy_bases').fetchone()[0] <= 32
    stored = store.conn.execute('SELECT sum(length(configuration)) FROM rec_serving_policies').fetchone()[0]
    stored += store.conn.execute('SELECT sum(byte_size) FROM rec_policy_bases').fetchone()[0]
    assert stored < 7 * 1024 * 1024  # Unshared payloads would exceed 3.8 GB.
    latest = store.rec_proposals.get_proposals('mode_bundles')['pools'][0]['pool_policy']
    assert store.rec_ledger.policy_configuration(latest)['weights']['decayed'] == 1 + 1 / (10000 + 79 * 120)


def test_unused_retention_protects_both_impression_paths_proposals_sessions_and_grace(store):
    for version, at in [('model', 1), ('42', 1), ('prepared', 1), ('session', 1), ('unused', 1), ('new', 9999)]:
        store.rec_ledger.record_serving_policy(version, {'policy': version}, at)
    store.rec_ledger.record_served_impressions('kept', 'home', [{'key': 'song', 'rank': 1,
        'model_version': 'model', 'provenance': {'pool_policy': 42}}], 10000)
    store.rec_proposals.put_proposals('some-prepared-surface', {'items': [{'pool_policy': 'prepared'}]}, 1)
    store.settings.set_setting('radio_session', json.dumps({'nested': {'pool_policy': 'session'}}))
    assert measurement.maintain_storage(store, 10000)
    assert {row[0] for row in store.conn.execute('SELECT model_version FROM rec_serving_policies')} == {
        'model', '42', 'prepared', 'session', 'new'}
    store.rec_ledger._prune_policy_batch(10000)
    assert store.rec_ledger.policy_configuration('prepared') == {'policy': 'prepared'}
    assert store.rec_ledger.policy_configuration('session') == {'policy': 'session'}


def test_prepared_reference_change_defers_cleanup(tmp_path, monkeypatch):
    store = Store(tmp_path / 'state.db')
    store.init_schema()
    store.rec_ledger.record_serving_policy('old-policy', {'exact': True}, 1)
    original = policy_storage.protected_references
    def raced(conn):
        result = original(conn)
        store.rec_proposals.put_proposals('new-pool', {'pool_policy': 'old-policy'}, 10000)
        return result
    monkeypatch.setattr(policy_storage, 'protected_references', raced)
    try:
        assert measurement.maintain_storage(store, 10000) is False
        assert store.rec_ledger.policy_configuration('old-policy') == {'exact': True}
    finally:
        store.conn.close()


def test_queued_offer_restores_collected_pool_with_its_impression(tmp_path):
    store = Store(tmp_path / 'state.db')
    store.init_schema()
    rows, base = _publish(store, rankers=('pool-ranker',))
    version = rows[0][0]
    offer = measurement.CapturedOffer(store, 'home', [{'key': 'song', 'pool_policy': version}], 2)
    try:
        assert measurement.maintain_storage(store, 10000)
        assert store.rec_ledger.policy_configuration(version) is None
        assert store.conn.execute('SELECT count(*) FROM rec_policy_bases').fetchone()[0] == 0
        offer.run()
        impression = store.conn.execute('SELECT id FROM rec_served_impressions').fetchone()[0]
        assert scorecard.served_recommendation(store, impression)['pool_configuration'] == {
            'weights': {'genre:rock': 1.4}, 'ranker': 'pool-ranker'}
        assert store.conn.execute('PRAGMA foreign_key_check').fetchall() == []
    finally:
        offer.close()
        store.conn.close()


@pytest.mark.parametrize('budget', ['MAX_UNUSED_BYTES', 'MAX_UNUSED_VARIANTS'])
def test_budget_rejection_preserves_last_good_projection_and_policies(store, monkeypatch, budget):
    rows, base = _publish(store)
    store.rec_proposals.put_proposals('mode_bundles', {'last-good': True}, 1)
    monkeypatch.setattr(policy_storage, budget, 1)
    with pytest.raises(policy_storage.PolicyStorageFull), store.conn:
        new_rows = [policy_config.variant_record(base[0], {'ranker': 'new'}, 2)]
        store.rec_ledger.publish_prepared_policies(new_rows, [base])
        store.rec_proposals.publish_prepared_home('{"last-good":false}', 2, surface='mode_bundles')
    assert store.rec_proposals.get_proposals('mode_bundles') == {'last-good': True}
    assert store.conn.execute('SELECT count(*) FROM rec_serving_policies').fetchone()[0] == len(rows)


def test_failed_policy_publication_rolls_back_bodies_variants_and_links(store):
    row, base = policy_config.pack({'ranker': 'valid', 'exact': True}, 1)
    bad = policy_config.variant_record('missing-body', {'ranker': 'invalid'}, 1)
    with pytest.raises(sqlite3.IntegrityError):
        store.rec_ledger.record_prepared_policies([row, bad], [base])
    assert not store.conn.in_transaction
    for table in ('rec_serving_policies', 'rec_policy_bases', 'rec_policy_variants'):
        assert store.conn.execute(f'SELECT count(*) FROM {table}').fetchone()[0] == 0


def test_sqlite_full_rolls_back_new_policy_and_keeps_personal_data(tmp_path):
    store = Store(tmp_path / 'state.db')
    store.init_schema()
    store.settings.set_setting('personal', 'keep')
    pages = store.conn.execute('PRAGMA page_count').fetchone()[0]
    store.conn.execute(f'PRAGMA max_page_count={pages}')
    row, base = policy_config.pack({'ranker': 'large', 'evidence': 'x' * 1000000}, 1)
    try:
        with pytest.raises(sqlite3.OperationalError, match='full'):
            store.rec_ledger.record_prepared_policies([row], [base])
        assert not store.conn.in_transaction
        assert store.settings.get_setting('personal') == 'keep'
        assert store.conn.execute('SELECT count(*) FROM rec_policy_bases').fetchone()[0] == 0
        assert store.conn.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
    finally:
        store.conn.close()


def test_low_headroom_allows_existing_startup_and_core_writes_but_defers_preparation(tmp_path, monkeypatch):
    path = tmp_path / 'state.db'
    store = Store(path)
    store.init_schema()
    store.conn.close()
    monkeypatch.setattr(db_space.shutil, 'disk_usage', lambda path: SimpleNamespace(free=10 * 1024 * 1024))
    store = Store(path)
    store.init_schema()
    try:
        store.settings.set_setting('personal', 'saved')
        with pytest.raises(db_space.LowDiskSpace, match='10.0 MiB available'):
            _publish(store)
        assert store.settings.get_setting('personal') == 'saved'
        assert store.conn.execute('SELECT count(*) FROM rec_serving_policies').fetchone()[0] == 0
    finally:
        store.conn.close()


def test_republishing_existing_variants_at_exact_budget_is_idempotent(store, monkeypatch):
    rows, base = _publish(store)
    size = base[2] + sum(len(row[1].encode()) for row in rows)
    monkeypatch.setattr(policy_storage, 'MAX_UNUSED_BYTES', size)
    monkeypatch.setattr(policy_storage, 'MAX_UNUSED_VARIANTS', len(rows))
    store.rec_ledger.record_prepared_policies(rows, [base])
    assert store.conn.execute('SELECT count(*) FROM rec_serving_policies').fetchone()[0] == len(rows)


def test_quota_accounting_tracks_inserted_and_expired_impression_references(store):
    rows, base = _publish(store)
    first, second = (row[0] for row in rows)
    store.rec_ledger.record_served_impressions('one', 'home', [{'key': 'song', 'rank': 1,
        'model_version': first, 'provenance': {'pool_policy': second}}], 1)
    assert [row[0] for row in store.conn.execute('SELECT served FROM rec_policy_variants')] == [1, 1]
    assert store.conn.execute('SELECT served FROM rec_policy_bases').fetchone()[0] == 1
    store.rec_ledger.record_served_impressions('two', 'home', [{'key': 'song', 'rank': 1,
        'model_version': first}], 100)
    store.rec_ledger._prune_impression_batch(50)
    assert {row[0]: row[1] for row in store.conn.execute('SELECT model_version,served FROM rec_policy_variants')} == {
        first: 1, second: 0}
    assert store.conn.execute('SELECT served FROM rec_policy_bases').fetchone()[0] == 1
    store.rec_ledger._prune_impression_batch(101)
    assert [row[0] for row in store.conn.execute('SELECT served FROM rec_policy_variants')] == [0, 0]
    assert store.conn.execute('SELECT served FROM rec_policy_bases').fetchone()[0] == 0


def test_quota_queries_use_partial_indexes_and_do_not_traverse_served_history(store):
    rows, base = _publish(store)
    # The publication queries must enter through the bounded unserved indexes.
    plans = [row[3] for row in store.conn.execute('EXPLAIN QUERY PLAN '
        'SELECT count(*),sum(length(p.configuration)) FROM rec_policy_variants v '
        'JOIN rec_serving_policies p USING(model_version) WHERE v.served=0')]
    assert any('ix_rec_policy_unused_variants' in line for line in plans), plans
    plans = [row[3] for row in store.conn.execute('EXPLAIN QUERY PLAN '
        'SELECT sum(byte_size) FROM rec_policy_bases WHERE served=0')]
    assert any('ix_rec_policy_unused_bases' in line for line in plans), plans


def test_legacy_cleanup_limits_payload_work_and_resumes_from_cursor(store):
    raw = json.dumps({'evidence': 'x' * 1000000})
    with store.conn:
        store.conn.executemany('INSERT INTO rec_serving_policies VALUES (?,?,1)',
                              [(f'legacy-{n:03}', raw) for n in range(12)])
    assert measurement.maintain_storage(store, 10000)
    remaining = store.conn.execute('SELECT count(*) FROM rec_serving_policies').fetchone()[0]
    assert 0 < remaining < 12
    assert store.settings.get_setting(policy_storage.CURSOR) is not None
    assert measurement.maintain_storage(store, 10000)
    assert store.conn.execute('SELECT count(*) FROM rec_serving_policies').fetchone()[0] == 0


def test_orphan_body_collection_also_limits_payload_work(store):
    for n in range(12):
        _publish(store, rankers=(), body={'evidence': 'x' * 1000000, 'generation': n})
    assert measurement.maintain_storage(store, 10000)
    remaining = store.conn.execute('SELECT count(*) FROM rec_policy_bases').fetchone()[0]
    assert 0 < remaining < 12
    assert measurement.maintain_storage(store, 10000)
    assert store.conn.execute('SELECT count(*) FROM rec_policy_bases').fetchone()[0] == 0
