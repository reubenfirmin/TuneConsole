"""Vibe preparation learns only from measurements, with sparse, durable cohorts."""
import json
from dataclasses import replace
from uuid import uuid4

import pytest

from tests.test_audio_vibes import sample
from yt_playlist.core import catalog
from yt_playlist.core.audio_vibe_contract import TAXONOMY_VERSION
from yt_playlist.core.store import Store
from yt_playlist.providers.base import MetadataObservation
from yt_playlist.rec import home_vibe, musical_character, musical_preparation, vibe_prediction as policy
from yt_playlist.rec import vibe_preparation
from yt_playlist.rec.progressive import Budget, Preparation


def track(store, name, genre='Psybient', artist='Shared artist'):
    tid = store.tracks.upsert_track(name, name, artist, '', 240)
    with store.conn:
        catalog.choose_field(store.conn, tid, 'genre', genre)
    return tid, store.tracks.identity_key_for_video(name)


def capture(store, video, *, calm=True, count=3):
    run = str(uuid4())
    now = 100 + store.conn.execute('SELECT COUNT(*) FROM sample_attributes').fetchone()[0]
    samples = []
    for point in ('thirty', 'middle', 'two_thirds')[:count]:
        row = sample(run_id=run, point=point, relaxed=.95 if calm else .05, aggressive=.05 if calm else .95)
        row['video_id'] = video
        row['traits']['arousal']['score'] = 1.4 if calm else 8.6
        store.audio_vibes.save_sample(row, now)
        samples.append(row)
    return samples


def drain(store, batch=8):
    worker = Preparation(store, budget=replace(Budget(), batch=batch))
    for _ in range(500):
        if (not store.prepared_content.has_pending() and not musical_preparation.due(store)
                and not vibe_preparation.due(store)):
            return worker
        assert worker.capture(1000)
    pytest.fail('Prediction work did not converge')


def fits(store, key):
    return store.musical_profiles.by_keys([key])[key]['vibes']['fits']


def test_explicit_intensity_stays_authoritative_after_audio_and_a_later_correction(store, monkeypatch):
    from fastapi.testclient import TestClient

    from yt_playlist.rec import audio_vibe_model
    from yt_playlist.web.app import create_app

    monkeypatch.setenv('YT_PLAYLIST_AUDIO_VIBES', '1')
    monkeypatch.setenv(audio_vibe_model.ENVIRONMENT_KEY, '')
    entry, key = track(store, 'override-song')
    with store.conn:
        catalog.choose_field(store.conn, entry, 'energy', .1, now=10)
    drain(store)
    item = {'musical': musical_character.load(store, [key])[key]}
    assert musical_character.intensity_evidence(item) == (.1, 1)

    run = str(uuid4())
    app = create_app(store, lambda: {}, now_fn=lambda: 1000, background_work=False)
    with TestClient(app, base_url='http://127.0.0.1') as client:
        for point in ('thirty', 'middle', 'two_thirds'):
            value = sample(run_id=run, point=point, relaxed=.05, aggressive=.95)
            value['video_id'] = 'override-song'
            response = client.post('/audio-vibes/samples', json=value)
            assert response.status_code == 200, response.text
    frozen = store.audio_vibes.samples(run)
    assert len(frozen) == 3

    for correction in (.1, .2):
        if correction == .2:
            with store.conn:
                catalog.choose_field(store.conn, entry, 'energy', correction, now=1001)
        drain(store)
        item = {'musical': musical_character.load(store, [key])[key]}
        assert item['musical']['dimensions']['intensity']['override'] is True
        assert {fit['source'] for fit in item['musical']['vibes']['fits'].values()} == {'audio_measurement'}
        assert musical_character.intensity_evidence(item) == (correction, 1)
        assert musical_character.intensity(item) == correction
        context = musical_character.provenance(item)
        assert context['intensity'] == correction and context['intensity_support'] == 1
        assert context['intensity_source'] is None and context['intensity_vibe'] is None
    assert store.audio_vibes.samples(run) == frozen


def test_consistent_distinct_songs_strengthen_sparse_prediction_and_disagreement_weakens_it():
    group = policy.groups('canonical artist', 'psybient')[0]
    calm = {'fits': {'relax': {'score': .95, 'support': 1}}}
    loud = {'fits': {'relax': {'score': .05, 'support': 1}}}
    one = policy.adjust({}, calm, 1)
    two = policy.adjust(one, calm, 1)
    conflicting = policy.adjust(one, loud, 1)
    first, consistent, varied = [policy.prediction(group, stats, 'relax') for stats in (one, two, conflicting)]
    assert 0 < first['support'] < consistent['support'] < 1
    assert varied['support'] < first['support']
    assert consistent['score'] == pytest.approx(.95)
    assert policy.adjust(one, calm, -1) == {}


def test_audio_wins_then_exact_artist_subgenre_then_artist_or_subgenre_then_fallback(store):
    _tid, measured = track(store, 'measured')
    _tid, same = track(store, 'same')
    _tid, other_style = track(store, 'other-style', 'Detroit Techno')
    _tid, other_artist = track(store, 'other-artist', artist='Another artist')
    _tid, empty = track(store, 'empty', 'Electronic', 'Unrelated')
    capture(store, 'measured')
    capture(store, 'other-style', calm=False)
    drain(store, batch=2)
    own, predicted, techno, genre, fallback = [fits(store, key) for key in
                                            (measured, same, other_style, other_artist, empty)]
    assert own['relax']['source'] == techno['relax']['source'] == 'audio_measurement'
    assert own['relax']['score'] > techno['relax']['score']
    assert predicted['relax']['source'] == 'artist_genre'
    assert predicted['relax']['recordings'] == 1
    assert genre['relax']['source'] == 'genre'
    assert fallback['relax']['source'] == 'heuristic'
    assert predicted['relax']['score'] > fallback['relax']['score']
    compact = musical_character.load(store, [measured, other_style])
    assert home_vibe.affinity({'key': measured}, 'relax', compact) == own['relax']['score']
    assert musical_character.intensity({'musical': compact[measured]}) < .2
    assert musical_character.intensity({'musical': compact[other_style]}) > .8


def test_replays_are_one_recording_partial_runs_do_not_replace_complete_and_annotations_never_seed(store):
    _tid, one = track(store, 'one')
    _tid, two = track(store, 'two')
    samples = capture(store, 'one')
    drain(store)
    before = fits(store, two)
    capture(store, 'one')
    capture(store, 'one', calm=False, count=2)
    store.audio_vibes.save_feedback(samples[0]['run_id'], [s['id'] for s in samples],
                                    ['release'], TAXONOMY_VERSION, None, 200)
    drain(store)
    assert fits(store, two) == before
    assert fits(store, two)['relax']['recordings'] == 1
    assert fits(store, one)['relax']['score'] > .8
    statements = []
    store.conn.set_trace_callback(statements.append)
    with store.conn:
        store.conn.execute('INSERT INTO rec_content_pending(identity_key) VALUES (?)', (two,))
    drain(store)
    store.conn.set_trace_callback(None)
    assert not any('audio_session_labels' in sql or 'audio_vibe_feedback' in sql for sql in statements)


@pytest.mark.parametrize('genre', ['Electronic', 'Techno', 'House', 'Ambient', 'Rock'])
def test_broad_genres_cannot_seed_genre_predictions(store, genre):
    track(store, 'measured', genre, 'One artist')
    _tid, target = track(store, 'target', genre, 'Another artist')
    capture(store, 'measured')
    drain(store)
    assert fits(store, target)['relax']['source'] == 'heuristic'
    assert not store.conn.execute('SELECT 1 FROM recording_vibes WHERE subgenre_key IS NOT NULL').fetchone()


def test_provider_classifiers_seed_predictions_without_captured_audio_or_prepared_state(store):
    tid, own = track(store, 'provider')
    _tid, target = track(store, 'target')
    store.enrichment_state.record_metadata_observations(tid, 'acousticbrainz', [
        MetadataObservation('mood_relaxed', .98, 'recording', 'native-recording', 'prediction',
                            match_evidence={'method': 'title_artist_match'}),
        MetadataObservation('mood_aggressive', .01, 'recording', 'native-recording', 'prediction',
                            match_evidence={'method': 'title_artist_match'})], run_id='provider', now=100)
    drain(store, batch=1)
    assert fits(store, own)['relax']['source'] == 'provider_measurement'
    assert fits(store, target)['relax']['source'] == 'artist_genre'
    assert fits(store, target)['relax']['score'] > .5
    assert not store.audio_vibes.samples('provider')


def test_changed_measurement_and_subgenre_retract_old_cohort_and_notify_without_content_refit(store):
    tid, own = track(store, 'one')
    _tid, target = track(store, 'target')
    capture(store, 'one')
    drain(store)
    counters = store.prepared_content.counters()
    before = fits(store, target)['relax']['score']
    capture(store, 'one', calm=False)
    drain(store, batch=1)
    assert fits(store, target)['relax']['score'] < before
    assert store.prepared_content.counters() == counters
    assert store.rec_refresh.revision() is not None
    with store.conn:
        catalog.choose_field(store.conn, tid, 'genre', 'Detroit Techno')
    drain(store, batch=1)
    assert fits(store, target)['relax']['source'] == 'artist'
    assert fits(store, own)['relax']['source'] == 'audio_measurement'


def test_prediction_queue_survives_restart_and_failure_rolls_back_all_derived_state(tmp_path, monkeypatch):
    path = tmp_path / 'predictions.db'
    store = Store(path)
    store.init_schema()
    track(store, 'one')
    _tid, target = track(store, 'two')
    capture(store, 'one')
    from yt_playlist.repos.vibe_predictions import VibePredictionsRepo
    original = VibePredictionsRepo.publish
    def fail(repo, *args):
        original(repo, *args)
        raise RuntimeError('interrupted prediction publication')
    with monkeypatch.context() as patch:
        patch.setattr(VibePredictionsRepo, 'publish', fail)
        with pytest.raises(RuntimeError, match='interrupted prediction'):
            Preparation(store).capture(1000)
    assert not store.conn.execute('SELECT 1 FROM recording_vibes').fetchone()
    assert not store.conn.execute('SELECT 1 FROM measurements').fetchone()
    assert not store.conn.execute('SELECT 1 FROM predictions').fetchone()
    assert store.prepared_content.has_pending()
    store.conn.close()
    store = Store(path)
    store.init_schema()
    try:
        drain(store, batch=1)
        assert fits(store, target)['relax']['source'] == 'artist_genre'
        assert not store.vibe_predictions.pending()
    finally:
        store.conn.close()


def test_model_version_change_recomputes_all_evidence_in_pages(store, monkeypatch):
    track(store, 'one')
    _tid, target = track(store, 'two')
    capture(store, 'one')
    drain(store)
    old = fits(store, target)
    monkeypatch.setattr(vibe_preparation.audio_vibe_model, 'identity', lambda: 'changed-model')
    assert vibe_preparation.due(store)
    assert vibe_preparation.step(store, batch=1)
    assert store.vibe_predictions.pending()
    drain(store, batch=1)
    assert fits(store, target) == old
    assert store.conn.execute('SELECT COUNT(DISTINCT recording_id) FROM recording_vibes WHERE inputs_version LIKE ?',
                              ('%changed-model',)).fetchone()[0] == 2


def test_artist_tags_never_become_recording_measurements_or_propagation_seeds(store):
    tid, own = track(store, 'one')
    _tid, target = track(store, 'two')
    store.enrichment_state.record_metadata_observations(tid, 'lastfm', [
        MetadataObservation('tags', [{'name': 'Ambient'}], 'artist', 'shared', 'reported',
                            match_evidence={'method': 'title_artist_match'})], run_id='artist-tags', now=1)
    drain(store)
    assert fits(store, own)['relax']['source'] == fits(store, target)['relax']['source'] == 'heuristic'
    assert not store.conn.execute('SELECT 1 FROM measurements').fetchone()


def test_removed_audio_retracts_predictions_and_keeps_recording_fallback(store):
    track(store, 'one')
    _tid, target = track(store, 'two')
    capture(store, 'one')
    drain(store)
    assert fits(store, target)['relax']['source'] == 'artist_genre'
    with store.conn:
        store.conn.execute('DELETE FROM sample_attributes')
    drain(store)
    assert fits(store, target)['relax']['source'] == 'heuristic'
    assert not store.conn.execute('SELECT 1 FROM recording_vibes WHERE measurement_id IS NOT NULL').fetchone()
    assert store.conn.execute('SELECT 1 FROM measurements').fetchone()  # history survives retraction


def test_many_interrupted_replays_preserve_last_complete_raw_measurement(store):
    _tid, own = track(store, 'one')
    capture(store, 'one')
    drain(store)
    before = fits(store, own)
    for _ in range(49):
        capture(store, 'one', calm=False, count=2)
    drain(store)
    assert fits(store, own) == before


def test_radio_chill_does_not_mistake_slow_forceful_music_for_low_intensity():
    item = {'musical': {'vibes': {'fits': {
        'energize': {'score': .1, 'support': 1, 'source': 'audio_measurement'},
        'release': {'score': .95, 'support': 1, 'source': 'audio_measurement'}}}}}
    assert musical_character.intensity(item) == .95
    assert musical_character.provenance(item)['intensity_vibe'] == 'release'


def test_played_catalog_recording_can_teach_library_before_it_is_saved(store):
    tid = store.tracks.upsert_track('outside', 'Outside', 'Shared artist', '', 240, library=False)
    with store.conn:
        catalog.choose_field(store.conn, tid, 'genre', 'Psybient')
    _tid, target = track(store, 'target')
    capture(store, 'outside')
    drain(store)
    assert fits(store, target)['relax']['source'] == 'artist_genre'
    assert not store.musical_profiles.by_keys(['outside|shared artist'])


def test_provider_backlog_cannot_starve_vibe_bootstrap_or_dependency_fanout(store):
    for index in range(10):
        tid, _key = track(store, f'song{index}')
        store.enrichment_state.record_metadata_observations(tid, 'acousticbrainz', [
            MetadataObservation('mood_relaxed', .9, 'recording', str(index), 'prediction',
                                match_evidence={'method': 'title_artist_match'})], run_id=str(index), now=1)
    worker = Preparation(store, budget=replace(Budget(), batch=2))
    assert worker.capture(1000)  # provider source
    assert worker.capture(1000)  # recordings
    before = store.conn.execute("SELECT cursor FROM rec_vibe_pending WHERE group_key='*'").fetchone()[0]
    assert worker.capture(1000)  # vibe job must get the next source admission
    after = store.conn.execute("SELECT cursor FROM rec_vibe_pending WHERE group_key='*'").fetchone()[0]
    assert after > before
    assert store.musical_profiles.pending()
    drain(store, batch=2)
