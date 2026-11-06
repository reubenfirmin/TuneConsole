"""Vibe preparation learns only from measurements, with sparse, durable cohorts."""
import json
from dataclasses import replace
import time
from uuid import uuid4

import pytest

from tests.test_audio_vibes import sample
from yt_playlist.core import catalog, fao, vibe_provenance
from yt_playlist.core.audio_vibe_contract import TAXONOMY_VERSION
from yt_playlist.core.store import Store
from yt_playlist.providers.base import MetadataObservation
from yt_playlist.rec import home_vibe, musical_character, musical_preparation, vibe_prediction as policy
from yt_playlist.rec import vibe_preparation, vibe_joint_model as joint, vibe_joint_preparation
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
    started, deferred = time.monotonic(), 0
    for turn in range(500):
        if (not store.prepared_content.has_pending() and not musical_preparation.due(store)
                and not vibe_preparation.due(store)):
            return worker
        if time.monotonic() - started >= 120:
            break
        # False includes a bounded source/capture deferral. Keep the same
        # worker's fair turns and adaptive batch size for the next admission.
        if not worker.capture(1000):
            deferred += 1
    store.maintenance_diagnostics.flush(force=True)
    pytest.fail('Prediction work did not converge: ' + repr({
        'turns': turn + 1, 'deferred': deferred, 'seconds': time.monotonic() - started,
        'batch': worker.batch, 'content': store.prepared_content.progress(),
        'source': store.musical_profiles.pending(),
        'vibe': tuple(store.vibe_predictions.pending() or ()),
        'joint': {vibe: vibe_joint_preparation.state(store, vibe) for vibe in home_vibe.PRESETS},
    }))


def fits(store, key):
    return store.musical_profiles.by_keys([key])[key]['vibes']['fits']


@pytest.fixture
def qualified_candidate(monkeypatch):
    # Controlled lifecycle tests exercise the candidate. This is not production
    # qualification or evidence that the held-out/resource gates have passed.
    monkeypatch.setattr(vibe_joint_preparation, 'QUALIFICATIONS',
                        {vibe_joint_preparation.config_identity(): 'controlled-fixture'})


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

    def measured(identity, score=.7, artist=1, genre='psybient', album=None, source='audio'):
        return {'recording': identity, 'measurement': identity, 'score': score, 'support': 1.,
                'artists': [artist], 'genre': genre, 'album': album, 'source': source}

    def prediction(rows, *, artist=1, genre='psybient'):
        posterior = joint.Posterior(joint.fit(rows, 'relax', fixed=joint.INITIAL))
        return posterior.predict([artist] if artist else [], genre)

    one = [measured(1)]
    ten = [measured(index) for index in range(1, 11)]
    first, agreeing = prediction(one), prediction(ten)
    assert .5 < first['score'] < agreeing['score'] <= .7 + 1e-12
    assert agreeing['mean_variance'] < first['mean_variance']
    assert agreeing['new_track_variance'] - agreeing['mean_variance'] == pytest.approx(joint.INITIAL['residual'])
    assert agreeing['new_track_variance'] > joint.INITIAL['residual']
    assert prediction(ten + ten) == agreeing  # Identical replays/uploads do not add songs.
    assert prediction([*ten, measured(11, .1)])['score'] < agreeing['score']
    with pytest.raises(ValueError, match='Conflicting original'):
        prediction([*one, measured(1, .1)])
    assert prediction(one, artist=None, genre=None) is None
    unknown = prediction(one, artist=999, genre='unknown-specific-genre')
    assert .5 < unknown['score'] <= .7
    assert unknown['mean_variance'] > first['mean_variance']
    assert unknown['new_track_variance'] > unknown['mean_variance']
    same_artist = prediction(ten, artist=999)
    diverse = prediction([measured(index, artist=index) for index in range(1, 11)], artist=999)
    assert diverse['mean_variance'] < same_artist['mean_variance']

    specific = [measured(1, .9)]
    broad = [measured(2, .1, genre='other'), measured(3, .1, artist=2)]
    pooled = prediction(specific + broad)
    assert pooled['source'] == 'artist_genre'
    assert pooled['score'] < prediction(specific)['score']  # Both broader effects participate jointly.
    assert pooled['recordings'] == 3
    clustered = prediction([measured(i, artist=i, album='shared') for i in range(1, 11)], artist=999)
    independent = prediction([measured(i, artist=i, album=str(i)) for i in range(1, 11)], artist=999)
    assert independent['mean_variance'] < clustered['mean_variance']
    posterior = joint.Posterior(joint.fit(one, 'relax', fixed=joint.INITIAL))
    with pytest.raises(ValueError, match='hidden'):
        posterior.predict([1], 'psybient', recording=1)

    # An independent dense Gaussian oracle covers crossed effects, fractional
    # artist credits and unequal observation noise. Also keep old immutable
    # posterior generations readable while the new representation prepares.
    import base64
    import numpy as np

    crossed = [measured(1, .8, album='a', source='a'),
               measured(2, .3, genre='other', album='a', source='b'),
               measured(3, .6, artist=2, album='b', source='a'),
               measured(4, .7, artist=3, genre='other', album='b', source='a')]
    crossed[1]['artists'] = [1, 2]
    for row, noise in zip(crossed, (.01, .025, 0., .03)):
        row['noise'] = noise
    columns = {
        'artist': [[1, .5, 0, 0], [0, .5, 1, 0], [0, 0, 0, 1]],
        'genre': [[1, 0, 1, 0], [0, 1, 0, 1]],
        'interaction': [[1, 0, 0, 0], [0, .5, 0, 0], [0, .5, 0, 0],
                        [0, 0, 1, 0], [0, 0, 0, 1]],
        'album': [[1, 1, 0, 0], [0, 0, 1, 1]],
        'source': [[1, 0, 1, 1], [0, 1, 0, 0]],
    }
    covariance = np.full((4, 4), joint.BASELINE_VARIANCE)
    for kind, vectors in columns.items():
        for vector in vectors:
            covariance += joint.INITIAL[kind] * np.outer(vector, vector)
    covariance += np.diag([joint.INITIAL['residual'] + row['noise'] for row in crossed])
    lower = np.linalg.cholesky(covariance)
    alpha = np.linalg.solve(covariance, np.array([row['score'] - .5 for row in crossed]))
    legacy = joint.Posterior({'format': 1, 'inputs': crossed, 'variances': joint.INITIAL,
        'alpha': alpha.tolist(), 'cholesky': base64.b64encode(
            lower[np.tril_indices(4)].astype('<f8').tobytes()).decode('ascii')})
    fitted = joint.fit(crossed, 'relax', fixed=joint.INITIAL)
    sparse = joint.Posterior({**fitted, 'format': 2})
    # The likelihood itself must equal the dense determinant/quadratic form.
    dense_loss = .5 * (np.array([row['score'] - .5 for row in crossed]) @ alpha
        + np.linalg.slogdet(covariance)[1] + 4 * np.log(2 * np.pi))
    assert fitted['diagnostics']['negative_log_likelihood'] == pytest.approx(dense_loss, abs=1e-10)
    for artists, genre, album in (([1, 2], 'psybient', 'a'), ([999], 'psybient', None),
                                   ([1], 'unknown', 'unknown'), ([3], None, 'b')):
        expected = legacy.predict(artists, genre, album=album)
        actual = sparse.predict(artists, genre, album=album)
        for field in ('score', 'support', 'mean_variance', 'new_track_variance'):
            assert actual[field] == pytest.approx(expected[field], abs=1e-12)

    # Independent finite-source mixture oracle: include covariance of the
    # weighted source mean, and retain between-source variation for a new song.
    source_predictions, source_cross = [], []
    query_vectors = {'artist': [1, 0, 0, 0], 'genre': [1, 0],
                     'interaction': [1, 0, 0, 0, 0], 'album': [1, 0]}
    for source in range(2):
        cross = np.full(4, joint.BASELINE_VARIANCE)
        prior = joint.BASELINE_VARIANCE
        for kind, vectors in columns.items():
            weights = [int(index == source) for index in range(2)] if kind == 'source' else query_vectors[kind]
            for weight, values in zip(weights, vectors):
                cross += joint.INITIAL[kind] * weight * np.array(values)
                prior += joint.INITIAL[kind] * weight * weight
        source_predictions.append((float(.5 + cross @ alpha),
            float(prior - cross @ np.linalg.solve(covariance, cross))))
        source_cross.append(cross)
    estimate = joint.Posterior(fitted).predict([1], 'psybient', album='a')
    weights = [.75, .25]
    expected_mean = sum(weight * mean for weight, (mean, _variance) in zip(weights, source_predictions))
    expected_variance = sum(weight * (variance + (mean - expected_mean) ** 2)
        for weight, (mean, variance) in zip(weights, source_predictions)) + joint.INITIAL['residual']
    cross_mean = sum(weight * cross for weight, cross in zip(weights, source_cross))
    prior_mean = (joint.BASELINE_VARIANCE + sum(joint.INITIAL[kind] for kind in query_vectors)
                  + joint.INITIAL['source'] * sum(weight ** 2 for weight in weights))
    assert estimate['score'] == pytest.approx(expected_mean, abs=1e-12)
    assert estimate['mean_variance'] == pytest.approx(
        prior_mean - cross_mean @ np.linalg.solve(covariance, cross_mean), abs=1e-12)
    assert estimate['new_track_variance'] == pytest.approx(expected_variance, abs=1e-12)

    # Exercise growth beyond the former dense row ceiling without a new case
    # or a costly large synthetic database. Every original must remain present.
    growing = [measured(i, artist=i // 3 + 1, album=str(i // 7)) for i in range(1, 2051)]
    fitted = joint.fit(growing, 'relax', fixed=joint.INITIAL)
    assert len(fitted['inputs']) == len(growing)
    assert fitted['diagnostics']['accounted_memory_bytes'] <= joint.MEMORY_BYTES
    assert joint.Posterior(fitted).predict([1], 'psybient')['recordings'] == len(growing)
    with pytest.raises(joint.WorkBudgetExceeded, match='memory budget'):
        joint.fit(growing, 'relax', memory_bytes=1024)
    crowded = measured(1)
    crowded['artists'] = list(range(1, 401))
    with pytest.raises(joint.WorkBudgetExceeded, match='sparse fill'):
        joint.fit([crowded], 'relax', memory_bytes=12 * 1024 * 1024)


def test_audio_wins_then_joint_artist_and_subgenre_then_sparse_fallback(store, qualified_candidate):
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
    assert predicted['relax']['recordings'] == 2
    assert genre['relax']['source'] == 'genre'
    assert fallback['relax']['source'] == 'artist'
    prediction_id, = store.conn.execute('SELECT prediction_id FROM recording_vibes v '
        'JOIN recordings r ON r.id=v.recording_id WHERE r.identity_key=? AND v.vibe=?',
        (empty, 'relax')).fetchone()
    receipt = vibe_provenance.read(store.conn, prediction_id)
    assert receipt['new_track_variance'] > receipt['mean_variance'] > 0
    assert predicted['relax']['score'] > fallback['relax']['score']
    compact = musical_character.load(store, [measured, other_style])
    assert home_vibe.affinity({'key': measured}, 'relax', compact) == own['relax']['score']
    assert musical_character.intensity({'musical': compact[measured]}) < .2
    assert musical_character.intensity({'musical': compact[other_style]}) > .8


def test_replays_are_one_recording_partial_runs_do_not_replace_complete_and_annotations_never_seed(store, qualified_candidate):
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
    assert not any('audio_annotation' in sql or 'audio_taxonom' in sql for sql in statements)


@pytest.mark.parametrize('genre', ['Electronic', 'Techno', 'House', 'Ambient', 'Rock'])
def test_broad_genres_cannot_seed_genre_predictions(store, genre):
    track(store, 'measured', genre, 'One artist')
    _tid, target = track(store, 'target', genre, 'Another artist')
    capture(store, 'measured')
    drain(store)
    assert fits(store, target)['relax']['source'] == 'heuristic'
    assert not store.conn.execute('SELECT 1 FROM recording_vibes WHERE subgenre_key IS NOT NULL').fetchone()


def test_provider_classifiers_seed_predictions_without_captured_audio_or_prepared_state(file_backed_store, qualified_candidate):
    store = file_backed_store
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


def test_changed_measurement_and_subgenre_retract_old_cohort_and_notify_without_content_refit(store, qualified_candidate):
    from yt_playlist.core import vibe_model_files, vibe_provenance
    tid, own = track(store, 'one')
    _tid, target = track(store, 'target')
    capture(store, 'one')
    drain(store)
    counters = store.prepared_content.counters()
    before = fits(store, target)['relax']['score']
    first_generation = store.settings.get_setting(vibe_model_files.HEAD_PREFIX + 'relax')
    first_prediction = store.conn.execute("SELECT v.prediction_id FROM recording_vibes v JOIN recordings r "
        "ON r.id=v.recording_id WHERE r.identity_key=? AND v.vibe='relax'", (target,)).fetchone()[0]
    first_inputs = vibe_provenance.read(store.conn, first_prediction)['inputs']
    # Equal model scores still need a new identity when the original sample
    # manifest changes. Replaying is not an extra recording in the fit.
    capture(store, 'one')
    drain(store, batch=1)
    equal_prediction = store.conn.execute("SELECT v.prediction_id FROM recording_vibes v JOIN recordings r "
        "ON r.id=v.recording_id WHERE r.identity_key=? AND v.vibe='relax'", (target,)).fetchone()[0]
    assert fits(store, target)['relax']['score'] == before
    assert equal_prediction != first_prediction
    assert store.settings.get_setting(vibe_model_files.HEAD_PREFIX + 'relax') != first_generation
    assert vibe_provenance.read(store.conn, equal_prediction)['inputs'] != first_inputs
    assert fits(store, target)['relax']['recordings'] == 1
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


@pytest.mark.parametrize('failure', [vibe_preparation.WorkBudgetExceeded, fao.FileBudgetExceeded, fao.FileChanged])
def test_ready_joint_vibes_refresh_when_last_channel_defers(store, monkeypatch, qualified_candidate, failure):
    """One deferred channel must not strand other newly published models."""
    from yt_playlist.core import vibe_model_files

    _entry, own = track(store, 'partial-original')
    _entry, target = track(store, 'partial-target')
    capture(store, 'partial-original')
    drain(store, batch=1)

    def generation(key, vibe):
        return store.conn.execute(
            'SELECT hex(g.generation) FROM recording_vibes v '
            'JOIN recordings r ON r.id=v.recording_id '
            'JOIN prediction_model_generations g ON g.prediction_id=v.prediction_id '
            'WHERE r.identity_key=? AND v.vibe=?', (key, vibe)).fetchone()[0].lower()

    healthy, deferred = 'relax', vibe_joint_preparation.VIBES[-1]
    previous_own_score = fits(store, own)[healthy]['score']
    previous_healthy = generation(target, healthy)
    previous_deferred = store.settings.get_setting(vibe_model_files.HEAD_PREFIX + deferred)
    original_fit = joint.fit
    attempts = []

    def defer_last(rows, vibe, **kwargs):
        if vibe == deferred:
            attempts.append(vibe)
            raise failure('controlled single-channel fit deferral')
        return original_fit(rows, vibe, **kwargs)

    monkeypatch.setattr(joint, 'fit', defer_last)
    capture(store, 'partial-original', calm=False)
    drain(store, batch=1)

    assert attempts == [deferred]
    assert fits(store, own)[healthy]['source'] == 'audio_measurement'
    assert fits(store, own)[healthy]['score'] < previous_own_score
    healthy_state = vibe_joint_preparation.state(store, healthy)
    deferred_state = vibe_joint_preparation.state(store, deferred)
    assert vibe_joint_preparation.current(store, healthy_state)
    assert healthy_state['outcome'] == 'ready'
    assert deferred_state['outcome'] == 'deferred'
    assert deferred_state['retry_at'] > vibe_joint_preparation.time.time()
    assert store.settings.get_setting(vibe_model_files.HEAD_PREFIX + deferred) == previous_deferred
    current_healthy = store.settings.get_setting(vibe_model_files.HEAD_PREFIX + healthy)
    assert current_healthy != previous_healthy
    assert generation(target, healthy) == current_healthy
    assert generation(target, deferred) == previous_deferred
    assert not store.prepared_content.has_pending()
    assert store.vibe_predictions.pending() is None
    # Retrying the same deferred inputs must not rescan the library again when
    # no channel published a replacement in the meantime.
    retry_at = deferred_state['retry_at']
    monkeypatch.setattr(vibe_joint_preparation.time, 'time', lambda: retry_at + 1)
    assert vibe_joint_preparation.due(store)
    assert vibe_joint_preparation.step(store)
    assert attempts == [deferred, deferred]
    assert vibe_joint_preparation.state(store, deferred)['attempts'] == 2
    assert store.vibe_predictions.pending() is None
    assert generation(target, healthy) == current_healthy
    assert generation(target, deferred) == previous_deferred


def test_deferral_and_refresh_queue_roll_back_together(store, monkeypatch):
    from yt_playlist.core import vibe_model_files
    from yt_playlist.repos.vibe_predictions import VibePredictionsRepo

    key = vibe_model_files.STATE_PREFIX + 'relax'
    previous = {'outcome': 'deferred', 'retry_at': 0}
    store.settings.set_values({key: json.dumps(previous)})
    before = [tuple(row) for row in store.conn.execute(
        'SELECT group_key,revision,cursor FROM rec_vibe_pending ORDER BY position')]
    original_queue = VibePredictionsRepo._queue

    def fail_queue(repo, keys):
        original_queue(repo, keys)
        raise RuntimeError('interrupted partial refresh enqueue')

    monkeypatch.setattr(VibePredictionsRepo, '_queue', fail_queue)
    with pytest.raises(RuntimeError, match='interrupted partial refresh enqueue'):
        store.vibe_predictions.defer_joint_model('relax', {**previous, 'retry_at': 60}, True)
    assert vibe_joint_preparation.state(store, 'relax') == previous
    assert [tuple(row) for row in store.conn.execute(
        'SELECT group_key,revision,cursor FROM rec_vibe_pending ORDER BY position')] == before


def test_prediction_queue_survives_restart_and_failure_rolls_back_all_derived_state(tmp_path, monkeypatch, qualified_candidate):
    path = tmp_path / 'predictions.db'
    store = Store(path)
    store.init_schema()
    track(store, 'one')
    _tid, target = track(store, 'two')
    capture(store, 'one')
    from yt_playlist.repos.vibe_predictions import VibePredictionsRepo
    original = VibePredictionsRepo.publish_prepared
    def fail(repo, *args):
        original(repo, *args)
        raise RuntimeError('interrupted prediction publication')
    with monkeypatch.context() as patch:
        patch.setattr(VibePredictionsRepo, 'publish_prepared', fail)
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


def test_model_version_change_recomputes_all_evidence_in_pages(store, monkeypatch, qualified_candidate):
    from yt_playlist.core import vibe_versions
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
    assert store.conn.execute('SELECT COUNT(DISTINCT recording_id) FROM recording_vibes v WHERE instr(' +
                              vibe_versions.expression(store.conn, 'recording_vibes', 'v') + ',?)>0',
                              ('changed-model',)).fetchone()[0] == 2


def test_artist_tags_never_become_recording_measurements_or_propagation_seeds(file_backed_store, monkeypatch):
    store = file_backed_store
    monkeypatch.setattr(vibe_joint_preparation, 'QUALIFICATIONS', {})
    monkeypatch.setattr(joint, 'fit', lambda *a, **k: pytest.fail('Unqualified candidate fit'))
    from yt_playlist.repos.vibe_predictions import VibePredictionsRepo
    monkeypatch.setattr(VibePredictionsRepo, 'joint_model', lambda *a, **k: pytest.fail('Unqualified model read'))
    tid, own = track(store, 'one')
    _tid, target = track(store, 'two')
    store.enrichment_state.record_metadata_observations(tid, 'lastfm', [
        MetadataObservation('tags', [{'name': 'Ambient'}], 'artist', 'shared', 'reported',
                            match_evidence={'method': 'title_artist_match'})], run_id='artist-tags', now=1)
    drain(store)
    assert fits(store, own)['relax']['source'] == fits(store, target)['relax']['source'] == 'heuristic'
    assert not store.conn.execute('SELECT 1 FROM measurements').fetchone()
    assert not vibe_joint_preparation.due(store)


def test_removed_audio_retracts_predictions_and_keeps_recording_fallback(store, qualified_candidate):
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


def test_provider_backlog_cannot_starve_vibe_bootstrap_or_dependency_fanout(file_backed_store):
    store = file_backed_store
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


def test_continuing_backlog_completes_a_model_cycle_across_worker_restart(file_backed_store, monkeypatch):
    from yt_playlist.core import vibe_model_files
    store = file_backed_store
    monkeypatch.setattr(vibe_joint_preparation, 'QUALIFICATIONS', {})
    track(store, 'cycle-original')
    track(store, 'cycle-target')
    capture(store, 'cycle-original')
    drain(store)
    monkeypatch.setattr(vibe_joint_preparation, 'QUALIFICATIONS',
        {vibe_joint_preparation.config_identity(): 'controlled-backlog-fixture'})
    monkeypatch.setattr(store.prepared_content, 'has_pending', lambda: True)
    monkeypatch.setattr(store.musical_profiles, 'pending', lambda: True)
    source_turns, fits_seen = [], []
    original_fit = joint.fit
    def source_step(*args, **kwargs):
        assert not vibe_joint_preparation.active(store)
        source_turns.append(True)
        return True
    def fit(rows, vibe, **kwargs):
        fits_seen.append(vibe)
        return original_fit(rows, vibe, **kwargs)
    monkeypatch.setattr(musical_preparation, 'source_step', source_step)
    monkeypatch.setattr(joint, 'fit', fit)
    worker = Preparation(store)
    for _ in range(4):
        worker.capture(1000)
        if fits_seen:
            break
    assert len(fits_seen) == 1
    assert vibe_joint_preparation.active(store)
    started = vibe_joint_preparation.state(store, fits_seen[0])['attempted_at']
    with monkeypatch.context() as interrupted:
        interrupted.setattr(vibe_joint_preparation.time, 'time',
            lambda: started + len(vibe_joint_preparation.VIBES) * 10 + 1)
        # Repeated failed snapshot publications cannot reserve the worker
        # indefinitely. An abandoned cycle returns to ordinary fair turns.
        assert not vibe_joint_preparation.active(store)
    # Durable per-channel state resumes after a worker restart. Our source
    # worker cannot change originals between the remaining seven admissions.
    source_before = len(source_turns)
    worker = Preparation(store)
    for _ in range(len(vibe_joint_preparation.VIBES) - 1):
        assert worker.capture(1000)
    assert len(source_turns) == source_before
    assert fits_seen == list(vibe_joint_preparation.VIBES)
    assert not vibe_joint_preparation.active(store)
    assert all(vibe_joint_preparation.state(store, vibe)['outcome'] == 'ready'
               for vibe in vibe_joint_preparation.VIBES)
    assert store.vibe_predictions.pending() is not None
    # New input revision during a backlog keeps the published heads and gives
    # upstream work a full cadence before starting another expensive cycle.
    heads = {v: store.settings.get_setting(vibe_model_files.HEAD_PREFIX + v)
             for v in vibe_joint_preparation.VIBES}
    attempted = max(vibe_joint_preparation.state(store, v)['attempted_at']
                    for v in vibe_joint_preparation.VIBES)
    revision = int(store.settings.get_setting(vibe_model_files.REVISION) or 0)
    store.settings.set_setting(vibe_model_files.REVISION, str(revision + 1))
    monkeypatch.setattr(vibe_joint_preparation.time, 'time', lambda: attempted + 1)
    assert not vibe_joint_preparation.admitted(store, waiting=True)
    assert vibe_joint_preparation.admitted(store, waiting=False)
    assert heads == {v: store.settings.get_setting(vibe_model_files.HEAD_PREFIX + v)
                     for v in vibe_joint_preparation.VIBES}
    monkeypatch.setattr(vibe_joint_preparation.time, 'time', lambda: attempted + 61)
    assert vibe_joint_preparation.admitted(store, waiting=True)
