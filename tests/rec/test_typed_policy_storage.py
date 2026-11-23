"""The existing ledger boundary writes and retains file-backed policy owners."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import replace
import json
from pathlib import Path
from threading import Event
import time

import pytest

from tests.legacy_recommendation_fixture_support import recommendations
from tests.repos.test_typed_recommendation_publication import plan, publish, collect
from yt_playlist.core import (policy_config, storage_maintenance,
                              policy_documents, recommendation_pools as pools)
from yt_playlist.core.activity import work_budget
from yt_playlist.core.evidence_ledger import canonical
from yt_playlist.repos import policy_storage, recommendation_publication as publication


def test_ledger_policy_round_trip_capture_reuse_and_impression_accounting(recommendations):
    store = recommendations
    configuration = {'algorithm': 'explicit-test-policy', 'params': {'minimum': 2}}
    variant, base = policy_config.pack(configuration, 10, version='typed-offer')
    store.rec_ledger.record_prepared_policies([variant], [base])
    assert store.rec_ledger.policy_configuration('typed-offer') == configuration
    captured, bodies = store.rec_ledger.captured_policies(['typed-offer'])
    assert [row[0] for row in captured] == ['typed-offer']
    assert len(bodies) == 1
    files = list(store._db_path.parent.glob(store._db_path.name + '.policies/*/*.json'))
    store.rec_ledger.record_prepared_policies(captured, bodies)
    assert list(store._db_path.parent.glob(store._db_path.name + '.policies/*/*.json')) == files
    store.rec_ledger.record_served_impressions('typed-request', 'home', [
        {'key': 'one', 'rank': 1, 'model_version': 'typed-offer', 'provenance': {'pool_policy': 'typed-offer'}}],
        11, policies=captured, bases=bodies)
    assert store.conn.execute('SELECT primary_policy_version,pool_policy_version FROM rec_served_impressions '
        "WHERE request_id='typed-request'").fetchone()[:] == ('typed-offer', 'typed-offer')
    assert store.conn.execute("SELECT served FROM recommendation_policies WHERE version='typed-offer'").fetchone()[0] == 1
    with store.conn:
        result = store.rec_ledger.prune_policies(['typed-offer'])
    assert result['policies_deleted'] == 0


def test_new_generation_reference_is_rechecked_at_policy_retirement(recommendations):
    store = recommendations
    store.rec_ledger.record_serving_policy('generation-policy', {'algorithm': 'real-saved-policy'}, -10000)
    candidates, _ = store.rec_ledger.policy_retention_candidates(100)
    assert 'generation-policy' in candidates
    original = plan(store)
    ranked = pools.prepare('wheelhouse', 'generation-policy', 'd' * 64, 'card', [pools.Candidate(1, .9)])
    prepared = replace(original, policy_version='generation-policy',
                       slots=(publication.Slot('home', 'all', 'wheelhouse', ranked),))
    publish(store, prepared)
    with store.conn:
        store.rec_ledger.prune_policies(candidates)
    assert store.rec_ledger.policy_configuration('generation-policy') == {'algorithm': 'real-saved-policy'}
    assert not store.conn.execute('PRAGMA foreign_key_check').fetchall()


def test_clicked_pool_references_remain_protected_until_retirement(recommendations):
    store = recommendations
    store.rec_ledger.record_serving_policy('old-prepared-policy', {'algorithm': 'old-prepared'}, -10000)
    store.rec_proposals.put_proposals('home_pool:wheelhouse', {
        'parameters': {'lane': 'wheelhouse', 'epoch': 0, 'include_genres': [], 'avoid_genres': []},
        'tracks': [{'key': 'one', 'pool_policy': 'old-prepared-policy'}]}, 10)
    candidates, _ = store.rec_ledger.policy_retention_candidates(100)
    assert 'old-prepared-policy' not in candidates
    store.recommendations.repository.clear_surface('home_pool:wheelhouse')
    collect(store)
    candidates, _ = store.rec_ledger.policy_retention_candidates(100)
    assert 'old-prepared-policy' in candidates
    with store.conn:
        store.rec_ledger.prune_policies(candidates)
    assert store.rec_ledger.policy_configuration('old-prepared-policy') is None


def test_budget_failure_rolls_back_before_publishing_policy_files(recommendations, monkeypatch):
    store = recommendations
    files = list(store._db_path.parent.glob(store._db_path.name + '.policies/*/*.json'))
    monkeypatch.setattr(policy_config, 'MAX_BATCH_BYTES', 4)
    with pytest.raises(policy_config.PolicyStorageFull, match='batch'):
        store.rec_ledger.record_prepared_policies([('too-large', json.dumps({'algorithm': 'a-large-new-policy'}), 10)])
    assert not store.conn.in_transaction
    assert store.rec_ledger.policy_configuration('too-large') is None
    assert list(store._db_path.parent.glob(store._db_path.name + '.policies/*/*.json')) == files


def test_immutable_policy_conflict_does_not_change_the_accepted_configuration(recommendations):
    store = recommendations
    store.rec_ledger.record_serving_policy('immutable', {'algorithm': 'first'}, 10)
    with pytest.raises(ValueError, match='immutable'):
        store.rec_ledger.record_serving_policy('immutable', {'algorithm': 'different'}, 11)
    assert store.rec_ledger.policy_configuration('immutable') == {'algorithm': 'first'}


def test_policy_collection_waiting_for_writer_keeps_publication_and_reads_available(file_backed_store, monkeypatch):
    store = file_backed_store
    retention = store.retention.managed
    root = policy_documents.directory(store.conn)
    orphan, _, _ = policy_documents.publish(root, {'orphan': True})
    collection_waiting = Event()
    reservation = retention.repository.reservation

    @contextmanager
    def observe_reservation(domain):
        collection_waiting.set()
        with reservation(domain):
            yield

    def read_saved_policy():
        with work_budget(5), store.read_view() as reader:
            return reader.rec_ledger.policy_configuration('published')

    monkeypatch.setattr(retention.repository, 'reservation', observe_reservation)
    saved = {'algorithm': 'previously-published'}
    now = time.time()
    store.rec_ledger.record_serving_policy('published', saved, now)
    try:
        with ThreadPoolExecutor(2) as workers:
            with work_budget(5), store._lock:
                collection = workers.submit(retention.maintain, now + 31 * 86400)
                assert collection_waiting.wait(4)
                read = workers.submit(read_saved_policy)
                store.rec_ledger.record_serving_policy('new', {'algorithm': 'new'}, now)
                assert read.result(timeout=4) == saved
            assert collection.result(timeout=4) == 1
    finally:
        retention.close()

    assert not Path(orphan).exists()
    assert store.rec_ledger.policy_configuration('published') == saved
    assert store.rec_ledger.policy_configuration('new') == {'algorithm': 'new'}


def test_unused_budget_counts_canonical_record_wrappers_before_file_publication(recommendations, monkeypatch):
    store = recommendations
    configuration = {'algorithm': 'small-new-variant'}
    raw = json.dumps(configuration)
    variants = store.conn.execute('SELECT COALESCE(sum(byte_size),0) FROM recommendation_policies WHERE served=0').fetchone()[0]
    bodies = store.conn.execute('SELECT COALESCE(sum(byte_size),0) FROM recommendation_policy_body_refs WHERE served=0').fetchone()[0]
    encoded_size = len(canonical({'format': policy_documents.FORMAT, 'configuration': configuration}))
    assert encoded_size > len(raw.encode())
    monkeypatch.setattr(policy_config, 'MAX_UNUSED_BYTES', variants + bodies + encoded_size - 1)
    files = list(store._db_path.parent.glob(store._db_path.name + '.policies/*/*.json'))
    with pytest.raises(policy_config.PolicyStorageFull, match='Unused'):
        store.rec_ledger.record_prepared_policies([('wrapper-budget', raw, 10)])
    assert store.rec_ledger.policy_configuration('wrapper-budget') is None
    assert list(store._db_path.parent.glob(store._db_path.name + '.policies/*/*.json')) == files


def test_recovery_cleanup_uses_actual_typed_owners_and_preserves_retained_semantics(recommendations):
    store = recommendations
    publish(store, plan(store))
    policy = store.recommendations.head('home')['policy_version']
    expected = store.rec_ledger.policy_configuration(policy)
    store.rec_ledger.record_serving_policy('recovery-unused', {'algorithm': 'unused'}, -10000)
    before = [tuple(row) for row in store.conn.execute('SELECT * FROM rec_served_impressions')]
    deleted = 0
    while True:
        candidates, more = store.rec_ledger.policy_retention_candidates(100)
        with store.conn:
            deleted += store.rec_ledger.prune_policies(candidates)['policies_deleted']
        if not more:
            break
    assert deleted >= 1
    assert store.rec_ledger.policy_configuration('recovery-unused') is None
    assert store.rec_ledger.policy_configuration(policy) == expected
    assert [tuple(row) for row in store.conn.execute('SELECT * FROM rec_served_impressions')] == before
    assert not store.conn.execute('PRAGMA foreign_key_check').fetchone()


def test_current_startup_preserves_old_repair_marker_without_pausing_recording(file_backed_store):
    # A fully admitted current database has completed regular DDL. The typed
    # recommendation fixture deliberately owns only a partial historical schema.
    store = file_backed_store
    marker = json.dumps({'phase': 'sharing', 'cursor': 'old'})
    store.settings.set_setting('repair_shared_policies_v1', marker)
    maintenance = storage_maintenance.StorageMaintenance(store)
    assert not maintenance.status()['active'] and not store.activity_paused
    assert store.conn.execute('PRAGMA query_only').fetchone()[0] == 0
    assert store.settings.get_setting('repair_shared_policies_v1') == marker
