"""Real staged publication keeps policy claims, heads and captured settings coherent."""
import sqlite3

import pytest

from tests.core.test_compact_groups_migration import predecessor
from tests.repos.test_compact_group_publication import database
from tests.repos.test_bundle_storage import recommendations, historical_recommendations, collect
from yt_playlist.rec import measurement, mode_surfaces
from yt_playlist.repos import policy_storage, recommendation_publication as publication


@pytest.fixture
def policies(monkeypatch):
    monkeypatch.setattr(measurement, 'serving_policy', lambda store, now: (
        'captured', {'algorithm': 'actual-controlled-inputs', 'params': {'minimum': 2}, 'ranker': 'lane'}))


def publish(store, *, now=10, guard=None):
    with store.read_snapshot() as snapshot:
        policies = measurement.PoolPolicies(snapshot.store, now)
        items = policies.stamp([{'key': 'one', 'score': .9, 'reason': 'actual explanation'}], 'wheelhouse')
        value = {'all': {'wheelhouse': items}, '_meta': {'full_pools': True}}
        if guard is not None:
            snapshot.store.settings.get_setting('bundle-guard')
            store.settings.set_setting('bundle-guard', guard)
        return mode_surfaces._publish_typed_bundles(store, snapshot, value, [], policies,
                                                  {'rec_candidate_preferences': str(now)}, now)


@pytest.mark.parametrize('retired', [False, True])
def test_staged_worker_publishes_captured_policy_generation_and_settings_together(recommendations, policies, monkeypatch, retired):
    store = recommendations
    generation = publish(store)
    assert generation is not None
    assert {store.recommendations.head(surface)['id'] for surface in ('home', 'fresh')} == {generation}
    head = store.recommendations.head('home')
    assert head['input_revision'] == store.prepared_content.serial()
    assert store.rec_ledger.policy_configuration(head['policy_version']) == {
        'algorithm': 'actual-controlled-inputs', 'params': {'minimum': 2}, 'ranker': 'prepared-bundle'}
    assert store.settings.get_setting('rec_candidate_preferences') == '10'
    assert not store.conn.execute('SELECT 1 FROM recommendation_builds').fetchone()
    assert not store.conn.execute("SELECT 1 FROM rec_proposals WHERE surface IN ('mode_bundles','fresh_pool')").fetchone()
    assert not store.conn.execute('PRAGMA foreign_key_check').fetchall()
    first_pool = store.recommendations.page('home', 'all', 'wheelhouse')[0]['pool_policy']
    before = store.conn.execute('SELECT count(*) FROM recommendation_candidates').fetchone()[0]
    monkeypatch.setattr(measurement, 'serving_policy', lambda reader, now: (
        'new-inputs', {'algorithm': 'new-captured-inputs', 'params': {'minimum': 3}, 'ranker': 'lane'}))
    claim, claims = store.recommendations.claim, []
    def capture_claim(plan, **kwargs):
        claims.append((plan, kwargs['policies'], kwargs['bases']))
        if retired:
            with store.conn:
                store.conn.execute('DELETE FROM recommendation_heads WHERE generation_id=?', (generation,))
            collect(store)
            assert not store.conn.execute('SELECT 1 FROM recommendation_pools').fetchone()
            with store.conn:
                result = policy_storage.prune(store.conn, [head['policy_version'], first_pool], None)
            assert result['policies_deleted'] == 2
            assert store.rec_ledger.policy_configuration(first_pool) is None
        return claim(plan, **kwargs)
    monkeypatch.setattr(store.recommendations, 'claim', capture_claim)
    assert publish(store, now=11) != generation
    plan, rows, bases = claims[0]
    # Unchanged facts retain their original immutable policy. Serving selects
    # the newly captured policy even if numerical scores happened to be capped.
    assert plan.policy_version != head['policy_version']
    assert {row[0] for row in rows} == {plan.policy_version, *(slot.pool.policy_version for slot in plan.slots),
                                      *(slot.revalidated_policy for slot in plan.slots if slot.revalidated)}
    assert len(bases) == 2
    assert store.rec_ledger.policy_configuration(plan.policy_version)['params'] == {'minimum': 3}
    assert store.rec_ledger.policy_configuration(first_pool)['params'] == {'minimum': 2}
    current_policy = store.recommendations.page('home', 'all', 'wheelhouse')[0]['pool_policy']
    assert store.rec_ledger.policy_configuration(current_policy)['params'] == {'minimum': 3}
    # The regular schema worker can install this owner after a long-lived
    # serving Store was bound. A cheap direct lookup must adopt that new table.
    store._recommendation_revalidations = False
    assert store.recommendations.pool('home', 'all', 'wheelhouse')['source_policy_version'] == first_pool
    with store.conn:
        assert policy_storage.prune(store.conn, [row[0] for row in rows], None)['policies_deleted'] == 0
    assert store.conn.execute('SELECT count(*) FROM recommendation_candidates').fetchone()[0] == before
    assert store.settings.get_setting('rec_candidate_preferences') == '11'


def test_input_changes_publish_captured_heads_and_settings_after_private_staging(recommendations, policies):
    store = recommendations
    first = publish(store)
    store.settings.set_setting('bundle-guard', 'before')
    second = publish(store, now=11, guard='changed')
    assert second is not None and second != first
    assert store.recommendations.head('home')['id'] == store.recommendations.head('fresh')['id'] == second
    assert store.settings.get_setting('rec_candidate_preferences') == '11'
    assert store.conn.execute("SELECT count(*) FROM recommendation_generations WHERE status='ready'").fetchone()[0] == 0


def test_late_head_switch_failure_rolls_back_both_heads_and_settings(recommendations, policies, monkeypatch):
    store = recommendations
    first = publish(store)
    original = publication.finish
    def fail(*args, **kwargs):
        original(*args, **kwargs)
        raise RuntimeError('controlled late publication failure')
    monkeypatch.setattr(publication, 'finish', fail)
    with pytest.raises(RuntimeError, match='controlled late'):
        publish(store, now=11)
    assert store.recommendations.head('home')['id'] == store.recommendations.head('fresh')['id'] == first
    assert store.settings.get_setting('rec_candidate_preferences') == '10'


def test_empty_first_preparation_captures_actual_inputs_without_inventing_a_past_policy(recommendations, policies):
    store = recommendations
    with store.read_snapshot() as snapshot:
        captured = measurement.PoolPolicies(snapshot.store, 10)
        assert not captured.rows
        generation = mode_surfaces._publish_typed_bundles(store, snapshot, {}, [], captured, {}, 10)
    head = store.recommendations.head('home')
    assert head['id'] == generation
    assert store.rec_ledger.policy_configuration(head['policy_version'])['ranker'] == 'prepared-bundle'
    assert store.recommendations.output('home', 'empty') == {}


@pytest.mark.parametrize('capacity', ['available', 'released', 'changed-inputs', 'invalid-batch'])
def test_ordinary_worker_uses_typed_publication_without_serializing_a_legacy_bundle(recommendations, policies, monkeypatch, capacity):
    store = recommendations
    first = publish(store)
    store.rec_ledger.record_serving_policy('expired', {'algorithm': 'unowned'}, -10000)
    # Give the next build genuinely new captured policies rather than reuse the
    # initial generation. One expired policy occupies the missing headroom.
    monkeypatch.setattr(measurement, 'serving_policy', lambda reader, now: (
        'next', {'algorithm': 'next-inputs', 'ranker': 'lane'}))
    monkeypatch.setattr(measurement, 'maintain_storage', lambda *args: None)
    calculations = []
    def calculate(reader, now, captured):
        calculations.append(now)
        reader.settings.get_setting('bundle-guard')
        items = captured.stamp([{'key': 'one', 'score': .8}], 'wheelhouse')
        lanes = {'wheelhouse': items}
        return {'all': lanes, '_meta': {'full_pools': True}}, []
    monkeypatch.setattr(mode_surfaces, '_calculate_bundles', calculate)
    monkeypatch.setattr(mode_surfaces, 'encode_proposals', lambda *args: pytest.fail('Encoded the obsolete bundle'))
    if capacity == 'invalid-batch':
        monkeypatch.setattr(policy_storage, 'MAX_BATCH_BYTES', 4)
        with pytest.raises(ValueError, match='publication budget'):
            mode_surfaces.prepare_bundles(store, 11)
        assert not hasattr(store, '_deferred_bundles')
        assert store.recommendations.head('home')['id'] == first
        return
    if capacity != 'available':
        count = store.conn.execute('SELECT count(*) FROM recommendation_policies WHERE served=0').fetchone()[0]
        additional = 1
        monkeypatch.setattr(policy_storage, 'MAX_UNUSED_VARIANTS', count + additional)
        for now in (11, 12, 13):
            with pytest.raises(policy_storage.PolicyCapacityFull, match=f'batch adds {additional+1} variants'):
                mode_surfaces.prepare_bundles(store, now)
            assert store.recommendations.head('home')['id'] == first
            assert not store.conn.in_transaction
            assert not store.conn.execute('SELECT 1 FROM recommendation_builds').fetchone()
        assert calculations == [11], 'Capacity retries repeated ranking'
        deferred = store._deferred_bundles
        assert deferred.snapshot._reader_released
        with pytest.raises(sqlite3.ProgrammingError, match='closed database'):
            deferred.snapshot.store.conn.execute('SELECT 1')
        if capacity == 'changed-inputs':
            store.settings.set_setting('bundle-guard', 'changed')
        with store.conn:
            pruned = policy_storage.prune(store.conn, ['expired'], None)
        assert pruned['policies_deleted'] == 1
        result = mode_surfaces.prepare_bundles(store, 14)
        assert result['all']['wheelhouse'][0]['key'] == 'one'
        assert calculations == [11]
        assert not hasattr(store, '_deferred_bundles')
        assert store.recommendations.head('home')['id'] != first
        assert not store.conn.execute('PRAGMA foreign_key_check').fetchall()
        return
    result = mode_surfaces.prepare_bundles(store, 15)
    assert result['all']['wheelhouse'][0]['key'] == 'one'
    assert store.recommendations.head('home') is not None
    assert not store.conn.execute("SELECT 1 FROM rec_proposals WHERE surface IN ('mode_bundles','fresh_pool')").fetchone()


def test_readiness_follows_committed_heads_without_reading_legacy_documents(recommendations, policies, monkeypatch):
    store = recommendations
    from yt_playlist.repos import proposal_cache
    monkeypatch.setattr(proposal_cache, 'head', lambda *args, **kwargs: pytest.fail('Read a legacy pool head'))
    assert not store.rec_proposals.has_proposals('mode_bundles')
    assert not store.rec_proposals.has_proposals('fresh_pool')
    publish(store)
    assert store.rec_proposals.has_proposals('mode_bundles')
    assert store.rec_proposals.has_proposals('fresh_pool')
    assert mode_surfaces.bundle_metadata(store) == {'full_pools': 1}
