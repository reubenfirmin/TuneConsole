"""Established bundle semantics survive conversion without copied catalog rows."""
from copy import deepcopy
from dataclasses import replace

import pytest

from tests.core.test_compact_groups_migration import predecessor
from tests.repos.test_compact_group_publication import database
from tests.repos.test_typed_recommendation_publication import recommendations, publish
from yt_playlist.core import recommendation_outputs as outputs, recommendation_pools as pools
from yt_playlist.rec import bundle_storage, home_vibe, musical_character, vibe_pools
from yt_playlist.repos import policy_storage, recommendation_publication as publication
from yt_playlist.repos.rec_proposals import RecProposalsRepo


def bundle(store):
    policy = store.conn.execute('SELECT policy_version FROM group_generations WHERE id=1').fetchone()[0]
    items = [dict(key=key, title='obsolete title', artist='obsolete artist', video_id='obsolete-upload',
                  genre='obsolete genre', year=1900, score=score, pool_policy=policy, reason='explanation ' + key,
                  musical={'digest': key + '-input', 'measurements': ['old', 'copied', 'history']})
             for key, score in [('one', .9), ('two', .7)]]
    return {'all': {'wheelhouse': deepcopy(items), 'fresh': deepcopy(items)},
            'small': {'wheelhouse': deepcopy(items)},
            '_meta': {'comfort_pool': 2, 'year_cuts': [1987, 2004], 'full_pools': True, 'unmodeled': False,
                      'musical_generation': 'musical-input', 'ppr_policy': {'algorithm': 'ppr-3'}},
            '_radio': {'generation': 'model-1', 'space': 'content-space', 'members': {'small': ['two', 'one']},
                       'scores': {'one': .9, 'two': .7},
                       'intensity': {'version': 1, 'profile_version': 'profile-2',
                                     'clusters': {'small': {'12': {'rare genre': ['two']}, '40': {'rock': ['one']}}}}},
            '_ppr': {'small': ['one', 'two']},
            '_vibes': {'version': 'vibes-5', 'profile_version': 'profile-2',
                       'pools': {'calm': {'comfort': deepcopy(items)}},
                       'genres': {'calm': {'comfort': {'rare genre': deepcopy(items)}}}}}


def prepare(store, value, fresh=None, key='converted'):
    policy = store.conn.execute('SELECT policy_version FROM group_generations WHERE id=1').fetchone()[0]
    return bundle_storage.prepare(store.conn, value, fresh, external_key=key, policy_version=policy,
                                  group_generation_id=1, input_revision=None, created_at=10)


@pytest.mark.parametrize('budget', ['bytes', 'variants'])
def test_pending_policy_preflight_is_bounded_without_sql_or_files(recommendations, monkeypatch, budget):
    store = recommendations
    raw = '{"algorithm":"privately-calculated"}'
    monkeypatch.setattr(policy_storage, 'MAX_BATCH_BYTES' if budget == 'bytes' else 'MAX_BATCH_VARIANTS',
                        len(raw.encode()) - 1 if budget == 'bytes' else 1)
    pending = [('new-root', raw, 10), ('new-candidate', raw, 10)]
    before = store.conn.total_changes
    with pytest.raises(ValueError, match='publication budget'):
        bundle_storage.prepare(store.conn, {}, [], external_key='pending-budget', policy_version='new-root',
                               input_revision=0, created_at=10, pending_policies=pending)
    assert store.conn.total_changes == before
    assert not store.conn.execute("SELECT 1 FROM recommendation_policies WHERE version='new-root'").fetchone()


def test_complete_ranked_slots_share_content_and_leave_catalog_fields_with_owners(recommendations):
    store = recommendations
    value = bundle(store)
    before = store.conn.total_changes
    prepared = prepare(store, value, value['all']['fresh'])
    assert store.conn.total_changes == before
    assert not store.conn.execute('SELECT 1 FROM recommendation_generations').fetchone()
    selected = {(slot.surface, slot.mode_key, slot.purpose): slot.pool for slot in prepared.slots}
    assert selected['home', 'all', 'wheelhouse'] == selected['home', 'small', 'wheelhouse']
    assert selected['home', 'all', 'fresh'] == selected['fresh', 'all', 'fresh']
    assert selected['vibe:calm', '', 'comfort'] == selected['vibe:calm', 'rare genre', 'comfort']
    assert [(row.recording_id, row.score, row.intensity_band, row.genre_key)
            for row in selected['radio', 'small', 'membership'].candidates] == [
                (2, .7, 12, 'rare genre'), (1, .9, 40, 'rock')]
    assert [row.recording_id for row in selected['radio', 'small', 'ppr'].candidates] == [1, 2]
    assert len(selected['radio', 'all', 'scores'].candidates) == 2
    encoded = b''.join(output.encoded for output in prepared.outputs)
    for copied in (b'obsolete', b'measurements', b'copied', b'history', b'year_cuts', b'intensity'):
        assert copied not in encoded
    assert b'explanation one' in encoded
    assert b'ppr-3' in encoded
    publish(store, prepared)
    assert store.recommendations.framing('home')['year_high'] == 2004
    assert [row['key'] for row in store.recommendations.page('radio', 'small', 'membership',
                                                          band=12, genres=['rare genre'])] == ['two']
    assert store.recommendations.display('home', 'all', 'wheelhouse', [1]) == {
        1: {'owner': ['recording', 2], 'reason': 'explanation two'}}


def test_missing_exact_owner_preserves_raw_rank_score_and_original_output(recommendations):
    store = recommendations
    value = bundle(store)
    missing = {**value['all']['wheelhouse'][0], 'key': 'missing-old-key', 'score': .8,
               'title': 'historical title', 'video_id': 'historical upload'}
    value['all']['wheelhouse'].insert(1, missing)
    prepared = prepare(store, value)
    slot = next(slot for slot in prepared.slots if (slot.surface, slot.mode_key, slot.purpose) == ('home', 'all', 'wheelhouse'))
    assert slot.pool.candidates[1] == pools.UnresolvedCandidate('missing-old-key', .8)
    publish(store, prepared)
    assert [(row['key'], row['rank']) for row in store.recommendations.page('home', 'all', 'wheelhouse')] == [
        ('one', 0), ('two', 2)]
    archived = store.recommendations.display('home', 'all', 'wheelhouse', [1])[1]
    assert archived['reason'] == 'missing_recording'
    assert archived['unresolved_candidate'] == missing
    assert not store.conn.execute("SELECT 1 FROM recordings WHERE identity_key='missing-old-key'").fetchone()


def test_sparse_explanations_use_actual_rank_ranges_and_read_only_covering_files(recommendations, monkeypatch):
    store = recommendations
    with store.conn:
        store.conn.executemany('INSERT INTO recordings(id,identity_key) VALUES (?,?)', [(i, f'key-{i}') for i in range(3, 30)])
    value = bundle(store)
    prototype = value['all']['wheelhouse'][0]
    items = [{**prototype, 'key': f'key-{i}', 'reason': 'x' * 90} for i in range(3, 30)]
    for item in items[:10]:
        item.pop('reason')
    value = {'all': {'wheelhouse': items}}
    monkeypatch.setattr(bundle_storage, 'EXPLANATION_BYTES', 200)
    prepared = prepare(store, value)
    displays = [output for output in prepared.outputs if output.pool_digest]
    assert displays[0].first_rank == 10
    assert displays[-1].last_rank == 26
    publish(store, prepared)
    reads, original = [], outputs.read
    def read(*args, **kwargs):
        reads.append(args[0])
        return original(*args, **kwargs)
    monkeypatch.setattr(outputs, 'read', read)
    assert store.recommendations.display('home', 'all', 'wheelhouse', [0, 9]) == {}
    assert reads == []
    assert set(store.recommendations.display('home', 'all', 'wheelhouse', [11, 26])) == {11, 26}
    assert len(reads) == 2
    assert len(reads) < len(displays)


@pytest.mark.parametrize('change', [
    lambda value: value['all']['wheelhouse'][0].update(score=float('nan')),
    lambda value: value['all']['wheelhouse'][0].update(pool_policy='missing-policy'),
    lambda value: value['all']['wheelhouse'][0].pop('pool_policy'),
    lambda value: value['all']['wheelhouse'][0].update(queried_future_field=True),
    lambda value: value['_radio']['intensity']['clusters']['small'].update({'04': {'rock': ['one']}}),
    lambda value: value['_radio']['members']['small'].append('two'),
    lambda value: value['_radio']['members']['small'].remove('two'),
    lambda value: value['_meta'].update(year_cuts=[2004, 1987]),
    lambda value: value.update(_unknown={'membership': ['one']}),
])
def test_invalid_legacy_inputs_fail_before_ownership_or_file_publication(recommendations, change):
    store = recommendations
    value = bundle(store)
    change(value)
    before = store.conn.total_changes
    with pytest.raises(ValueError):
        prepare(store, value)
    assert store.conn.total_changes == before
    assert not store.conn.execute('SELECT 1 FROM recommendation_generations').fetchone()
    assert not store.conn.execute('SELECT 1 FROM prepared_outputs').fetchone()


def test_conversion_budget_is_checked_before_accumulating_all_slots(recommendations, monkeypatch):
    store = recommendations
    monkeypatch.setattr(publication, 'MAX_PLAN_BYTES', 10)
    with pytest.raises(ValueError, match='budget'):
        prepare(store, bundle(store))
    assert not store.conn.execute('SELECT 1 FROM recommendation_generations').fetchone()


def test_empty_unprepared_bundle_has_an_explicit_small_last_good_generation(recommendations):
    store = recommendations
    prepared = prepare(store, {}, [])
    publish(store, prepared)
    assert store.recommendations.head('home')
    assert store.recommendations.output('home', 'empty') == {}
    assert store.recommendations.page('fresh', 'all', 'fresh') == []


@pytest.mark.parametrize('failure', ['owner', 'range', 'overlap'])
def test_display_ownership_is_checked_against_the_exact_selected_ranked_pool(recommendations, failure):
    prepared = prepare(recommendations, bundle(recommendations))
    index, output = next((i, output) for i, output in enumerate(prepared.outputs) if output.pool_digest)
    values = list(prepared.outputs)
    if failure == 'owner':
        value = outputs._decode(output.encoded)['output']
        value['rows'][0][1] = ['recording', 999]
        values[index] = replace(output, encoded=outputs.encode(output.surface, output.chunk_key, value))
    elif failure == 'range':
        values[index] = replace(output, first_rank=1)
    else:
        key = 'overlapping-copy'
        value = outputs._decode(output.encoded)['output']
        values.append(replace(output, chunk_key=key, encoded=outputs.encode(output.surface, key, value)))
    with pytest.raises(ValueError, match='Display|display'):
        publication.validate(replace(prepared, outputs=tuple(values)))


def test_atomic_import_preserves_source_until_caller_retires_it_and_reuses_complete_pools(recommendations):
    store = recommendations
    prepared = prepare(store, bundle(store))
    with store.conn:
        store.conn.execute("INSERT INTO rec_proposals VALUES ('mode_bundles','legacy-source',9)")
        generation = publication.import_plan(store.conn, prepared, 11)
        assert store.conn.execute("SELECT payload FROM rec_proposals WHERE surface='mode_bundles'").fetchone()[0] == 'legacy-source'
    before = [tuple(row) for row in store.conn.execute('SELECT * FROM recommendation_candidates ORDER BY pool_id,rank')]
    with store.conn:
        store.conn.execute('BEGIN IMMEDIATE')
        second = publication.import_plan(store.conn, replace(prepared, external_key='next-import'), 12)
    assert second != generation
    assert store.recommendations.head('home')['id'] == second
    assert before == [tuple(row) for row in store.conn.execute('SELECT * FROM recommendation_candidates ORDER BY pool_id,rank')]
    assert not store.conn.execute('SELECT 1 FROM recommendation_builds').fetchone()
    assert not store.conn.execute('PRAGMA foreign_key_check').fetchall()


def test_import_preflight_checks_every_canonical_owner_before_publishing_files(recommendations, monkeypatch):
    store = recommendations
    prepared = prepare(store, bundle(store))
    slot = prepared.slots[0]
    invalid = pools.prepare(slot.pool.pool_key, slot.pool.policy_version, None, slot.pool.input_fingerprint,
                            slot.pool.purpose, [pools.Candidate(999)])
    prepared = replace(prepared, slots=(replace(slot, pool=invalid),), outputs=())
    calls = []
    monkeypatch.setattr(outputs, 'publish', lambda *args: calls.append(args))
    before = store.conn.total_changes
    with pytest.raises(ValueError, match='unresolved canonical'), store.conn:
        store.conn.execute('BEGIN IMMEDIATE')
        publication.import_plan(store.conn, prepared, 11)
    assert store.conn.total_changes == before
    assert calls == []
    assert not store.conn.execute('SELECT 1 FROM recommendation_generations').fetchone()


def test_late_import_failure_rolls_back_owners_and_heads_without_deleting_legacy_source(recommendations, monkeypatch):
    store = recommendations
    prepared = prepare(store, bundle(store))
    first = publish(store, prepared)
    with store.conn:
        store.conn.execute("INSERT INTO rec_proposals VALUES ('mode_bundles','original-evidence',9)")
    counts = {table: store.conn.execute(f'SELECT count(*) FROM {table}').fetchone()[0]
              for table in ('recommendation_generations', 'recommendation_candidates', 'prepared_outputs')}
    original, calls = publication._output, 0
    def interrupted(*args):
        nonlocal calls
        calls += 1
        original(*args)
        if calls == 2:
            raise RuntimeError('controlled late owner failure')
    monkeypatch.setattr(publication, '_output', interrupted)
    with pytest.raises(RuntimeError, match='controlled late'), store.conn:
        store.conn.execute('BEGIN IMMEDIATE')
        publication.import_plan(store.conn, replace(prepared, external_key='interrupted-import'), 12)
    assert store.recommendations.head('home')['id'] == first.generation_id
    for table, count in counts.items():
        assert store.conn.execute(f'SELECT count(*) FROM {table}').fetchone()[0] == count
    assert store.conn.execute("SELECT payload FROM rec_proposals WHERE surface='mode_bundles'").fetchone()[0] == 'original-evidence'
    assert not store.conn.execute('PRAGMA foreign_key_check').fetchall()


def catalog(store):
    with store.conn:
        for recording, key in [(1, 'one'), (2, 'two')]:
            store.conn.execute('UPDATE recordings SET genre=? WHERE id=?', ('current genre', recording))
            store.conn.execute('INSERT INTO youtube_items(video_id,recording_id,title,artist,album,duration_s,mb_year) '
                               'VALUES (?,?,?,?,?,?,?)', (key + '-current', recording, key + ' current title',
                                                        'current artist', 'current album', 180, '2001'))
            entry = store.conn.execute('INSERT INTO catalog_entries(recording_id,video_id) VALUES (?,?) RETURNING id',
                                       (recording, key + '-current')).fetchone()[0]
            store.conn.execute("INSERT INTO library_memberships VALUES (?,'import',1)", (entry,))


def test_serving_hydrates_only_a_rank_page_from_current_canonical_metadata(recommendations, monkeypatch):
    store = recommendations
    catalog(store)
    publish(store, prepare(store, bundle(store)))
    def forbidden(*args, **kwargs):
        pytest.fail('Bounded candidate serving attempted to decode the legacy bundle')
    monkeypatch.setattr(RecProposalsRepo, '_proposal_json', forbidden)
    page = store.recommendations.candidate_page('home', 'all', 'wheelhouse', limit=1)
    assert page['after'] == 0 and not page['exhausted']
    assert len(page['tracks']) == 1
    track = page['tracks'][0]
    assert track['pool_rank'] == 0 and 'rank' not in track
    assert (track['key'], track['title'], track['artist'], track['video_id'], track['genre'], track['year']) == (
        'one', 'one current title', 'current artist', 'one-current', 'current genre', 2001)
    assert (track['score'], track['reason'], track['duration'], track['_radio_fresh']) == (
        .9, 'explanation one', 180, False)
    with store.conn:
        store.conn.execute("UPDATE youtube_items SET title='corrected after publication' WHERE video_id='one-current'")
    assert store.recommendations.candidate_page('home', 'all', 'wheelhouse', limit=1)['tracks'][0]['title'] == 'corrected after publication'


def test_unavailable_upload_does_not_reset_rank_cursor_or_hide_following_candidates(recommendations):
    store = recommendations
    catalog(store)
    publish(store, prepare(store, bundle(store)))
    with store.conn:
        store.conn.execute("UPDATE youtube_items SET available=0 WHERE video_id='one-current'")
    first = store.recommendations.candidate_page('home', 'all', 'wheelhouse', limit=1)
    assert first['tracks'] == [] and first['after'] == 0 and not first['exhausted']
    second = store.recommendations.candidate_page('home', 'all', 'wheelhouse', after=first['after'], limit=1)
    assert [track['key'] for track in second['tracks']] == ['two']
    assert second['after'] == 1 and second['exhausted']


def test_pinned_reader_keeps_one_generation_while_next_publication_switches_heads(recommendations):
    store = recommendations
    catalog(store)
    prepared = prepare(store, bundle(store))
    first = publish(store, prepared)
    with store.read_view() as reader:
        assert reader.recommendations.head('home')['id'] == first.generation_id
        second = publish(store, replace(prepared, external_key='next-serving-generation'))
        page = reader.recommendations.candidate_page('home', 'all', 'wheelhouse', limit=1)
        assert page['generation'] == first.generation_id
        assert page['tracks'][0]['key'] == 'one'
    assert store.recommendations.candidate_page('home', 'all', 'wheelhouse', limit=1)['generation'] == second.generation_id


def test_vibe_reader_reaches_selected_genre_pool_outside_main_head_without_legacy_decode(recommendations, monkeypatch):
    store = recommendations
    catalog(store)
    with store.conn:
        store.conn.execute("UPDATE recordings SET genre=CASE id WHEN 1 THEN 'rock' ELSE 'jazz' END")
    value = bundle(store)
    one, two = value['all']['wheelhouse']
    value['_vibes'] = {'version': home_vibe.VERSION, 'profile_version': musical_character.VERSION,
                       'pools': {'relax': {'comfort': [one]}},
                       'genres': {'relax': {'comfort': {'jazz': [two]}}}}
    publish(store, prepare(store, value))
    def forbidden(*args, **kwargs):
        pytest.fail('Typed vibe serving opened the legacy bundle')
    monkeypatch.setattr(RecProposalsRepo, '_proposal_json', forbidden)
    monkeypatch.setattr(musical_character, 'load', lambda store, keys: {})
    candidates = vibe_pools.candidates(store, 'relax', 'comfort', include_genres=('jazz',))
    assert [(row['key'], row['genre'], row['video_id']) for row in candidates] == [('two', 'jazz', 'two-current')]
    assert [row['key'] for row in vibe_pools.candidates(store, 'relax', 'comfort')] == ['one']
