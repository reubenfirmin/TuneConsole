"""Storage growth, retention races and exact attribution for prepared policies."""
import json
import sqlite3
from types import SimpleNamespace

import pytest

from tests.conftest import seed_catalog_track
from yt_playlist.core import db_space, policy_config, radio_storage
from yt_playlist.core.store import Store
from yt_playlist.rec import candidate_refresh, measurement, mode_surfaces, scorecard
from yt_playlist.repos import policy_storage


def _publish(store, now=1, rankers=('first', 'second'), body=None):
    config = body or {'weights': {'genre:rock': 1.4}, 'ranker': 'lane'}
    base = policy_config.base_record(config, now)
    rows = [policy_config.variant_record(base[0], {'ranker': ranker}, now) for ranker in rankers]
    store.rec_ledger.record_prepared_policies(rows, [base])
    return rows, base


@pytest.fixture
def legacy_policy_store(tmp_path):
    """The genuine SQL-policy predecessor for its retained cleanup contracts."""
    from tests.legacy_policy_support import open_legacy as _open_legacy

    store = _open_legacy(tmp_path / 'legacy-policies.db')
    try:
        yield store
    finally:
        store.conn.close()


def test_pool_variants_share_large_body_and_preserve_exact_configuration(store, monkeypatch):
    body = {'weights': {'genre:rock': 1.23456789}, 'ranker': 'lane', 'evidence': 'x' * 48000}
    monkeypatch.setattr(measurement, 'serving_policy', lambda *args: ('base', body))
    policies = measurement.PoolPolicies(store, 100)
    items = [policies.stamp([{'key': 'song'}], f'vibe:lane:family-{n}')[0] for n in range(1008)]
    store.rec_ledger.record_prepared_policies(policies.rows.values(), policies.bases.values())
    assert len(policies.bases) == 1
    assert len(policies.rows) == 1008
    assert len({item['pool_policy'] for item in items}) == 1008
    size = store.conn.execute('SELECT sum(byte_size) FROM recommendation_policies').fetchone()[0]
    size += store.conn.execute('SELECT sum(byte_size) FROM recommendation_policy_bodies').fetchone()[0]
    assert size < 512000  # Includes canonical file wrappers, versus about 48 MB unshared.
    assert store.rec_ledger.policy_configuration(items[-1]['pool_policy']) == {
        **body, 'ranker': 'vibe:lane:family-1007'}


def test_idle_collection_reports_bounded_progress_and_matches_rebuild_closure(store, monkeypatch):
    for index in range(5):
        store.rec_ledger.record_serving_policy('unused-'+str(index), {'value':index}, 1)
    store.rec_ledger.record_serving_policy('live', {'value':'live'}, 9999)
    closure = policy_storage.retained_closure(store.conn,10000)
    assert closure['recommendation_policies'] == {'live'}
    monkeypatch.setattr(policy_storage,'SCAN_LIMIT',2)
    progress = []
    for _ in range(4):
        result = measurement.maintain_storage(store,10000,progress=True)
        progress.append(result)
        if not result['pending']:
            break
    assert [row['policies_deleted'] for row in progress] == [2,2,1]
    assert store.rec_ledger.policy_configuration('live') == {'value':'live'}
    assert {row[0] for row in store.conn.execute('SELECT version FROM recommendation_policies')} == closure['recommendation_policies']


def check_preparation_retention(store, monkeypatch, *, generations, variants, evidence_bytes,
                                maximum_generations, byte_bound):
    monkeypatch.setattr(measurement, 'serving_policy', lambda reader, now: ('base', {
        'ranker': 'lane', 'weights': {'decayed': 1 + 1 / now}, 'evidence': 'x' * evidence_bytes}))
    monkeypatch.setattr(candidate_refresh, 'prepared_settings', lambda *args: {})
    seed_catalog_track(store, 'song', 'Song', 'Artist', video_id='song')
    def calculate(reader, now, policies):
        # Calculate several variants, but publish only the last one. Unused
        # calculated variants must not become retained policy identities.
        for n in range(variants):
            latest = policies.stamp([{'key': 'song'}], f'vibe:lane:family-{n}')
        return {'0': {'wheelhouse': latest}}, []
    monkeypatch.setattr(mode_surfaces, '_calculate_bundles', calculate)
    for tick in range(generations):
        now = 10000 + 120 * tick
        assert mode_surfaces.prepare_bundles(store, now) is not None
        if tick == 0:
            assert store.conn.execute('SELECT count(*) FROM recommendation_policies').fetchone()[0] == 2
            original_versions = {row[0] for row in store.conn.execute('SELECT version FROM recommendation_policies')}
        # Between preparation cycles the worker also retires the replaced pool.
        # Complete actual bounded maintenance before checking unused retention.
        for _ in range(3):
            if not store.recommendations.cleanup():
                break
        else:
            pytest.fail('Replaced recommendation generations did not finish bounded cleanup')
        # Retiring replaced pools releases their policy roots. Finish that
        # separate maintenance owner too, as the real worker does.
        assert measurement.maintain_storage(store, now)
    assert store.conn.execute('SELECT count(*) FROM rec_served_impressions').fetchone()[0] == 0
    policies = store.conn.execute('SELECT version,created_at,base_version FROM recommendation_policies').fetchall()
    # Unchanged tracks reuse the first immutable pool. Its original generation
    # and pool policies remain meaningful roots beyond the three-tick grace
    # window; later unused revalidation policies must still be collected.
    expired = {row['version'] for row in policies if row['created_at'] < now - policy_storage.GRACE_SECONDS}
    roots = {row[0] for row in store.conn.execute('SELECT policy_version FROM recommendation_generations '
        'UNION SELECT policy_version FROM recommendation_pools')}
    assert expired == original_versions
    assert expired <= roots
    assert len(policies) <= (maximum_generations + 1) * 2
    assert store.conn.execute('SELECT count(*) FROM recommendation_policy_bodies').fetchone()[0] <= maximum_generations + 1
    stored = store.conn.execute('SELECT sum(byte_size) FROM recommendation_policies').fetchone()[0]
    stored += store.conn.execute('SELECT sum(byte_size) FROM recommendation_policy_bodies').fetchone()[0]
    assert stored < byte_bound
    latest = store.recommendations.candidate_page('home', '0', 'wheelhouse', limit=1)['tracks'][0]['pool_policy']
    config = store.rec_ledger.policy_configuration(latest)
    assert config['weights']['decayed'] == 1 + 1 / (10000 + (generations - 1) * 120)
    assert config['ranker'] == f'vibe:lane:family-{variants - 1}'


def test_repeated_advancing_time_preparation_without_serving_has_bounded_bytes(store, monkeypatch):
    monkeypatch.setattr(policy_storage, 'GRACE_SECONDS', 240)
    check_preparation_retention(store, monkeypatch, generations=8, variants=4,
        evidence_bytes=256, maximum_generations=3, byte_bound=16000)


def test_unused_retention_protects_both_impression_paths_proposals_sessions_and_grace(store):
    for version, at in [('model', 1), ('42', 1), ('prepared', 1), ('session', 1), ('unused', 1), ('new', 9999)]:
        store.rec_ledger.record_serving_policy(version, {'policy': version}, at)
    store.rec_ledger.record_served_impressions('kept', 'home', [{'key': 'song', 'rank': 1,
        'model_version': 'model', 'provenance': {'pool_policy': 42}}], 10000)
    store.rec_proposals.put_proposals('home_choice', {'tracks': [{'pool_policy': 'prepared'}]}, 1)
    account = store.accounts.upsert_account('Retention', 'retention.json', None, True)
    with store.conn:
        radio_storage.save(store.conn, {'episode_id': 'retained-radio', 'identity': account,
            'phase': 'appending', 'tracks': [{'video_id': 'retained-video', 'pool_policy': 'session'}]},
            expected=None, now=1)
    assert measurement.maintain_storage(store, 10000)
    assert {row[0] for row in store.conn.execute('SELECT version FROM recommendation_policies UNION '
        "SELECT version FROM recommendation_policy_history WHERE state='archived'")} == {
        'model', '42', 'prepared', 'session', 'new'}
    store.rec_ledger._prune_policy_batch(10000)
    assert store.rec_ledger.policy_configuration('prepared') == {'policy': 'prepared'}
    assert store.rec_ledger.policy_configuration('session') == {'policy': 'session'}


def test_prepared_reference_change_defers_cleanup(file_backed_store, monkeypatch):
    store = file_backed_store
    store.rec_ledger.record_serving_policy('old-policy', {'exact': True}, 1)
    original = policy_storage.protected_references
    def raced(conn):
        result = original(conn)
        store.rec_proposals.put_proposals('home_choice', {'context': {'pool_policy': 'old-policy'}}, 10000)
        return result
    monkeypatch.setattr(policy_storage, 'protected_references', raced)
    try:
        assert measurement.maintain_storage(store, 10000) is False
        assert store.rec_ledger.policy_configuration('old-policy') == {'exact': True}
    finally:
        store.conn.close()


def test_queued_offer_restores_collected_pool_with_its_impression(file_backed_store):
    store = file_backed_store
    rows, base = _publish(store, rankers=('pool-ranker',))
    version = rows[0][0]
    offer = measurement.CapturedOffer(store, 'home', [{'key': 'song', 'pool_policy': version}], 2)
    try:
        assert measurement.maintain_storage(store, 10000)
        assert store.rec_ledger.policy_configuration(version) is None
        assert store.conn.execute('SELECT count(*) FROM recommendation_policy_bodies').fetchone()[0] == 0
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
    store.rec_proposals.put_proposals('home_choice', {'last-good': True}, 1)
    monkeypatch.setattr(policy_storage, budget, 1)
    with pytest.raises(policy_storage.PolicyStorageFull), store.conn:
        new_rows = [policy_config.variant_record(base[0], {'ranker': 'new'}, 2)]
        store.rec_ledger.publish_prepared_policies(new_rows, [base])
        store.rec_proposals.publish_prepared_home('{"last-good":false}', 2, surface='home_choice')
    assert store.rec_proposals.get_proposals('home_choice') == {'last-good': True}
    assert store.conn.execute('SELECT count(*) FROM recommendation_policies').fetchone()[0] == len(rows)


def test_failed_policy_publication_rolls_back_bodies_variants_and_links(store):
    row, base = policy_config.pack({'ranker': 'valid', 'exact': True}, 1)
    bad = policy_config.variant_record('missing-body', {'ranker': 'invalid'}, 1)
    with pytest.raises(ValueError, match='Missing shared recommendation policy body'):
        store.rec_ledger.record_prepared_policies([row, bad], [base])
    assert not store.conn.in_transaction
    for table in ('recommendation_policies', 'recommendation_policy_bodies', 'recommendation_impression_policies'):
        assert store.conn.execute(f'SELECT count(*) FROM {table}').fetchone()[0] == 0


def test_sqlite_full_rolls_back_new_policy_and_keeps_personal_data(file_backed_store):
    store = file_backed_store
    store.settings.set_setting('personal', 'keep')
    # Policy bodies live in files now. Force a genuine SQLITE_FULL during the
    # associated SQL owner insert, independent of the body's compression ratio.
    with store.conn:
        store.conn.execute('CREATE TABLE controlled_policy_growth(payload BLOB)')
        store.conn.execute('CREATE TRIGGER controlled_policy_full BEFORE INSERT ON recommendation_policies '
            'BEGIN INSERT INTO controlled_policy_growth VALUES (zeroblob(1000000)); END')
    pages = store.conn.execute('PRAGMA page_count').fetchone()[0]
    store.conn.execute(f'PRAGMA max_page_count={pages}')
    row, base = policy_config.pack({'ranker': 'large', 'evidence': 'x' * 1000000}, 1)
    try:
        with pytest.raises(sqlite3.OperationalError, match='full'):
            store.rec_ledger.record_prepared_policies([row], [base])
        assert not store.conn.in_transaction
        assert store.settings.get_setting('personal') == 'keep'
        assert store.conn.execute('SELECT count(*) FROM recommendation_policy_bodies').fetchone()[0] == 0
        assert store.conn.execute('SELECT count(*) FROM recommendation_policies').fetchone()[0] == 0
        assert store.conn.execute('SELECT count(*) FROM controlled_policy_growth').fetchone()[0] == 0
        assert store.conn.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
    finally:
        store.conn.close()


def test_low_headroom_allows_existing_startup_and_core_writes_but_defers_preparation(file_backed_store, monkeypatch):
    path = file_backed_store._db_path
    store = file_backed_store
    store.conn.close()
    monkeypatch.setattr(db_space.shutil, 'disk_usage', lambda path: SimpleNamespace(free=10 * 1024 * 1024))
    store = Store(path)
    try:
        store.init_schema()
        store.settings.set_setting('personal', 'saved')
        with pytest.raises(db_space.LowDiskSpace, match='10.0 MiB available'):
            _publish(store)
        assert store.settings.get_setting('personal') == 'saved'
        assert store.conn.execute('SELECT count(*) FROM recommendation_policies').fetchone()[0] == 0
    finally:
        store.conn.close()


def test_republishing_existing_variants_at_exact_budget_is_idempotent(store, monkeypatch):
    rows, base = _publish(store)
    size = store.conn.execute('SELECT sum(byte_size) FROM recommendation_policy_bodies').fetchone()[0]
    size += store.conn.execute('SELECT sum(byte_size) FROM recommendation_policies').fetchone()[0]
    monkeypatch.setattr(policy_storage, 'MAX_UNUSED_BYTES', size)
    monkeypatch.setattr(policy_storage, 'MAX_UNUSED_VARIANTS', len(rows))
    store.rec_ledger.record_prepared_policies(rows, [base])
    assert store.conn.execute('SELECT count(*) FROM recommendation_policies').fetchone()[0] == len(rows)
    assert store.conn.execute('SELECT sum(byte_size) FROM recommendation_policy_bodies').fetchone()[0] + \
        store.conn.execute('SELECT sum(byte_size) FROM recommendation_policies').fetchone()[0] == size


def test_quota_accounting_tracks_inserted_and_expired_impression_references(store):
    rows, base = _publish(store)
    first, second = (row[0] for row in rows)
    store.rec_ledger.record_served_impressions('one', 'home', [{'key': 'song', 'rank': 1,
        'model_version': first, 'provenance': {'pool_policy': second}}], 1)
    assert [row[0] for row in store.conn.execute('SELECT served FROM recommendation_policies')] == [1, 1]
    assert store.conn.execute('SELECT served FROM recommendation_policy_bodies').fetchone()[0] == 1
    store.rec_ledger.record_served_impressions('two', 'home', [{'key': 'song', 'rank': 1,
        'model_version': first}], 100)
    store.rec_ledger._prune_impression_batch(50)
    assert {row[0]: row[1] for row in store.conn.execute('SELECT version,served FROM recommendation_policies')} == {
        first: 1, second: 0}
    assert store.conn.execute('SELECT served FROM recommendation_policy_bodies').fetchone()[0] == 1
    store.rec_ledger._prune_impression_batch(101)
    assert [row[0] for row in store.conn.execute('SELECT served FROM recommendation_policies')] == [0, 0]
    assert store.conn.execute('SELECT served FROM recommendation_policy_bodies').fetchone()[0] == 0


def test_quota_queries_use_partial_indexes_and_do_not_traverse_served_history(store):
    rows, base = _publish(store)
    # The publication queries must enter through the bounded unserved indexes.
    plans = [row[3] for row in store.conn.execute('EXPLAIN QUERY PLAN '
        'SELECT count(*),sum(byte_size) FROM recommendation_policies WHERE served=0')]
    assert any('ix_recommendation_policies_unused' in line for line in plans), plans
    plans = [row[3] for row in store.conn.execute('EXPLAIN QUERY PLAN '
        'SELECT sum(byte_size) FROM recommendation_policy_bodies WHERE served=0')]
    assert any('ix_recommendation_policy_bodies_unused' in line for line in plans), plans


def test_legacy_cleanup_limits_payload_work_and_resumes_from_cursor(legacy_policy_store, monkeypatch):
    store = legacy_policy_store
    monkeypatch.setattr(policy_storage, 'SCAN_BYTES', 8 * 1024)
    raw = json.dumps({'evidence': 'x' * 1024})
    with store.conn:
        store.conn.executemany('INSERT INTO rec_serving_policies VALUES (?,?,1)',
                              [(f'legacy-{n:03}', raw) for n in range(12)])
    assert measurement.maintain_storage(store, 10000)
    remaining = store.conn.execute('SELECT count(*) FROM rec_serving_policies').fetchone()[0]
    assert 0 < remaining < 12
    assert store.settings.get_setting(policy_storage.CURSOR) is not None
    assert measurement.maintain_storage(store, 10000)
    assert store.conn.execute('SELECT count(*) FROM rec_serving_policies').fetchone()[0] == 0


def test_orphan_body_collection_also_limits_payload_work(legacy_policy_store, monkeypatch):
    store = legacy_policy_store
    monkeypatch.setattr(policy_storage, 'SCAN_BYTES', 8 * 1024)
    for n in range(12):
        _publish(store, rankers=(), body={'evidence': 'x' * 1024, 'generation': n})
    assert measurement.maintain_storage(store, 10000)
    remaining = store.conn.execute('SELECT count(*) FROM rec_policy_bases').fetchone()[0]
    assert 0 < remaining < 12
    assert measurement.maintain_storage(store, 10000)
    assert store.conn.execute('SELECT count(*) FROM rec_policy_bases').fetchone()[0] == 0
