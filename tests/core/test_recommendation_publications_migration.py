"""The active cache, its policies and complete ranks relocate in one transaction."""
import json

import pytest

from tests.core.test_compact_groups_migration import predecessor
from tests.repos.test_compact_group_publication import database
from yt_playlist.core import json_cache, recommendation_outputs as outputs
from yt_playlist.core.migrations import v073_recommendation_publications as migration
from yt_playlist.core.migrations.runner import Context, MigrationError
from yt_playlist.repos import proposal_cache, recommendation_publication as publication


@pytest.mark.parametrize('format', ['plain', 'v1', 'v2'])
def test_conversion_keeps_input_digests_and_complete_unresolved_evidence_without_profile_copies(format):
    profile = {'digest': 'input', 'samples': ['large retained source'] * 1000}
    value = {'all': {'wheelhouse': [{'key': key, 'musical': profile} for key in ('known', 'unknown')]}}
    if format == 'plain':
        encoded = json.dumps(value)
    elif format == 'v1':
        encoded = json.dumps({json_cache.MARKER: json_cache.LEGACY_FORMAT,
                              'json': json_cache._pack(json.dumps(value))})
    else:
        encoded = json_cache.encode(value, group_pools=True)
    result = json_cache.decode(encoded, profile_fields=('digest',),
                               preserve_profiles=lambda item: item.get('key') == 'unknown', max_bytes=1_000_000)
    known, unknown = result['all']['wheelhouse']
    assert known['musical'] == {'digest': 'input'}
    assert unknown['musical'] == profile
    assert value['all']['wheelhouse'][0]['musical'] == profile


def test_decompressed_source_budget_counts_all_envelope_sections():
    # Each individual section fits; together they exceed the admitted expansion.
    value = {'all': {'large': 'x' * 40_000}, 'mode': {'large': 'y' * 40_000}}
    encoded = json_cache.encode(value, group_pools=True)
    assert json.loads(encoded)[json_cache.MARKER] == json_cache.FORMAT
    with pytest.raises(ValueError, match='compressed recommendation cache'):
        json_cache.decode(encoded, max_bytes=60_000)


def seed(store, *, fresh_only=False):
    policy = store.conn.execute('SELECT policy_version FROM group_generations LIMIT 1').fetchone()[0]
    items = [dict(key=key, score=score, pool_policy=policy, reason='retained explanation ' + key)
             for key, score in [('two', .9), ('one', .8), ('missing-exact-key', .7)]]
    bundle = {'all': {'wheelhouse': items}, 'rare': {'wheelhouse': items[1:]},
              '_meta': {'full_pools': True, 'year_cuts': [1980, 2010]},
              '_radio': {'members': {'rare': ['one', 'two']}, 'scores': {'one': .8, 'two': .9}},
              '_ppr': {'rare': ['two', 'one']}}
    if fresh_only:
        store.rec_proposals.put_proposals('fresh_pool', items, 12)
    else:
        # Exercise the real staged envelope, not only a plain legacy JSON row.
        bundle['all']['wheelhouse'][0]['reason'] += 'x' * 100_000
        from yt_playlist.repos.rec_proposals import encode_proposals
        encoded = encode_proposals('mode_bundles', bundle)
        assert json.loads(encoded)[json_cache.MARKER] == json_cache.FORMAT
        prepared = store.rec_proposals.stage_prepared_bundles(encoded, encode_proposals('fresh_pool', items))
        with store.conn:
            store.conn.execute('BEGIN IMMEDIATE')
            store.rec_proposals.publish_prepared_bundles(*prepared, 12)
    return bundle, items


def apply(store):
    with store.conn:
        store.conn.execute('BEGIN IMMEDIATE')
        migration.apply(store.conn, Context(99))
    migration.validate(store.conn)
    store._bind_repositories()


def test_staged_complete_cache_becomes_typed_ranks_before_old_storage_is_retired(database):
    store, _ = database
    bundle, fresh = seed(store)
    apply(store)
    head = store.recommendations.head('home')
    assert head['input_revision'] is None and head['revision_reason'] == 'legacy_not_recorded'
    assert head['group_generation_id'] is None
    assert head['created_at'] == head['published_at'] == 12
    assert [row['key'] for row in store.recommendations.page('home', 'rare', 'wheelhouse')] == ['one']
    assert [row['key'] for row in store.recommendations.page('radio', 'rare', 'membership')] == ['one', 'two']
    assert [row['key'] for row in store.recommendations.page('radio', 'rare', 'ppr')] == ['two', 'one']
    missing = store.recommendations.display('fresh', 'all', 'fresh', [2])[2]
    assert missing['unresolved_candidate'] == fresh[2]
    assert store.rec_proposals.get_proposals('mode_bundles') is None
    assert proposal_cache.dependencies(store.conn) == set()
    assert not store.conn.execute('PRAGMA foreign_key_check').fetchone()
    # An ordinary remaining display update must not run a retired-cache trigger.
    store.rec_proposals.put_proposals('unrelated-display', {'revision': 1}, 13)
    store.rec_proposals.put_proposals('unrelated-display', {'revision': 2}, 14)
    assert store.rec_proposals.get_proposals('unrelated-display') == {'revision': 2}


@pytest.mark.parametrize('problem', ['policy', 'candidate', 'compressed'])
def test_invalid_source_fails_before_any_new_owner_or_file_is_created(database, problem):
    store, _ = database
    bundle, _ = seed(store)
    with store.conn:
        # A legacy write invalidates the staged head through its existing trigger.
        if problem == 'policy':
            bundle['all']['wheelhouse'][0]['pool_policy'] = 'missing-policy'
        elif problem == 'candidate':
            bundle['rare']['wheelhouse'][0]['new_queried_field'] = True
        raw = json.dumps(bundle) if problem != 'compressed' else json.dumps(
            {json_cache.MARKER: json_cache.FORMAT, 'json': 'broken', 'profiles': 'broken'})
        store.conn.execute("UPDATE rec_proposals SET payload=? WHERE surface='mode_bundles'", (raw,))
    changes = store.conn.total_changes
    with pytest.raises(MigrationError, match='Invalid original recommendation'), store.conn:
        store.conn.execute('BEGIN IMMEDIATE')
        migration.apply(store.conn, Context(99))
    assert store.conn.total_changes == changes
    assert not outputs.directory(store.conn).exists()
    assert not store.conn.execute("SELECT 1 FROM sqlite_master WHERE name='recommendation_generations'").fetchone()
    assert proposal_cache.read(store.conn, 'fresh_pool') is not None


def test_late_interruption_restores_old_heads_and_restart_can_reuse_durable_files(database, monkeypatch):
    store, _ = database
    seed(store)
    before = [tuple(row) for row in store.conn.execute('SELECT * FROM rec_proposal_cache_heads ORDER BY surface')]
    original = publication._output
    def interrupted(*args):
        original(*args)
        raise RuntimeError('controlled relocation interruption')
    monkeypatch.setattr(publication, '_output', interrupted)
    with pytest.raises(RuntimeError, match='controlled relocation'), store.conn:
        store.conn.execute('BEGIN IMMEDIATE')
        migration.apply(store.conn, Context(99))
    assert before == [tuple(row) for row in store.conn.execute('SELECT * FROM rec_proposal_cache_heads ORDER BY surface')]
    assert proposal_cache.read(store.conn, 'mode_bundles') is not None
    assert not store.conn.execute("SELECT 1 FROM sqlite_master WHERE name='recommendation_heads'").fetchone()
    monkeypatch.setattr(publication, '_output', original)
    apply(store)
    assert store.recommendations.head('home')


def test_fresh_only_history_does_not_invent_a_home_or_group_generation(database):
    store, _ = database
    seed(store, fresh_only=True)
    apply(store)
    assert store.recommendations.head('home') is None
    assert store.recommendations.head('fresh')['group_generation_id'] is None
    assert [row['key'] for row in store.recommendations.page('fresh', 'all', 'fresh')] == ['two', 'one']
