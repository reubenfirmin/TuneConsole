"""Real staged publication keeps policy claims, heads and captured settings coherent."""
import pytest

from tests.core.test_compact_groups_migration import predecessor
from tests.repos.test_compact_group_publication import database
from tests.repos.test_typed_recommendation_publication import recommendations
from yt_playlist.rec import measurement, mode_surfaces
from yt_playlist.repos import recommendation_publication as publication


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


def test_staged_worker_publishes_captured_policy_generation_and_settings_together(recommendations, policies):
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


def test_guard_conflict_keeps_last_heads_and_settings_after_private_staging(recommendations, policies):
    store = recommendations
    first = publish(store)
    store.settings.set_setting('bundle-guard', 'before')
    assert publish(store, now=11, guard='changed') is None
    assert store.recommendations.head('home')['id'] == store.recommendations.head('fresh')['id'] == first
    assert store.settings.get_setting('rec_candidate_preferences') == '10'
    assert store.conn.execute("SELECT count(*) FROM recommendation_generations WHERE status='ready'").fetchone()[0] == 1


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


def test_ordinary_worker_uses_typed_publication_without_serializing_a_legacy_bundle(recommendations, policies, monkeypatch):
    store = recommendations
    monkeypatch.setattr(measurement, 'maintain_storage', lambda *args: None)
    def calculate(reader, now, captured):
        items = captured.stamp([{'key': 'one', 'score': .9}], 'wheelhouse')
        return {'all': {'wheelhouse': items}, '_meta': {'full_pools': True}}, []
    monkeypatch.setattr(mode_surfaces, '_calculate_bundles', calculate)
    monkeypatch.setattr(mode_surfaces, 'encode_proposals', lambda *args: pytest.fail('Encoded the obsolete bundle'))
    result = mode_surfaces.prepare_bundles(store, 10)
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
