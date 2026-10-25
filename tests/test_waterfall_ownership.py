"""Reject whole stale results, including fields, retries, reuse and artwork."""
from types import SimpleNamespace

import pytest

from yt_playlist.providers import coverart
from yt_playlist.providers.base import EnrichmentResult, MetadataObservation
from yt_playlist.providers.waterfall import run_waterfall


def provider(probe, **kwargs):
    return SimpleNamespace(name='acousticbrainz', probe=probe, reset=lambda: None,
                           available=lambda store: True, tripped=kwargs.get('tripped', lambda: False))


def run(store, tid, source, now=1000, **kwargs):
    return run_waterfall(store, [store.tracks.track_for_waterfall(tid)],
        [{'name': 'acousticbrainz', 'enabled': True}], kwargs.pop('on_progress', lambda event: None),
        registry={'acousticbrainz': source}, now_fn=lambda: now, **kwargs)


def move(store, target):
    store.tracks.associate_youtube_item('original', store.tracks.catalog_subject(target)['recording_id'],
        source='review', evidence={'reason': 'different recording'}, now=1001, same_recording=False)


@pytest.mark.parametrize('observations', [False, True])
@pytest.mark.parametrize('discovery', [False, True])
def test_in_flight_answer_cannot_enter_corrected_recording_or_completed_reuse(store, observations, discovery):
    if discovery:
        store.discovery.upsert_discovered_track('wrong|artist', 'original', 'Wrong', 'Artist', None,
                                               None, None, None, 'radio:test', 10)
        original = store.tracks.track_ids_for_videos(['original'])['original']
    else:
        original = store.tracks.upsert_track('original', 'Wrong', 'Artist', '', 200)
    target = store.tracks.upsert_track('target', 'Correct', 'Artist', '', 200)
    store.enrichment_queue.mark_enriched([target], 100)
    recording = store.tracks.catalog_subject(target)['recording_id']
    def probe(track, _store):
        move(store, target)
        evidence = (MetadataObservation('energy', .9, 'recording', 'old-mbid', 'prediction'),) if observations else ()
        return EnrichmentResult('acousticbrainz', {'energy': .9}, evidence)
    events = []
    assert run(store, original, provider(probe), on_progress=events.append) == []
    assert store.enrichment_state.enrichment_provider_state(recording) == {}
    assert store.enrichment_state.metadata_observations('correct|artist') == []
    assert store.tracks.get_track_audio(target)[1] is None
    assert not store.conn.execute('SELECT 1 FROM provider_attempts WHERE recording_id=?', (recording,)).fetchone()
    assert store.conn.execute('SELECT last_enriched_at FROM recordings WHERE id=?', (recording,)).fetchone()[0] is None
    assert any(event.get('fully_attempted') is False for event in events)
    healthy = provider(lambda track, store: EnrichmentResult('acousticbrainz', {'bpm': 120}))
    assert run(store, original, healthy, now=10000)
    assert run(store, original, healthy, now=20000, reuse_completed=True)
    assert store.tracks.get_track_audio(target)[:2] == (120, None)


def test_exception_after_reassociation_does_not_create_wrong_subject_retry_state(store):
    original = store.tracks.upsert_track('original', 'Wrong', 'Artist', '', 200)
    target = store.tracks.upsert_track('target', 'Correct', 'Artist', '', 200)
    def probe(track, _store):
        move(store, target)
        raise TimeoutError('old lookup failed')
    assert run(store, original, provider(probe)) == []
    assert not store.conn.execute('SELECT 1 FROM provider_attempts').fetchone()
    assert not store.enrichment_state.enrichment_cooldowns()


def test_stale_admitted_job_is_requeued_without_retargeting_its_completion_id(store):
    original = store.tracks.upsert_track('original', 'Wrong', 'Artist', '', 200)
    target = store.tracks.upsert_track('target', 'Correct', 'Artist', '', 200)
    admitted = store.tracks.track_for_waterfall(original)
    admitted['job_id'] = admitted['recording_id']
    move(store, target)
    def unexpected(*args):
        pytest.fail('An obsolete recording job must be admitted again')
    source = provider(unexpected)
    assert run_waterfall(store, [admitted], [{'name': 'acousticbrainz', 'enabled': True}], lambda event: None,
                         registry={'acousticbrainz': source}) == []
    assert not store.conn.execute('SELECT 1 FROM provider_attempts').fetchone()


def test_reassociation_between_result_publication_and_attempt_keeps_evidence_on_original(store):
    original = store.tracks.upsert_track('original', 'Wrong', 'Artist', '', 200)
    old = store.tracks.catalog_subject(original)['recording_id']
    target = store.tracks.upsert_track('target', 'Correct', 'Artist', '', 200)
    armed = []
    def probe(track, _store):
        armed.append(True)
        return EnrichmentResult('acousticbrainz', {'energy': .9}, (
            MetadataObservation('energy', .9, 'recording', 'old-mbid', 'prediction'),))
    def tripped():
        if armed:
            armed.clear()
            move(store, target)
        return False
    assert run(store, original, provider(probe, tripped=tripped)) == []
    assert store.conn.execute('SELECT recording_id FROM metadata_findings WHERE provenance_json IS NOT NULL').fetchone()[0] == old
    assert store.tracks.get_track_audio(target)[1] is None
    assert not store.conn.execute('SELECT 1 FROM provider_attempts').fetchone()


def test_artwork_lookup_cannot_publish_after_recording_correction(store, monkeypatch):
    original = store.tracks.upsert_track('original', 'Wrong', 'Artist', '', 200)
    target = store.tracks.upsert_track('target', 'Correct', 'Artist', '', 200)
    def lookup(ids):
        move(store, target)
        return coverart.CoverResult('found', 'https://example.test/old-art.jpg')
    monkeypatch.setattr(coverart, 'lookup', lookup)
    source = provider(lambda track, store: EnrichmentResult('acousticbrainz', {'mb_release_ids': ['old-release']}))
    assert run(store, original, source) == []
    current = store.tracks.track_for_waterfall(original)
    assert current['thumbnail'] != 'https://example.test/old-art.jpg'
