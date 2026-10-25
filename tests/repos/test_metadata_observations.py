"""Source evidence retains scope and disagreement without selecting catalog facts."""
from dataclasses import replace

import pytest

from yt_playlist.core import catalog
from yt_playlist.providers.base import MetadataObservation


def observation(**changes):
    return replace(MetadataObservation('mood_party', 0.8, 'recording', 'mbid', 'prediction',
                                      unit='classifier_probability'), **changes)


def test_competing_evidence_is_idempotent_per_delivery_and_does_not_change_choices(store):
    tid = store.tracks.upsert_track('video', 'Song', 'Artist', '', 200)
    with store.conn:
        catalog.choose_field(store.conn, tid, 'energy', None)
    repo = store.enrichment_state
    first = observation(field='energy', value=0.8)
    repo.record_metadata_observations(tid, 'source-a', [first], run_id='run-1', now=10)
    repo.record_metadata_observations(tid, 'source-a', [first], run_id='run-1', now=11)
    repo.record_metadata_observations(tid, 'source-b', [replace(first, value=0.2)], run_id='run-1', now=12)
    repo.record_metadata_observations(tid, 'source-a', [first], run_id='run-2', now=13)
    rows = repo.metadata_observations('song|artist')
    assert [(r['provider'], r['value'], r['observed_at']) for r in rows] == [
        ('source-a', 0.8, 10), ('source-b', 0.2, 12), ('source-a', 0.8, 13)]
    assert [r['provenance']['delivery_id'] for r in rows] == ['run-1', 'run-1', 'run-2']
    assert store.tracks.get_track_audio(tid)[1] is None
    assert store.enrichment_reports.metadata_provenance(tid)['fields']['energy'] == {
        'value': None, 'override': True, 'source': 'user'}
    assert store.conn.execute('SELECT energy FROM recordings').fetchone()[0] is None


def test_scopes_tags_and_caller_owned_values_are_retained_without_reinterpretation(store):
    tid = store.tracks.upsert_track('video', 'Song', 'Artist', '', 200)
    tags = [{'name': 'Jazz', 'weight': 0.7}, {'name': 'Soul', 'weight': 0.3}]
    observations = [observation(field='tags', value=tags, scope=scope, entity_id=entity,
                                kind='reported', unit='provider_tag_weight')
                    for scope, entity in [('recording', 'mbid'), ('release', 'release-mbid'),
                                          ('artist', 'artist-mbid'), ('genre', 'Jazz'), ('item', 'video')]]
    store.enrichment_state.record_metadata_observations(tid, 'tags-provider', observations,
                                                        run_id='tags', now=10)
    tags[0]['name'] = 'Changed later'
    rows = store.enrichment_state.metadata_observations('song|artist')
    assert [(r['provenance']['scope'], r['provenance']['entity_id']) for r in rows] == [
        ('recording', 'mbid'), ('release', 'release-mbid'), ('artist', 'artist-mbid'),
        ('genre', 'Jazz'), ('item', 'video')]
    assert all(r['value'][0] == {'name': 'Jazz', 'weight': 0.7} for r in rows)
    assert store.tracks.get_track_enrichment(tid)[0] in (None, '')


def test_pages_are_bounded_restartable_and_exclude_legacy_unscoped_findings(store):
    tid = store.tracks.upsert_track('video', 'Song', 'Artist', '', 200)
    store.enrichment.apply_provider_fields(tid, 'old', {'bpm': 100}, providers=['old'])
    repo = store.enrichment_state
    repo.record_metadata_observations(tid, 'new', [observation(value=v) for v in (0.1, 0.2, 0.3)],
                                     run_id='pages', now=10)
    first = repo.metadata_observations('song|artist', limit=2)
    second = repo.metadata_observations('song|artist', after_id=first[-1]['id'], limit=2)
    assert [r['value'] for r in first + second] == [0.1, 0.2, 0.3]
    assert repo.metadata_observations('song|artist', after_id=second[-1]['id']) == []
    assert repo.metadata_observations('unknown') == []
    for limit in (0, -1, 513, True, 1.5):
        with pytest.raises(ValueError, match='page size'):
            repo.metadata_observations('song|artist', limit=limit)
    for cursor in (-1, True, 0.5):
        with pytest.raises(ValueError, match='cursor'):
            repo.metadata_observations('song|artist', after_id=cursor)


@pytest.mark.parametrize('changes', [
    {'field': ''}, {'entity_id': ''}, {'scope': 'listener'}, {'kind': 'reliability'},
    {'kind': 'derived'}, {'kind': 'inherited'}, {'value': float('nan')},
    {'model_version': {'bad': float('inf')}}, {'match_evidence': []},
])
def test_invalid_batch_does_not_partially_persist(store, changes):
    tid = store.tracks.upsert_track('video', 'Song', 'Artist', '', 200)
    with pytest.raises(ValueError):
        store.enrichment_state.record_metadata_observations(
            tid, 'provider', [observation(), observation(**changes)], run_id='bad', now=10)
    assert store.enrichment_state.metadata_observations('song|artist') == []


def test_inherited_and_curated_evidence_remain_distinct():
    inherited = observation(kind='inherited', derivation_version='artist-prior-v1',
                            dependencies=('artist:mbid/tags',)).payload()
    curated = observation(kind='curated', scope='genre', entity_id='Jazz').payload()
    assert inherited['derivation_version'] == 'artist-prior-v1'
    assert inherited['dependencies'] == ['artist:mbid/tags']
    assert curated['derivation_version'] is None
    assert curated['kind'] != inherited['kind']


def test_in_flight_answer_cannot_follow_a_recording_correction(store):
    old = store.tracks.upsert_track('old', 'Old song', 'Artist', '', 200)
    target = store.tracks.upsert_track('target', 'Correct song', 'Artist', '', 200)
    old_recording = store.tracks.catalog_subject(old)['recording_id']
    target_recording = store.tracks.catalog_subject(target)['recording_id']
    store.enrichment_state.record_metadata_observations(old, 'provider', [observation()],
        run_id='before', now=10, expected_recording_id=old_recording)
    store.tracks.associate_youtube_item('old', target_recording, source='review',
        evidence={'reason': 'recording correction'}, now=20, same_recording=False)
    with pytest.raises(ValueError, match='subject changed'):
        store.enrichment_state.record_metadata_observations(old, 'provider', [observation(value=0.3)],
            run_id='in-flight', now=30, expected_recording_id=old_recording)
    assert store.enrichment_state.metadata_observations('correct song|artist') == []
    rows = store.conn.execute('SELECT recording_id,value_json FROM metadata_findings '
                              'WHERE provenance_json IS NOT NULL').fetchall()
    assert [tuple(row) for row in rows] == [(old_recording, '0.8')]
