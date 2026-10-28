"""Flagged listening experiment: combined signals, durable labels and metadata-only APIs."""
import json
from pathlib import Path
import shutil
import subprocess
from uuid import uuid4

from fastapi.testclient import TestClient
import pytest

from yt_playlist.core import features
from yt_playlist.rec import audio_vibe_preview, home_vibe
from yt_playlist.web.app import create_app


def attributes():
    return {'version': 'audio-attributes-1', 'sample_rate': 44100, 'channels': 2, 'seconds': 10,
        'runtime': 'essentia-csp-audio-2', 'meyda_version': '5.6.3', 'scope': 'excerpt', 'values': {
            'integrated_lufs': -14.2, 'momentary_lufs_mean': -14.1, 'short_term_lufs_mean': -14.3,
            'excerpt_loudness_range_lu': 1.2, 'tempo_bpm': 124.0, 'tempo_confidence': 2.1,
            'beat_interval_cv': .03, 'beat_count': 20, 'rms': .2, 'peak': .9,
            'rms_dbfs': -13.98, 'peak_dbfs': -.92, 'crest_factor': 4.5,
            'peak_normalized_rms_dbfs': -13.06, 'clipped_fraction': 0.0,
            'zero_crossings_per_second': 1000.0, 'spectral_centroid_hz': 2500.0,
            'spectral_flatness': .1, 'spectral_rolloff_hz': 6000.0, 'spectral_spread_hz': 1000.0,
            'spectral_slope': -.01, 'spectral_skewness': 2.0, 'spectral_kurtosis': 3.0,
            'spectral_crest': 10.0, 'perceptual_spread': .5, 'perceptual_sharpness': 1.0,
            'bark_loudness': 20.0}}


def sample(*, run_id=None, point='thirty', relaxed=.1, aggressive=.9, **tags):
    scores = {f'tag{i}': 0.0 for i in range(50 - len(tags))} | tags
    traits = {key: {'score': value, 'per_patch': [value] * 3} for key, value in {
        'relaxed': relaxed, 'aggressive': aggressive, 'happy': .3, 'sad': .1,
        'arousal': 6.0, 'valence': 5.5}.items()}
    target = {'thirty': 30.0, 'middle': 120.0, 'two_thirds': 160.0}[point]
    return {'id': str(uuid4()), 'run_id': run_id or str(uuid4()), 'point': point,
        'target_seconds': target, 'position_seconds': target + .1, 'source_position_seconds': target,
        'duration_seconds': 240.0, 'video_id': 'observed_song', 'title': 'Observed title',
        'artist': 'Observed artist', 'metadata_verified': False, 'analysis_location': 'youtube_tab',
        'audio_transferred_to_app': False, 'source_identity_verified': False,
        'recommendation_adoption': False, 'raw_audio_retained': False, 'analysed_seconds': 8.992,
        'tags': scores, 'traits': traits, 'model': 'msd-musicnn-1', 'model_version': '1',
        'mood_versions': {key: '1' for key in ('relaxed', 'aggressive', 'happy', 'sad', 'deam')},
        'artifact_hashes': {'musicnn/model.json': 'a' * 64}, 'audio_attributes': attributes()}


def test_local_switch_defaults_off_and_explicit_environment_wins(tmp_path, monkeypatch):
    monkeypatch.delenv('YT_PLAYLIST_AUDIO_VIBES')
    monkeypatch.setattr(features, 'LOCAL_ENV', tmp_path / '.env.local')
    assert not features.audio_vibes_enabled()
    features.LOCAL_ENV.write_text('# Personal preview\nYT_PLAYLIST_AUDIO_VIBES=1\n')
    assert features.audio_vibes_enabled()
    monkeypatch.setenv('YT_PLAYLIST_AUDIO_VIBES', '0')
    assert not features.audio_vibes_enabled()
    features.LOCAL_ENV.write_text('YT_PLAYLIST_AUDIO_VIBES=0\n')
    monkeypatch.setenv('YT_PLAYLIST_AUDIO_VIBES', '1')
    assert features.audio_vibes_enabled()


def test_flag_gates_rating_ui_and_feedback_but_not_collection(store, monkeypatch):
    with TestClient(create_app(store, lambda: {}), base_url='http://127.0.0.1') as client:
        html = client.get('/').text
        assert 'audio-vibe-collection.js' in html
        assert 'data-audio-vibe-collection' in html
        assert 'audio-vibe-banner.css' not in html
        assert 'data-audio-vibe-banner' not in html
        assert 'data-vibe-choice=' not in html
        value = sample()
        statements = []
        store.conn.set_trace_callback(statements.append)
        response = client.post('/audio-vibes/samples', json=value)
        state = response.json()
        assert response.status_code == 200, state
        assert state['samples'] == [{'id': value['id'], 'point': value['point']}]
        assert 'feedback' not in state and 'choices' not in state and 'prediction' not in state
        assert client.get('/audio-vibes/runs/' + value['run_id']).json() == state
        assert client.post('/audio-vibes/runs', json={key: value[key] for key in
                           ('run_id', 'video_id', 'title', 'artist')}).status_code == 200
        assert client.post('/audio-vibes/feedback', json={}).status_code == 404
        assert client.post('/audio-vibes/failures', json={'run_id': value['run_id'],
            'point': value['point'], 'attempt': 1, 'phase': 'saving', 'message': 'Retry'}).status_code == 200
        assert client.get('/audio-vibes/runs/' + str(uuid4())).status_code == 200
        store.conn.set_trace_callback(None)
        assert not any('audio_session_labels' in sql or 'audio_vibe_feedback' in sql for sql in statements)
        monkeypatch.setenv('YT_PLAYLIST_AUDIO_VIBES', '1')
        for path in ('/', '/playlists', '/charts'):
            html = client.get(path).text
            assert 'audio-vibe-collection.js' in html and 'data-audio-vibe-banner' in html
            for key, preset in home_vibe.PRESETS.items():
                assert f'data-vibe-choice="{key}"' in html and audio_vibe_preview.ATTRIBUTE_LABELS[key] in html
            assert 'data-vibe-choice=""' not in html
        assert 'audio-vibe-collection.js' not in client.get('/audio-preview').text
        # A live dev process may still own the previous route globals while
        # Jinja reloads a newly edited template. Keep that page usable until restart.
        del client.app.state.ctx.templates.env.globals['audio_vibes_enabled']
        assert client.get('/').status_code == 200


def test_samples_and_feedback_survive_reload_and_freeze_the_labeled_excerpts(store, monkeypatch):
    monkeypatch.setenv('YT_PLAYLIST_AUDIO_VIBES', '1')
    store.settings.set_setting(home_vibe.SETTING, 'release')
    first = sample(electronic=.9, dance=.8)
    with TestClient(create_app(store, lambda: {}), base_url='http://127.0.0.1') as client:
        response = client.post('/audio-vibes/samples', json=first)
        assert response.status_code == 200, response.text
        state = response.json()
        assert {key: choice['label'] for key, choice in state['choices'].items()} == audio_vibe_preview.LABELS
        assert {key: choice['attribute_label'] for key, choice in state['choices'].items()} == {
            'relax': 'Calm, soothing, unhurried', 'concentrate': 'Steady, absorbing, unobtrusive',
            'cruise': 'Easygoing, moderately lively, comfortable company',
            'energize': 'Driving, animated, propulsive', 'uplift': 'Positive, buoyant, encouraging',
            'release': 'Forceful, hard-hitting, cathartic'}
        assert state['choices']['release']['label'] == 'Let off steam'
        assert state['choices']['concentrate']['description'] == (
            'Supports concentration without repeatedly grabbing my attention.')
        assert state['samples'][0]['source_identity_verified'] is False
        assert state['samples'][0]['metadata_verified'] is False
        assert state['prediction'] is None
        assert state['samples'][0]['audio_attributes'] == attributes()
        assert len(client.post('/audio-vibes/samples', json=first).json()['samples']) == 1
        conflicting = {**first, 'id': str(uuid4())}
        assert client.post('/audio-vibes/samples', json=conflicting).status_code == 409
        assert client.post('/audio-vibes/samples', json={**first, 'run_id': str(uuid4())}).status_code == 409
        label = {'run_id': first['run_id'], 'sample_ids': [first['id']], 'vibes': ['cruise', 'release'],
                 'taxonomy_version': audio_vibe_preview.TAXONOMY_VERSION}
        assert client.post('/audio-vibes/feedback', json=label).status_code == 200
        second = sample(run_id=first['run_id'], point='middle', relaxed=.95, aggressive=.05, acoustic=.8)
        updated = client.post('/audio-vibes/samples', json=second).json()
        assert updated['prediction'] is None
        assert updated['feedback']['sample_ids'] == [first['id']]
        assert updated['feedback']['prediction'] is None
        # Clicking an active choice again must also persist removal of the last
        # label. An empty selection is not an automatic negative judgment.
        cleared = client.post('/audio-vibes/feedback', json={**label, 'vibes': []})
        assert cleared.status_code == 200
        assert cleared.json()['feedback']['vibes'] == []
        for mutation in ({'vibes': ['']}, {'vibes': ['focus']}, {'vibes': ['bright']},
                         {'vibes': ['unwind']}, {'vibes': ['gentle']}, {'vibes': ['boost']},
                         {'vibes': ['intense']}, {'vibes': ['move']}, {'vibes': ['relax', 'relax']},
                         {'taxonomy_version': 'old'}, {'vibe': 'move'}):
            assert client.post('/audio-vibes/feedback', json={**label, **mutation}).status_code == 422
        third = sample(run_id=first['run_id'], point='two_thirds')
        complete = client.post('/audio-vibes/samples', json=third).json()
        assert complete['prediction']['sample_count'] == 3
        assert complete['prediction']['aggregation'] == 'ordered_arc'
        assert [row['sample_id'] for row in complete['prediction']['arc']['excerpts']] == [
            first['id'], second['id'], third['id']]
        assert complete['prediction']['varying'] is True
        assert complete['prediction']['recommendation_adoption'] is False
        assert client.post('/audio-vibes/feedback', json={**label,
            'sample_ids': [second['id'], first['id'], third['id']], 'vibes': ['relax', 'cruise']}).status_code == 200
        foreign = sample(point='two_thirds')
        client.post('/audio-vibes/samples', json=foreign)
        assert client.post('/audio-vibes/feedback', json={**label, 'sample_ids': [foreign['id']]}).status_code == 409
        assert client.post('/audio-vibes/feedback', json={**label, 'sample_ids': [first['id']] * 2}).status_code == 409
        assert client.post('/audio-vibes/samples', json={**second, 'id': str(uuid4()),
            'point': 'two_thirds', 'target_seconds': 160.0, 'position_seconds': 160.1,
            'video_id': 'another_song'}).status_code == 409
    with TestClient(create_app(store, lambda: {}), base_url='http://127.0.0.1') as client:
        state = client.get('/audio-vibes/runs/' + first['run_id']).json()
        assert len(state['samples']) == 3
        assert state['feedback']['vibes'] == ['relax', 'cruise']
        assert state['feedback']['taxonomy_version'] == audio_vibe_preview.TAXONOMY_VERSION
        assert state['feedback']['prediction']['sample_count'] == 3
        assert state['feedback']['prediction']['arc']['order'] == 'position_seconds'
    assert store.settings.get_setting(home_vibe.SETTING) == 'release'
    assert store.conn.execute('SELECT COUNT(*) FROM rec_musical_profiles').fetchone()[0] == 0


def test_failures_are_bounded_metadata_and_reach_app_logs(store, monkeypatch, caplog):
    monkeypatch.setenv('YT_PLAYLIST_AUDIO_VIBES', '1')
    value = {'run_id': str(uuid4()), 'point': 'thirty', 'attempt': 2,
             'phase': 'analysing', 'message': 'Tab-local analysis timed out.'}
    with TestClient(create_app(store, lambda: {}), base_url='http://127.0.0.1') as client:
        assert client.post('/audio-vibes/failures', json=value).json() == {'recorded': True}
        assert value['run_id'] in caplog.text and value['message'] in caplog.text
        assert client.post('/audio-vibes/failures', json={**value, 'attempt': 10}).status_code == 200
        for mutation in ({'pcm': [0.1]}, {'attempt': 10001}, {'message': 'x' * 513}, {'phase': 'anything'}):
            assert client.post('/audio-vibes/failures', json={**value, **mutation}).status_code == 422
    assert store.conn.execute('SELECT COUNT(*) FROM sample_attributes').fetchone()[0] == 0


@pytest.mark.parametrize('mutation', [
    {'pcm': 'raw audio must not be accepted'}, {'source_identity_verified': True},
    {'recommendation_adoption': True}, {'audio_transferred_to_app': True},
    {'analysis_location': 'app_tab'}, {'position_seconds': 50.0},
    {'target_seconds': 15.0, 'position_seconds': 15.0}, {'point': 'elsewhere'},
    {'duration_seconds': 35.0}, {'tags': {'metal': 1.0}}, {'traits': {}},
    {'mood_versions': {}}, {'artifact_hashes': {'model': 'unverified'}},
])
def test_collection_rejects_audio_unbounded_or_inconsistent_metadata(store, monkeypatch, mutation):
    monkeypatch.setenv('YT_PLAYLIST_AUDIO_VIBES', '1')
    with TestClient(create_app(store, lambda: {}), base_url='http://127.0.0.1') as client:
        assert client.post('/audio-vibes/samples', json={**sample(), **mutation}).status_code == 422
        assert store.conn.execute('SELECT COUNT(*) FROM sample_attributes').fetchone()[0] == 0


def test_collection_rejects_nonfinite_inconsistent_patches_and_large_requests(store, monkeypatch):
    monkeypatch.setenv('YT_PLAYLIST_AUDIO_VIBES', '1')
    value = sample()
    with TestClient(create_app(store, lambda: {}), base_url='http://127.0.0.1') as client:
        value['traits']['relaxed']['per_patch'] = [.1, .1, .9]
        assert client.post('/audio-vibes/samples', json=value).status_code == 422
        value = sample()
        value['traits']['relaxed']['score'] = float('nan')
        assert client.post('/audio-vibes/samples', content=json.dumps(value)).status_code == 422
        assert client.post('/audio-vibes/samples', content=' ' * 65537).status_code == 413


def test_replacement_provenance_and_attribute_validation(store, monkeypatch):
    monkeypatch.setenv('YT_PLAYLIST_AUDIO_VIBES', '1')
    with TestClient(create_app(store, lambda: {}), base_url='http://127.0.0.1') as client:
        value = {**sample(), 'capture_reason': 'catch_up', 'position_seconds': 75.0}
        saved = client.post('/audio-vibes/samples', json=value)
        assert saved.status_code == 200, saved.text
        assert saved.json()['samples'][0]['capture_reason'] == 'catch_up'
        assert saved.json()['samples'][0]['audio_attributes']['values']['tempo_bpm'] == 124
        assert client.post('/audio-vibes/samples', json={**sample(),
            'capture_reason': 'catch_up', 'position_seconds': 29.0}).status_code == 422
        for key, bad in [('pcm', [0.1]), ('tempo_bpm', 400.0), ('integrated_lufs', float('inf')),
                         ('peak', -1.0), ('bark_loudness', 'loud')]:
            invalid = sample()
            invalid['audio_attributes']['values'][key] = bad
            assert client.post('/audio-vibes/samples', content=json.dumps(invalid)).status_code == 422
        # Old local extension results remain readable, without fabricating measurements.
        legacy = sample()
        del legacy['audio_attributes']
        assert client.post('/audio-vibes/samples', json=legacy).json()['samples'][0]['audio_attributes'] is None


def test_vibes_are_independent_cross_genre_targets_and_require_three_samples():
    club = sample(electronic=.95, House=.9, dance=.8)
    metal = sample(metal=.95, dance=.8, **{'heavy metal': .9})
    assert audio_vibe_preview.predict([club]) is None
    assert audio_vibe_preview.predict([club, metal]) is None
    assert audio_vibe_preview.predict([club] * 3)['ranking'] == audio_vibe_preview.predict([metal] * 3)['ranking']
    assert audio_vibe_preview.predict([club] * 3)['vibes'] == ['release']
    # Positive tone selects uplift separately from energy and force.
    high_energy = []
    for tone in (2.0, 8.0):
        value = sample(aggressive=.1)
        value['traits']['arousal'] = {'score': 8.0, 'per_patch': [8.0] * 3}
        value['traits']['valence'] = {'score': tone, 'per_patch': [tone] * 3}
        happy = .1 if tone == 2 else .9
        value['traits']['happy'] = {'score': happy, 'per_patch': [happy] * 3}
        high_energy.append(audio_vibe_preview.predict([value] * 3))
    assert high_energy[0]['vibes'] == ['energize']
    assert set(high_energy[1]['vibes']) == {'energize', 'uplift'}
    for target in ('energize', 'release'):
        assert next(r['score'] for r in high_energy[0]['ranking'] if r['vibe'] == target) == next(
            r['score'] for r in high_energy[1]['ranking'] if r['vibe'] == target)
    assert high_energy[0]['dimensions']['emotional_tone']['values'][0] < high_energy[1]['dimensions']['emotional_tone']['values'][0]
    quiet = sample(relaxed=1.0, aggressive=0.0, dance=.9)
    quiet['traits']['arousal'] = {'score': 3.0, 'per_patch': [3.0] * 3}
    prediction = audio_vibe_preview.predict([quiet] * 3)
    assert 'relax' in prediction['vibes'] and 'release' not in prediction['vibes']
    assert prediction['calibrated'] is False
    assert {row['vibe'] for row in prediction['ranking']} == set(home_vibe.PRESETS)
    heavy = sample(aggressive=1.0)
    heavy['traits']['arousal'] = {'score': 2.0, 'per_patch': [2.0] * 3}
    assert audio_vibe_preview.predict([heavy] * 3)['vibes'] == ['release']
    # Cruise has a genuine measured middle, rather than another name for calm.
    moderate = sample(relaxed=.5, aggressive=.1)
    moderate['traits']['arousal'] = {'score': 5.0, 'per_patch': [5.0] * 3}
    prediction = audio_vibe_preview.predict([moderate] * 3)
    assert prediction['vibes'] == ['cruise']


def test_every_complete_song_gets_a_best_guess_even_when_all_fits_are_weak():
    value = sample(relaxed=.3, aggressive=.55)
    for key, score in {'happy': .2, 'sad': .6, 'arousal': 5.8}.items():
        value['traits'][key] = {'score': score, 'per_patch': [score] * 3}
    assert audio_vibe_preview.predict([value] * 2) is None
    prediction = audio_vibe_preview.predict([value] * 3)
    assert all(row['score'] < audio_vibe_preview.MATCH_THRESHOLD for row in prediction['ranking'])
    assert prediction['selection_method'] == 'best_available'
    assert prediction['vibe'] == prediction['ranking'][0]['vibe']
    assert prediction['vibes'] == [prediction['vibe']]
    assert prediction['label'] == home_vibe.PRESETS[prediction['vibe']]['label']
    assert prediction['calibrated'] is False


def arc_sample(point, energy, force=.1):
    value = sample(point=point, relaxed=1 - energy, aggressive=force)
    value['traits']['arousal'] = {'score': 1 + 8 * energy, 'per_patch': [1 + 8 * energy] * 3}
    return value


def target_score(prediction, target):
    return next(row['score'] for row in prediction['ranking'] if row['vibe'] == target)


def test_arc_does_not_turn_quiet_and_intense_sections_into_moderate_cruise():
    points = ('thirty', 'middle', 'two_thirds')
    steady = audio_vibe_preview.predict([arc_sample(point, .5) for point in points])
    building = audio_vibe_preview.predict([arc_sample(point, energy) for point, energy in zip(points, (0, .5, 1))])
    # These have the same arithmetic feature means. Only the steady song has
    # observed, consistently moderate excerpts; averaging invented that fit.
    assert steady['vibe'] == 'cruise'
    assert building['vibe'] == 'energize'
    assert target_score(steady, 'cruise') > .8
    assert target_score(building, 'cruise') < .4
    assert building['aggregation'] == 'ordered_arc'
    assert building['dimensions']['intensity']['values'] == [0, .5, 1]
    assert building['dimensions']['intensity']['shape'] == 'rising'


def test_arc_distinguishes_build_fade_and_isolated_peak_with_the_same_values():
    points = ('thirty', 'middle', 'two_thirds')
    predictions = [audio_vibe_preview.predict([arc_sample(point, energy)
        for point, energy in zip(points, energies)])
        for energies in ((0, .5, 1), (1, .5, 0), (0, 1, 0), (1, 0, 1))]
    build, fade, peak, dip = predictions
    assert [value['dimensions']['intensity']['shape'] for value in predictions] == ['rising', 'falling', 'peak', 'dip']
    assert target_score(build, 'energize') > target_score(fade, 'energize')
    assert target_score(peak, 'energize') == 0
    assert peak['dimensions']['intensity']['maximum'] == 1
    assert peak['dimensions']['intensity']['steps'] == [1, -1]
    assert dip['dimensions']['intensity']['steps'] == [-1, 1]
    assert build['dimensions']['intensity']['net_change'] == 1
    assert fade['dimensions']['intensity']['net_change'] == -1


def test_forceful_middle_passage_is_retained_without_labeling_the_song_relaxed():
    values = [arc_sample(point, energy, force) for point, energy, force in (
        ('thirty', .1, .1), ('middle', .9, .9), ('two_thirds', .1, .1))]
    prediction = audio_vibe_preview.predict(values)
    assert prediction['vibe'] == 'release'
    assert 'relax' not in prediction['vibes']
    assert prediction['dimensions']['aggression']['shape'] == 'peak'
    release = next(row for row in prediction['ranking'] if row['vibe'] == 'release')
    assert release['score'] == pytest.approx(.9)
    assert release['rule'] == 'peak_excerpt'
    assert release['excerpt_scores'][1] > release['excerpt_scores'][0]


def test_arc_uses_actual_positions_and_keeps_replacement_timing_and_excerpt_ids():
    early = arc_sample('middle', .1)
    later = arc_sample('two_thirds', .5)
    replacement = arc_sample('thirty', .9)
    replacement.update(position_seconds=215.0, capture_reason='catch_up')
    prediction = audio_vibe_preview.predict([replacement, later, early])
    assert prediction == audio_vibe_preview.predict([early, replacement, later])
    arc = prediction['arc']
    assert arc['scope'] == 'sampled_excerpts'
    assert [row['sample_id'] for row in arc['excerpts']] == [early['id'], later['id'], replacement['id']]
    assert [row['point'] for row in arc['excerpts']] == ['middle', 'two_thirds', 'thirty']
    assert arc['excerpts'][-1]['target_seconds'] == 30
    assert arc['excerpts'][-1]['position_seconds'] == 215
    assert arc['excerpts'][-1]['capture_reason'] == 'catch_up'
    assert [row['gap_seconds'] for row in arc['transitions']] == pytest.approx([40, 54.9])
    assert arc['transitions'][0]['changes']['intensity'] == pytest.approx(.4)
    assert prediction['dimensions']['intensity']['shape'] == 'rising'
    assert prediction['sample_count'] == 3


def test_changing_labels_preserves_original_prediction_across_heuristic_versions(store, monkeypatch):
    first = sample()
    values = [first] + [sample(run_id=first['run_id'], point=point) for point in ('middle', 'two_thirds')]
    for value in values:
        store.audio_vibes.save_sample(value, 1)
    ids = [value['id'] for value in values]
    original = {'version': 'audio-vibe-preview-5', 'vibe': 'release', 'sample_count': 3,
                'dimensions': {'intensity': .6}}
    monkeypatch.setattr(audio_vibe_preview, 'predict', lambda samples, **kwargs: original)
    from yt_playlist.rec import audio_vibe_feedback
    audio_vibe_feedback.save(store, first['run_id'], ids, ['release'], audio_vibe_preview.TAXONOMY_VERSION, 2)
    monkeypatch.setattr(audio_vibe_preview, 'predict', lambda samples, **kwargs: {'version': 'new-version', 'vibe': 'energize'})
    audio_vibe_feedback.save(store, first['run_id'], ids[::-1], ['energize'], audio_vibe_preview.TAXONOMY_VERSION, 3)
    feedback = store.audio_vibes.feedback(first['run_id'])
    assert feedback['vibes'] == ['energize']
    assert feedback['prediction'] == original


def test_audio_migration_preserves_existing_settings_and_reopens(tmp_path):
    from yt_playlist.core.store import Store
    from yt_playlist.core.migrations import migrate
    from yt_playlist.core.migrations.registry import MIGRATIONS
    path = tmp_path / 'upgrade.db'
    previous = Store(path)
    migrate(previous.conn, registry=MIGRATIONS[:55])
    previous.conn.execute("INSERT INTO settings VALUES ('audio_test_sentinel','keep')")
    legacy_run = str(uuid4())
    previous.conn.execute('INSERT INTO audio_vibe_feedback VALUES (?,?,?,?,?)',
                          (legacy_run, '["historical-excerpt"]', '{"version":"old"}', 'focus', 5))
    previous.conn.execute('INSERT INTO audio_vibe_labels VALUES (?,?,?,?,?,?)',
        (legacy_run, 'audio-vibe-targets-1', '["four-target-excerpt"]', '{"version":"previous"}',
         '["gentle","move"]', 6))
    previous.conn.commit()
    previous.conn.close()
    upgraded = Store(path)
    upgraded.init_schema()
    assert upgraded.settings.get_setting('audio_test_sentinel') == 'keep'
    value = sample(run_id=legacy_run)
    upgraded.audio_vibes.save_sample(value, 10)
    assert upgraded.audio_vibes.feedback(legacy_run) is None
    assert len(upgraded.audio_vibes.earlier_feedback(legacy_run)) == 2
    from yt_playlist.rec import audio_vibe_feedback
    audio_vibe_feedback.save(upgraded, legacy_run, [value['id']], ['relax', 'cruise'],
                             audio_vibe_preview.TAXONOMY_VERSION, 11)
    upgraded.conn.close()
    reopened = Store(path)
    reopened.init_schema()
    assert reopened.conn.execute('SELECT COUNT(*) FROM sample_attributes').fetchone()[0] == 1
    assert reopened.audio_vibes.legacy_feedback(legacy_run) == {
        'vibe': 'focus', 'sample_ids': ['historical-excerpt'], 'prediction': {'version': 'old'}, 'updated_at': 5}
    assert reopened.audio_vibes.feedback(legacy_run)['vibes'] == ['relax', 'cruise']
    assert reopened.audio_vibes.earlier_feedback(legacy_run)[0] == {
        'taxonomy_version': 'audio-vibe-targets-1', 'vibes': ['gentle', 'move'],
        'sample_ids': ['four-target-excerpt'], 'prediction': {'version': 'previous'}, 'updated_at': 6}
    reopened.conn.close()


def test_feedback_without_analysis_keeps_song_and_observation_frozen(store, monkeypatch):
    monkeypatch.setenv('YT_PLAYLIST_AUDIO_VIBES', '1')
    run_id = str(uuid4())
    with TestClient(create_app(store, lambda: {}), base_url='http://127.0.0.1') as client:
        playback = {'run_id': run_id, 'video_id': 'no-analysis', 'title': 'Unanswered song', 'artist': 'Artist'}
        assert client.post('/audio-vibes/runs', json=playback).status_code == 200
        label = {'run_id': run_id, 'sample_ids': [], 'vibes': ['relax'],
                 'taxonomy_version': 'audio-session-targets-1'}
        response = client.post('/audio-vibes/feedback', json=label)
        assert response.status_code == 200
        frozen = response.json()['feedback']
        assert frozen['prediction'] is None and frozen['sample_ids'] == []
        value = sample(run_id=run_id)
        value.update(video_id='no-analysis')
        response = client.post('/audio-vibes/samples', json=value)
        assert response.status_code == 200
        assert response.json()['feedback'] == frozen
        assert client.post('/audio-vibes/runs', json={**playback, 'video_id': 'other'}).status_code == 409
        assert client.post('/audio-vibes/feedback', json={**label, 'run_id': str(uuid4())}).status_code == 409
        response = client.post('/audio-vibes/feedback', json={**label, 'vibes': ['cruise']})
        assert response.json()['feedback']['vibes'] == ['cruise']
        assert response.json()['feedback']['sample_ids'] == []
        assert client.get('/audio-vibes/runs/' + run_id).json()['playback']['title'] == 'Unanswered song'
    # An absent prediction is also a frozen observation across policy versions.
    from yt_playlist.rec import audio_vibe_feedback
    monkeypatch.setattr(audio_vibe_preview, 'predict', lambda samples: {'version': 'later-policy'})
    audio_vibe_feedback.save(store, run_id, [], ['relax'], 'audio-session-targets-1', 10)
    assert store.audio_vibes.feedback(run_id)['prediction'] is None


@pytest.mark.parametrize('previous_labels', [['relax', 'cruise'], []])
def test_replay_restores_latest_exact_video_answer_without_copying_or_changing_it(store, monkeypatch, previous_labels):
    monkeypatch.setenv('YT_PLAYLIST_AUDIO_VIBES', '1')
    first, latest, unrelated, replay = [str(uuid4()) for _ in range(4)]
    store.audio_vibes.save_playback(first, 'repeat-song', 'Song', 'Artist', 1)
    store.audio_vibes.save_feedback(first, [], ['release'], audio_vibe_preview.TAXONOMY_VERSION, None, 2)
    # Older sample-only runs also identify the exact video, without a runs row.
    value = sample(run_id=latest)
    value['video_id'] = 'repeat-song'
    store.audio_vibes.save_sample(value, 3)
    store.audio_vibes.save_feedback(latest, [value['id']], previous_labels,
                                    audio_vibe_preview.TAXONOMY_VERSION, None, 4)
    store.audio_vibes.save_playback(unrelated, 'another-song', 'Song', 'Artist', 5)
    store.audio_vibes.save_feedback(unrelated, [], ['energize'], audio_vibe_preview.TAXONOMY_VERSION, None, 6)
    frozen = store.audio_vibes.feedback(latest)
    with TestClient(create_app(store, lambda: {}), base_url='http://127.0.0.1') as client:
        response = client.get(f'/audio-vibes/runs/{replay}?video_id=repeat-song')
        assert response.status_code == 200
        state = response.json()
        assert state['previous_feedback'] == {'run_id': latest, **frozen}
        assert state['feedback'] is None and state['samples'] == []
        assert store.audio_vibes.feedback(replay) is None
        assert store.audio_vibes.playback(replay) is None, 'Looking up an earlier answer must be read-only'
        body = {'run_id': replay, 'video_id': 'repeat-song', 'title': 'Song', 'artist': 'Artist'}
        assert client.post('/audio-vibes/runs', json=body).json()['previous_feedback'] == {'run_id': latest, **frozen}
        # A real playback's identity overrides a conflicting caller-supplied hint.
        assert client.get(f'/audio-vibes/runs/{replay}?video_id=another-song').json()['previous_feedback']['run_id'] == latest
        answer = {'run_id': replay, 'sample_ids': [], 'vibes': ['concentrate'],
                  'taxonomy_version': audio_vibe_preview.TAXONOMY_VERSION}
        state = client.post('/audio-vibes/feedback', json=answer).json()
        assert state['feedback']['vibes'] == ['concentrate']
        assert state['previous_feedback'] is None
        assert store.audio_vibes.feedback(latest) == frozen


def test_previous_answer_lookup_ignores_earlier_taxonomies(store):
    earlier, current = str(uuid4()), str(uuid4())
    store.audio_vibes.save_playback(current, 'exact-song', 'Song', 'Artist', 1)
    store.audio_vibes.save_feedback(current, [], ['cruise'], audio_vibe_preview.TAXONOMY_VERSION, None, 2)
    store.audio_vibes.save_playback(earlier, 'exact-song', 'Song', 'Artist', 3)
    store.audio_vibes.save_feedback(earlier, [], ['gentle'], 'audio-vibe-targets-1', None, 4)
    assert store.audio_vibes.latest_feedback('exact-song', exclude_run_id=str(uuid4()))['run_id'] == current
    assert store.audio_vibes.latest_feedback('unknown-song', exclude_run_id=current) is None


def test_sampler_replaces_missed_points_without_seeking_and_deduplicates_reloads():
    node = shutil.which('node')
    if not node:
        pytest.skip('Node.js required for scheduling checks')
    script = r"""
import assert from 'node:assert/strict';
import {AudioVibeSampler,samplingPoints,sampleMetadata} from './src/yt_playlist/web/static/audio-vibe-sampler.js';
import {AudioPreviewSession} from './src/yt_playlist/web/static/audio-preview-session.js';
let now=1000000,position=0,playing={video_id:'song',title:'Song',duration:240,paused:false};
let captures=0,changes=[],collected=[],cached=null;
const session={port:{},current:null,validPlaying:()=>playing.paused?null:{...playing,position},
 start(){this.current={};captures++},check(){return true},stop(){this.current=null}};
const opts={session,playback:()=>({...playing,position}),clock:()=>now,
 save:value=>{cached=structuredClone(value)},changed:value=>changes.push(structuredClone(value)),result:value=>collected.push(value)};
const sampler=new AudioVibeSampler(opts);
sampler.tick(); assert.equal(captures,0);
position=29.9;sampler.tick();assert.equal(captures,0);
position=30;sampler.tick();assert.equal(captures,1);
sampler.tick();assert.equal(captures,1);
session.current=null;
sampler.tick();assert.equal(captures,1);
now+=5000;position=35;playing.paused=true;sampler.tick();assert.equal(captures,1);
playing.paused=false;sampler.tick();assert.equal(captures,2);
assert.equal(sampler.active.reason,'catch_up');
// Mark success exactly as the response handler does; never duplicate a saved point.
sampler.active=null;session.current=null;sampler.run.points.thirty='saved';sampler.persist();
const resumed=new AudioVibeSampler({...opts,cached});resumed.tick();assert.equal(captures,2);
position=30;resumed.tick();assert.equal(captures,2);
// Seeking past two points queues two distinct replacement excerpts, one at a time.
position=170;resumed.tick();assert.equal(captures,3);assert.equal(resumed.active.point.key,'middle');
resumed.tick();assert.equal(captures,3);
resumed.active=null;session.current=null;resumed.run.points.middle='saved';
position=185;resumed.tick();assert.equal(captures,4);assert.equal(resumed.active.point.key,'two_thirds');
assert.equal(resumed.active.reason,'catch_up');
resumed.active=null;session.current=null;resumed.run.points.two_thirds='saved';
position=239;resumed.tick();assert.equal(captures,4);
playing={...playing,video_id:'new_song'};resumed.tick();
assert.equal(resumed.run.points.thirty,'too_short');assert.equal(captures,4,'never capture past the end');
position=100;resumed.run.points={};resumed.tick();assert.equal(captures,5);
assert.equal(resumed.active.reason,'catch_up');
// Keep recovering beyond three failures, with backoff and no tight loop.
const beforeRetries=captures;
for(let i=0;i<5;i++) {
  session.current=null;resumed.tick();resumed.tick();
  assert.equal(captures,beforeRetries+i);
  now+=31000;resumed.tick();
}
assert.equal(resumed.run.attempts.thirty,6);
assert.equal(captures,beforeRetries+5);
// Finish the old song's immutable excerpt even after playback moves on.
const oldRun=resumed.run;
session.current.analysing=true;
playing={...playing,video_id:'after_analysis'};position=0;resumed.tick();
assert.equal(resumed.active.run,oldRun);
resumed.completed({observed_playback:{video_id:oldRun.video_id},prediction:{scores:{},bundle:{files:[]},
 moods:{traits:{},models:{}}}});
assert.equal(collected.at(-1).run_id,oldRun.id);
assert.notEqual(resumed.run.id,oldRun.id);
session.current=null;
assert.equal(samplingPoints(null).length,1);
assert.deepEqual(samplingPoints(240).map(p=>p.seconds),[30,120,160]);
// A continuous native media timeline is diagnostic context, not song position.
const metadata={observed_playback:{video_id:'song'},song_position_at_start:30.25,
  source_position_at_start:1722.25,observed_position_at_start:2,
  prediction:{scores:{},bundle:{files:[]},moods:{traits:{},models:{}}}};
const context={run:{id:'clock-run'},point:{key:'thirty',seconds:30},duration:195};
const excerpt=sampleMetadata(metadata,context);
assert.equal(excerpt.position_seconds,30.25);
assert.equal(excerpt.source_position_seconds,1722.25);
assert.equal(excerpt.capture_reason,'scheduled');
metadata.prediction.embedding={version:'musicnn-embedding-1',values:Array(200).fill(-.25)};
assert.deepEqual(sampleMetadata(metadata,context).embedding,metadata.prediction.embedding);
delete metadata.song_position_at_start;
metadata.observed_position_at_start=31;
assert.equal(sampleMetadata(metadata,context).position_seconds,31,
  'an older extension cannot pass raw media time off as song position');
// A synchronous port failure also consumes an attempt and respects backoff.
let disconnectedStarts=0;
position=40;playing={...playing,video_id:'disconnected_song'};
const disconnected={...session,start(){disconnectedStarts++},current:null};
const recovering=new AudioVibeSampler({...opts,session:disconnected,cached:null});
for(let i=0;i<6;i++) {
  recovering.tick();recovering.tick();assert.equal(disconnectedStarts,i+1);now+=31000;
}
assert.equal(disconnectedStarts,6);
assert.equal(recovering.run.attempts.thirty,6);
// Controlled scheduling must never auto-start when the extension becomes ready.
const controlled=new AudioPreviewSession({playback:()=>({video_id:'x',paused:false,position:30,at:Date.now()}),
 changed(){},result(){},automatic:false});
controlled.port={postMessage(){throw Error('Unscheduled capture')},disconnect(){}};
controlled.tryAutomatic();assert.equal(controlled.current,null);controlled.destroy();
"""
    result = subprocess.run([node, '--input-type=module', '-e', script],
        cwd=Path(__file__).resolve().parents[1], capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr
