"""Vibe preparation learns only from measurements, with sparse, durable cohorts."""
import json
from contextlib import closing
from dataclasses import replace
import time
from uuid import uuid4

import pytest

from tests.test_audio_vibes import sample
from tests.genre_catalog_support import bound_shipped_catalog
from tests.recommendation_support import seed_publication_subjects
from tests.store_fixture_support import (
    bounded_consumer_schema, initialize_consumer, mock_snapshot_coordinator, native_sql_calls,
    reuse_reporting_reader, without_query_timing,
)
from yt_playlist.core import catalog, content_work, fao, schema_inspection, sqlite_database as store_core, vibe_provenance
from yt_playlist.core.audio_vibe_contract import TAXONOMY_VERSION
from yt_playlist.core.activity import WorkBudgetExceeded
from yt_playlist.core.db_revisions import Revisions
from yt_playlist.core.db_writes import atomic_write
from yt_playlist.core.store import Store
from yt_playlist.providers.base import MetadataObservation
from yt_playlist.rec import home_vibe, musical_character, musical_preparation, vibe_prediction as policy
from yt_playlist.rec import (vibe_preparation, vibe_recording_rules, vibe_joint_model as joint,
                             vibe_joint_state as vibe_joint_preparation,
                             vibe_joint_preparation as joint_commands)
from yt_playlist.rec.progressive import Budget, Preparation


pytestmark = pytest.mark.usefixtures('without_query_timing')


@pytest.fixture(autouse=True)
def controlled_measurements(request, monkeypatch):
    """Exercise storage/lifecycle with known audio responses, not fitted quality.

    Specialist inference and artifact rollouts have their own production tests.
    These subjects vary captured native mood heads to control cohort responses.
    """
    if 'arithmetic_rollout' in request.node.name:
        return
    from types import SimpleNamespace
    from yt_playlist.library import vibe_composition
    from experiments.audio_lab_tools.legacy import audio_vibe_prediction

    def predict(samples, **options):
        result = audio_vibe_prediction.predict(samples, use_model=False, **options)
        return {**result, 'suitability': {row['vibe']: row['score'] for row in result['ranking']}} if result else None

    monkeypatch.setattr(vibe_composition, 'prediction', lambda: SimpleNamespace(
        capture=lambda: SimpleNamespace(predict=predict, usable_samples=lambda samples: samples)))


@pytest.fixture(autouse=True)
def disable_periodic_library_sync():
    """The two HTTP consumers explicitly construct apps without background work."""


@pytest.fixture(scope='module')
def prediction_schema():
    # TEMP guards belong to each physical consumer, not its immutable SQL seed.
    with bounded_consumer_schema((), snapshot_guards=False) as schema:
        yield schema


@pytest.fixture
def prediction_input(prediction_schema, tmp_path, monkeypatch, native_sql_calls):
    from yt_playlist.core import genre_identity
    from yt_playlist.core.migrations import registry

    assert prediction_schema.execute('PRAGMA query_only').fetchone()[0] == 1
    assert list(map(tuple, prediction_schema.execute(
        'SELECT version,name FROM schema_migrations ORDER BY version'))) == [
            (entry.version, entry.name) for entry in registry.MIGRATIONS]
    assert prediction_schema.execute('SELECT count(*) FROM recordings').fetchone()[0] == 0
    assert prediction_schema.execute('SELECT count(*) FROM recording_vibes').fetchone()[0] == 0
    assert prediction_schema.execute('SELECT count(*) FROM genre_catalog').fetchone()[0] == 0
    assert not prediction_schema.execute('PRAGMA foreign_key_check').fetchone()


    # Keep the documented parent edges used by the categorical-prior check.
    bound_shipped_catalog(monkeypatch, ('psybient', 'detroit techno', 'electronic', 'techno', 'edm'))
    store = Store(tmp_path / 'predictions.db')
    try:
        # These consumers own committed prediction facts, not power-loss recovery.
        store.conn.execute('PRAGMA synchronous=OFF')
        initialize_consumer(store, prediction_schema, snapshot_guards=False)
        store._snapshot_revisions = Revisions(store.conn)
        # Retain the generated SQL for the independent restart, without first
        # generating and discarding an identical set on the immutable seed.
        prediction_schema.capture_revisions(store._snapshot_revisions)
        assert prediction_schema._revision_script
        store.conn.execute('PRAGMA wal_autocheckpoint=0')
        with store.conn:
            genre_identity.seed_catalog(store.conn)
        with schema_inspection.reuse_tables(store.conn):
            yield store
    finally:
        store.conn.close()


@pytest.fixture
def prediction_store(prediction_input, monkeypatch):
    with reuse_reporting_reader(prediction_input, monkeypatch):
        yield prediction_input


@pytest.fixture
def uncontended_prediction_store(prediction_input, monkeypatch):
    with mock_snapshot_coordinator(prediction_input, monkeypatch):
        yield prediction_input


def track(store, name, genre='Psybient', artist='Shared artist'):
    tid = store.tracks.upsert_track(name, name, artist, '', 240)
    with store.conn:
        catalog.choose_field(store.conn, tid, 'genre', genre)
    return tid, store.tracks.identity_key_for_video(name)


def catalog_track(store, name, genre='Psybient', artist='Shared artist'):
    """Completed catalog inputs beneath measurement and prediction owners."""
    from yt_playlist.util.matching import identity_key

    key = identity_key(name, artist)
    with atomic_write(store):
        seed_publication_subjects(store, [{'key': key, 'video_id': name, 'title': name,
            'artist': artist, 'genre': genre, 'duration_s': 240}])
        tid = store.conn.execute('SELECT id FROM track_sources WHERE video_id=?', (name,)).fetchone()[0]
        store.artists.observe(tid, artist, source='fixture')
    return tid, key


def capture(store, video, *, calm=True, count=3, run=None, start=0):
    run = run or str(uuid4())
    now = 100 + store.conn.execute('SELECT COUNT(*) FROM sample_attributes').fetchone()[0]
    samples = []
    for point in ('thirty', 'middle', 'two_thirds')[start:start + count]:
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
    return store.musical_profiles.by_keys([key])[key].get('vibes', {}).get('fits', {})


@pytest.fixture
def individual_model_rollout(monkeypatch):
    # Individual artifact rollout owns measured scores. Qualified joint
    # preparation and its generation references have separate lifecycle coverage.
    monkeypatch.setattr(vibe_joint_preparation, 'qualified', lambda: False)


@pytest.fixture
def qualified_candidate(monkeypatch):
    # Controlled lifecycle tests exercise the candidate. This is not production
    # qualification or evidence that the held-out/resource gates have passed.
    monkeypatch.setattr(vibe_joint_preparation, 'QUALIFICATIONS',
                        {vibe_joint_preparation.config_identity(): 'controlled-fixture'})


@pytest.fixture
def relax_joint_channel(monkeypatch):
    # These identity/retraction assertions observe relax. Other channels retain
    # their model and full-cycle owners elsewhere in this module.
    monkeypatch.setattr(vibe_joint_preparation, 'VIBES', ('relax',))


@pytest.fixture
def fixed_measurement_version(monkeypatch):
    # These consumers change evidence and joint models, not shipped specialists.
    # Read their real identities once rather than reopening the same files on
    # every queue poll. Artifact replacement has separate rollout owners below.
    version = vibe_preparation.version()
    monkeypatch.setattr(vibe_recording_rules, 'version', lambda: version)


def test_ordinary_preparation_uses_qualified_release_and_rejects_config_drift(file_backed_store, monkeypatch):
    store = file_backed_store
    assert vibe_joint_preparation.qualified()
    track(store, 'release-original')
    _entry, target = track(store, 'release-target')
    capture(store, 'release-original')
    worker = drain(store)
    worker.close()
    states = {vibe: vibe_joint_preparation.state(store, vibe) for vibe in vibe_joint_preparation.VIBES}
    assert all(vibe_joint_preparation.current(store, value) and value['outcome'] == 'ready'
               for value in states.values())
    heads = [store.vibe_predictions.joint_state(vibe)[0]
             for vibe in vibe_joint_preparation.VIBES]
    assert len(set(heads)) == len(vibe_joint_preparation.VIBES)
    assert store.vibe_predictions.has_joint_scores(heads)
    assert store.conn.execute('SELECT 1 FROM recording_vibes v '
        'JOIN recordings r ON r.id=v.recording_id '
        'JOIN predictions p ON p.id=v.prediction_id '
        'WHERE r.identity_key=? AND p.model_generation IS NOT NULL', (target,)).fetchone()
    assert fits(store, target)['relax']['source'] == 'artist_genre'

    configuration = joint.configuration()
    monkeypatch.setattr(joint, 'configuration', lambda: {**configuration, 'version': 'unreviewed-change'})
    assert not vibe_joint_preparation.qualified()
    assert not vibe_joint_preparation.due(store)
    assert vibe_joint_preparation.posterior(store, 'relax') is None


def test_direct_audio_stays_authoritative_after_a_provider_component_correction(store, monkeypatch):
    from fastapi.testclient import TestClient

    from tests.audio_model_support import disable_models
    from yt_playlist.web.app import create_app

    monkeypatch.setenv('YT_PLAYLIST_AUDIO_VIBES', '1')
    disable_models(monkeypatch)
    entry, key = track(store, 'override-song')
    with store.conn:
        catalog.choose_field(store.conn, entry, 'mood_relaxed', .9, now=10)
    drain(store)
    item = {'musical': musical_character.load(store, [key])[key]}
    assert musical_character.dimension(item, 'intensity')[0] < .5
    assert musical_character.intensity_evidence(item) == (None, 0)

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

    intensity = None
    for correction in (.9, .8):
        if correction == .8:
            with store.conn:
                catalog.choose_field(store.conn, entry, 'mood_relaxed', correction, now=1001)
        drain(store)
        item = {'musical': musical_character.load(store, [key])[key]}
        assert item['musical']['dimensions']['relaxed']['override'] is True
        assert {fit['source'] for fit in item['musical']['vibes']['fits'].values()} == {'audio_measurement'}
        context = musical_character.provenance(item)
        assert context['intensity_support'] == 1 and context['intensity_source'] == 'specialist_ensemble'
        assert {component['source'] for component in context['intensity_ensemble']['components'].values()} == {
            'audio_measurement'}
        intensity = context['intensity'] if intensity is None else intensity
        assert musical_character.intensity_evidence(item) == (intensity, 1)
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


def test_audio_wins_then_joint_projection_requires_a_sampled_artist(
        uncontended_prediction_store, qualified_candidate, relax_joint_channel, fixed_measurement_version):
    store = uncontended_prediction_store
    _tid, measured = catalog_track(store, 'measured', 'Psybient', 'Artist A')
    _tid, other_style = catalog_track(store, 'other-style', 'Detroit Techno', 'Artist B')
    cases = {
        'exact': ('Psybient', 'Artist A', 'artist_genre'),
        'artist-only': (None, 'Artist A', 'artist'),
        'split': ('Detroit Techno', 'Artist A', 'artist'),
        'genre-only': ('Psybient', 'Artist C', None),
        'unrelated': ('Electronic', 'Artist C', None),
    }
    targets = [(catalog_track(store, name, genre, artist)[1], source)
               for name, (genre, artist, source) in cases.items()]
    capture(store, 'measured')
    capture(store, 'other-style', calm=False)
    drain(store).close()
    own, techno = fits(store, measured), fits(store, other_style)
    assert own['relax']['source'] == techno['relax']['source'] == 'audio_measurement'
    assert own['relax']['score'] > techno['relax']['score']
    for key, source in targets:
        row = store.conn.execute('SELECT v.source,v.prediction_id FROM recording_vibes v '
            'JOIN recordings r ON r.id=v.recording_id WHERE r.identity_key=? AND v.vibe=?',
            (key, 'relax')).fetchone()
        fit = fits(store, key).get('relax')
        if source is None:
            assert row is None and fit is None
            assert not store.vibe_predictions.fit_scores([key], 'relax')
        else:
            assert row['source'] == fit['source'] == source
            assert fit['recordings'] == 2
            receipt = vibe_provenance.read(store.conn, row['prediction_id'])
            assert receipt['new_track_variance'] > receipt['mean_variance'] > 0
    compact = musical_character.load(store, [measured, other_style])
    assert home_vibe.affinity({'key': measured}, 'relax', compact) == own['relax']['score']
    assert musical_character.intensity({'musical': compact[measured]}) < .2
    assert musical_character.intensity({'musical': compact[other_style]}) > .8


def test_full_on_uses_specialists_and_their_predictions_in_prepared_radio(
        uncontended_prediction_store, qualified_candidate, tmp_path, monkeypatch):
    from tests.musical_support import record_acousticbrainz
    from tests.test_audio_vibe_cruise import samples
    from tests.test_audio_vibe_energize import controlled as energize_artifact
    from tests.test_audio_vibe_release import controlled as release_artifact
    from yt_playlist.core.audio_vibe_contract import INTENSITY_CHANNELS
    from yt_playlist.library import vibe_composition
    from yt_playlist.rec import acousticbrainz_vibes, audio_vibe_specialists, radio_intensity, radio_ranking, radio_tuning

    # Run actual specialist inference with controlled scores and compatible excerpts.
    monkeypatch.setattr(vibe_composition, 'prediction', vibe_composition.configured)
    artifacts = {'energize': energize_artifact(tmp_path, .94), 'release': release_artifact(tmp_path, .76)}
    for name, specialist in audio_vibe_specialists.SPECIALISTS:
        monkeypatch.setenv(specialist.MODE_KEY, 'active' if name in artifacts else 'off')
        if name in artifacts:
            monkeypatch.setenv(specialist.ENVIRONMENT_KEY, str(artifacts[name]))
    monkeypatch.setattr(vibe_joint_preparation, 'VIBES', tuple(INTENSITY_CHANNELS))

    store = uncontended_prediction_store
    _entry, measured = catalog_track(store, 'sampled', 'Psybient', 'Artist A')
    _entry, same_subgenre = catalog_track(store, 'same-subgenre', 'Psybient', 'Artist A')
    _entry, other_subgenre = catalog_track(store, 'other-subgenre', 'Detroit Techno', 'Artist A')
    entry, provider = catalog_track(store, 'provider', 'Psybient', 'Artist B')
    record_acousticbrainz(store, entry, dict.fromkeys(acousticbrainz_vibes.FIELDS, .82))
    excerpts = samples('sampled')
    for now, row in enumerate(excerpts, 1):
        store.audio_vibes.save_sample(row, now)
    drain(store).close()

    keys = [measured, same_subgenre, other_subgenre, provider]
    profiles = musical_character.load(store, keys)
    items = {key: {'key': key, 'musical': profiles[key]} for key in keys}
    assert musical_character.intensity_evidence(items[measured]) == (pytest.approx(.94), 1.)
    assert profiles[provider]['vibes']['fits']['energize']['source'] == acousticbrainz_vibes.SOURCE
    assert musical_character.intensity_evidence(items[provider]) == (None, 0)

    retained = store.vibe_predictions.retained_joint_fits([same_subgenre, other_subgenre])
    for channel in INTENSITY_CHANNELS:
        for key, source in ((same_subgenre, 'artist_genre'), (other_subgenre, 'artist')):
            fit = retained[key][channel]
            assert fit['source'] == source
            receipt = vibe_provenance.read(store.conn, fit['prediction_id'])
            [original] = store.vibe_predictions.joint_model(receipt['generation'])['inputs']
            assert original['source'].startswith('audio_measurement/')
            assert {sid for sid, in store.conn.execute('SELECT sample_id FROM measurement_samples '
                'WHERE measurement_id=?', (original['measurement'],))} == {row['id'] for row in excerpts}

    radio_catalog = {'members': {'style': keys}}
    radio_intensity.prepare(store, radio_catalog, {}, {})
    bands = radio_catalog['intensity']['clusters']['style']
    assert {key for group in bands['25'].values() for key in group} == {provider}
    assert {key for group in bands['47'].values() for key in group} == {measured}
    for key in (same_subgenre, other_subgenre):
        score, support = musical_character.intensity_evidence(items[key])
        assert score is not None and support > 0
        band = str(radio_intensity.band(score))
        assert any(key in group for group in bands[band].values())
    options = dict(prior=0, recent=[], target=None, feedback=[], breadth=0, genre_bonus=0,
                   tuning={**radio_tuning.DEFAULTS, 'energy': 1.})
    scores = {key: radio_ranking.candidate_score(item, **options) for key, item in items.items()}
    assert len(set(scores.values())) == 1


@pytest.mark.parametrize('installed', [False, True], ids=['new-capture', 'installed-fit'])
def test_sampled_artist_dispatch_publishes_before_the_catalog_cursor(
        uncontended_prediction_store, qualified_candidate, relax_joint_channel,
        fixed_measurement_version, monkeypatch, installed):
    from yt_playlist.controllers import vibe_joint_preparation as commands

    store = uncontended_prediction_store
    artist = 'Measured artist'
    _entry, original = catalog_track(store, 'b-original', 'Psybient', artist)
    unrelated = [catalog_track(store, f'a-unrelated-{index}', 'Psybient', 'Other artist')[1]
                 for index in range(8)]
    _entry, related = catalog_track(store, 'z-related', 'Detroit Techno', artist)
    entry, collaboration = catalog_track(store, 'z-collaboration', 'Psybient', 'Measured artist & Guest')
    store.artists.observe(entry, 'Measured artist & Guest', [{'name': artist}, {'name': 'Guest'}], source='youtube')
    targets = {collaboration: 'artist_genre', related: 'artist'}
    if not installed:
        catalog_track(store, 'b-earlier-sample', 'Psybient', 'Earlier artist')
        capture(store, 'b-earlier-sample', calm=False)
    drain(store, batch=1).close()
    previous_head = store.vibe_predictions.joint_state('relax')[0]
    assert bool(previous_head) is not installed
    capture(store, 'b-original')

    worker = Preparation(store, budget=replace(Budget(), batch=1))
    try:
        if installed:
            with monkeypatch.context() as legacy:
                legacy.setattr(commands, 'sampled_artists', lambda *_args: [])
                for _ in range(50):
                    worker.capture(1000)
                    if head := store.vibe_predictions.joint_state('relax')[0]:
                        break
                else:
                    pytest.fail('The legacy fit did not publish')
            state = vibe_joint_preparation.state(store, 'relax')
            state.pop('dispatch_version')
            store.vibe_predictions.repository.save_joint_outcome('relax', state)
            assert all('relax' not in fits(store, key) for key in targets)

            def unexpected_fit(*_args, **_kwargs):
                pytest.fail('Dispatch from an installed generation should not refit it')

            monkeypatch.setattr(joint, 'fit', unexpected_fit)

        published = set()
        for _ in range(80):
            worker.capture(1000)
            for key, source in targets.items():
                if key in published or not (fit := fits(store, key).get('relax')):
                    continue
                cursor = store.conn.execute("SELECT cursor FROM rec_vibe_pending WHERE group_key='*'").fetchone()
                assert cursor is not None and cursor[0] < key
                assert fit['source'] == source
                published.add(key)
            if published == targets.keys():
                break
        assert published == targets.keys()
        if installed:
            assert store.vibe_predictions.joint_state('relax')[0] == head
        else:
            assert store.vibe_predictions.joint_state('relax')[0] != previous_head
    finally:
        worker.close()
    drain(store, batch=1).close()
    assert fits(store, original)['relax']['source'] == 'audio_measurement'
    assert all('relax' not in fits(store, key) for key in unrelated)


def test_artist_dispatch_coalesces_new_heads_without_restarting_an_active_page(
        uncontended_prediction_store, fixed_measurement_version):
    from yt_playlist.repos.vibe_predictions import JOINT_ARTIST

    store = uncontended_prediction_store
    entry, first = catalog_track(store, 'first')
    _entry, second = catalog_track(store, 'second')
    [artist] = json.loads(store.vibe_predictions.inputs([first])[first]['artist'])
    key = f'{JOINT_ARTIST}relax:{artist}'
    repo = store.vibe_predictions.repository
    with store.conn:
        store.conn.execute('DELETE FROM rec_vibe_pending WHERE position>0')
        content_work.acknowledge(store.conn, content_work.pending(store.conn, -1))

    repo.install_joint_model('relax', 'a' * 64, {}, [], True, artists=[artist])
    assert repo.fanout(1)
    assert tuple(repo.pending()) == (key, 1, str(entry))
    for generation in (marker * 64 for marker in 'bcd'):
        repo.install_joint_model('relax', generation, {}, [], True, artists=[artist])
        assert tuple(repo.pending()) == (key, 2, str(entry))
    for _ in range(5):
        assert repo.fanout(1)
    assert repo.pending()['group_key'] == '*'
    assert [subject for subject, _ in content_work.pending(store.conn, 10)] == [first, second]


def test_masked_metadata_refits_after_categorical_changes_without_learning_own_numeric_fields(
        prediction_store, qualified_candidate, relax_joint_channel):
    store = prediction_store
    entry, own = catalog_track(store, 'metadata-original')
    _entry, target = catalog_track(store, 'metadata-target')
    capture(store, 'metadata-original')
    drain(store)

    def current():
        value = store.vibe_predictions.joint_model(store.vibe_predictions.joint_state('relax')[0])
        assert value['format'] == 4 and len(value['inputs']) == 1
        return value['inputs'][0], value

    original, first = current()
    with store.conn:
        catalog.choose_field(store.conn, entry, 'mood_relaxed', .99)
    drain(store)
    numeric, _value = current()
    assert numeric['metadata_prior'] == original['metadata_prior']
    assert numeric['metadata_profile_digest'] == original['metadata_profile_digest']
    with store.conn:
        catalog.choose_field(store.conn, entry, 'genre', 'Detroit Techno')
    drain(store)
    categorical, changed = current()
    assert categorical['metadata_prior'] != original['metadata_prior']
    assert categorical['metadata_profile_digest'] != original['metadata_profile_digest']
    assert changed != first
    assert fits(store, own)['relax']['source'] == 'audio_measurement'
    assert fits(store, target)['relax']['source'] == 'artist'
    prediction, = store.conn.execute('SELECT v.prediction_id FROM recording_vibes v JOIN recordings r '
        "ON r.id=v.recording_id WHERE r.identity_key=? AND v.vibe='relax'", (target,)).fetchone()
    assert 'metadata_prior' in vibe_provenance.read(store.conn, prediction)['conditioning']
    revision = store.vibe_predictions.joint_revision()
    # A derived profile publication and queue admission cannot feed back into
    # fitting. The replay has exactly the same raw inputs and current originals.
    with store.conn:
        content_work.enqueue(store.conn, (target,))
    drain(store)
    assert store.vibe_predictions.joint_revision() == revision


def test_symbolic_cache_rechecks_memory_and_distinguishes_equal_size_graphs(monkeypatch):
    from scipy.sparse import csr_matrix
    from yt_playlist.rec import vibe_joint_common

    monkeypatch.setattr(vibe_joint_common, '_ORDERING_CACHE', None)
    first = csr_matrix([[1., 1., 0.], [1., 0., 1.]])
    expected = joint._ordering(first, 1000, joint.MEMORY_BYTES)
    with monkeypatch.context() as cached:
        cached.setattr(vibe_joint_common.heapq, 'heapify', lambda *_: pytest.fail('Repeated symbolic elimination'))
        # Weights and observation values do not change precision sparsity.
        assert joint._ordering(first * .5, 2000, joint.MEMORY_BYTES) == (
            expected[0], expected[1], expected[2] + 1000)
        with pytest.raises(joint.WorkBudgetExceeded, match='sparse fill'):
            joint._ordering(first, 2000, expected[2])
    # Shape and nonzero count alone cannot identify the elimination graph.
    second = csr_matrix([[1., 1., 1.], [1., 0., 0.]])
    actual = joint._ordering(second, 1000, joint.MEMORY_BYTES)
    monkeypatch.setattr(vibe_joint_common, '_ORDERING_CACHE', None)
    assert actual == joint._ordering(second, 1000, joint.MEMORY_BYTES)
    assert actual[1] != expected[1]


def test_partial_audio_has_less_influence_at_the_same_score():
    row = {'recording': 1, 'measurement': 1, 'score': .9, 'artists': [1], 'genre': 'psybient',
           'album': None, 'source': 'audio_measurement/fixture', 'metadata_prior': .5}
    estimates = []
    for support in (1/3, 2/3, 1.):
        model = joint.fit([{**row, 'support': support}], 'relax', fixed=joint.INITIAL)
        posterior = joint.Posterior(model)
        estimates.append(posterior.predict([1], 'psybient', metadata_prior=.5))
    one, two, three = estimates
    assert .5 < one['score'] < two['score'] < three['score'] < .9
    assert one['mean_variance'] > two['mean_variance'] > three['mean_variance']
    assert one['new_track_variance'] > two['new_track_variance'] > three['new_track_variance']


def test_complete_capture_upgrades_partial_measurement_with_exact_provenance(
        prediction_store, qualified_candidate, relax_joint_channel):
    store = prediction_store
    _entry, measured = catalog_track(store, 'measured')
    _entry, target = catalog_track(store, 'target')
    drain(store).close()
    assert 'relax' not in fits(store, measured)
    assert 'relax' not in fits(store, target)

    capture(store, 'measured', calm=False, start=1, count=1)
    run, sample_ids, measurement_ids = str(uuid4()), set(), set()
    for index in range(3):
        [saved] = capture(store, 'measured', run=run, start=index, count=1)
        sample_ids.add(saved['id'])
        drain(store).close()
        direct = fits(store, measured)['relax']
        assert direct['source'] == 'audio_measurement'
        assert direct['support'] == pytest.approx((index + 1) / 3)
        prediction = fits(store, target)['relax']
        assert prediction['source'] == 'artist_genre'
        assert prediction['recordings'] == 1
        assert prediction['support'] > 0
        [measurement_id] = store.conn.execute(
            'SELECT v.measurement_id FROM recording_vibes v JOIN recordings r ON r.id=v.recording_id '
            "WHERE r.identity_key=? AND v.vibe='relax'", (measured,)).fetchone()
        assert measurement_id not in measurement_ids
        measurement_ids.add(measurement_id)
        assert {sid for sid, in store.conn.execute('SELECT sample_id FROM measurement_samples '
            'WHERE measurement_id=?', (measurement_id,))} == sample_ids
        generation = store.vibe_predictions.joint_state('relax')[0]
        [original] = store.vibe_predictions.joint_model(generation)['inputs']
        assert original['measurement'] == measurement_id
        assert original['support'] == direct['support']


def test_replays_are_one_recording_partial_runs_do_not_replace_complete_and_annotations_never_seed(
        prediction_store, qualified_candidate, relax_joint_channel):
    store = prediction_store
    _tid, one = catalog_track(store, 'one')
    _tid, two = catalog_track(store, 'two')
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
        content_work.enqueue(store.conn, (two,))
    drain(store)
    store.conn.set_trace_callback(None)
    assert not any('audio_annotation' in sql or 'audio_taxonom' in sql for sql in statements)


@pytest.mark.parametrize('genre', ['Electronic', 'Techno', 'House', 'Ambient', 'Rock'])
def test_broad_genres_cannot_seed_genre_predictions(store, relax_joint_channel, genre):
    track(store, 'measured', genre, 'One artist')
    _tid, target = track(store, 'target', genre, 'One artist')
    _tid, unrelated = track(store, 'unrelated', genre, 'Another artist')
    capture(store, 'measured')
    drain(store).close()
    predicted = fits(store, target)['relax']
    assert predicted['source'] == 'artist'
    assert 'relax' not in fits(store, unrelated)
    prediction, = store.conn.execute('SELECT v.prediction_id FROM recording_vibes v JOIN recordings r '
        "ON r.id=v.recording_id WHERE r.identity_key=? AND v.vibe='relax'", (target,)).fetchone()
    assert vibe_provenance.read(store.conn, prediction)['conditioning']['genre'] is None
    fitted = store.vibe_predictions.joint_model(store.vibe_predictions.joint_state('relax')[0])
    assert all(row['genre'] is None for row in fitted['inputs'])
    assert not store.conn.execute('SELECT 1 FROM recording_vibes WHERE subgenre_key IS NOT NULL').fetchone()


def test_unverified_provider_classifiers_do_not_supply_vibes_or_seed_predictions(
        prediction_store, qualified_candidate, relax_joint_channel):
    store = prediction_store
    tid, own = catalog_track(store, 'provider')
    _tid, target = catalog_track(store, 'target')
    store.enrichment_state.record_metadata_observations(tid, 'acousticbrainz', [
        MetadataObservation('mood_relaxed', .98, 'recording', 'native-recording', 'prediction',
                            match_evidence={'method': 'title_artist_match'}),
        MetadataObservation('mood_aggressive', .01, 'recording', 'native-recording', 'prediction',
                            match_evidence={'method': 'title_artist_match'})], run_id='provider', now=100)
    drain(store, batch=1).close()
    for key in (own, target):
        profile = store.musical_profiles.by_keys([key])[key]
        assert not profile.get('vibes', {}).get('fits')
        assert home_vibe.affinity({'musical': profile}, 'relax') == .5
    assert vibe_joint_preparation.state(store, 'relax')['outcome'] == 'empty'
    assert not store.audio_vibes.samples('provider')


def test_acousticbrainz_estimates_and_daily_refit_use_verified_recording_pairs(
        prediction_store, individual_model_rollout, monkeypatch):
    from types import SimpleNamespace

    from tests.musical_support import record_acousticbrainz
    from yt_playlist.rec import acousticbrainz_vibe_preparation as daily, acousticbrainz_vibes as mapping

    store = prediction_store
    stamp = mapping.measurement_version()
    monkeypatch.setattr(vibe_recording_rules, 'version', lambda: stamp)
    now = 1000
    monkeypatch.setattr(daily, 'time', SimpleNamespace(time=lambda: now))
    assert daily.step(store)

    def matched(name):
        entry, key = catalog_track(store, name)
        record_acousticbrainz(store, entry, dict.fromkeys(mapping.FIELDS, .82))
        recording = store.conn.execute('SELECT id FROM recordings WHERE identity_key=?', (key,)).fetchone()[0]
        return recording, key

    rid, own = matched('sampled')
    _unplayed, _unplayed_key = matched('unplayed')
    target_rid, target = matched('estimated')
    _other, other = catalog_track(store, 'unrelated')
    account = store.accounts.upsert_account('listener', None, True)
    store.history.record_play_event(account, own, 'sampled', 100)
    store.history.record_play_event(account, own, 'sampled', 500)
    capture(store, 'sampled')
    capture(store, 'unplayed', calm=False)
    drain(store).close()

    direct = fits(store, own)['relax']
    assert direct['source'] == 'audio_measurement'
    initial = mapping.score([.82] * len(mapping.FIELDS), mapping.coefficients({}))
    assert {vibe: fit['score'] for vibe, fit in fits(store, target).items()} == initial
    assert all(fit['source'] == mapping.SOURCE for fit in fits(store, target).values())
    profile = store.musical_profiles.by_keys([target])[target]
    assert musical_character.intensity_evidence({'musical': profile}) == (None, 0)
    rows = store.vibe_predictions.acousticbrainz_training_rows(stamp)
    assert len(rows) == len(mapping.VIBES) and {row[0] for row in rows} == {rid}
    assert not daily.step(store)

    now += mapping.INTERVAL
    assert daily.step(store)
    assert daily.step(store) is False
    assert daily.rescore(store, 1)
    assert daily.rescore(store, 1)
    assert daily.rescore(store, 1) is False
    assert not store.prepared_content.has_pending()
    changed = fits(store, target)['relax']['score']
    assert abs(changed - direct['score']) < abs(initial['relax'] - direct['score'])
    assert fits(store, own)['relax'] == direct
    assert home_vibe.affinities(store, [target, other], 'relax') == {target: changed, other: .5}
    assert {row['recording_id'] for row in store.vibe_predictions.ranked('relax')} >= {target_rid, rid}

    now += mapping.INTERVAL
    with monkeypatch.context() as unchanged:
        unchanged.setattr(mapping, 'adjusted', lambda *_: pytest.fail('Refitted unchanged pairs'))
        assert daily.step(store)
    assert store.vibe_predictions.pending() is None

    entry = store.conn.execute('SELECT id FROM track_sources WHERE recording_id=?', (target_rid,)).fetchone()[0]
    store.metadata_choices.choose(entry, 'mb_recording_id', 'cc08a6cc-62a3-42ff-aa3c-844ce15e0cfa')
    assert home_vibe.affinities(store, [target], 'relax') == {target: .5}
    assert target not in store.vibe_predictions.acousticbrainz_matches([target])
    store.metadata_choices.choose(entry, 'mb_recording_id', withdraw=True)
    assert home_vibe.affinities(store, [target], 'relax') == {target: changed}

    _source, selected = store.vibe_predictions.acousticbrainz_matches([target])[target]
    store.metadata_choices.choose(entry, 'mb_recording_id', selected)
    with store.conn:
        store.conn.execute('UPDATE recordings SET mb_recording_id=? WHERE id=?',
                           ('cc08a6cc-62a3-42ff-aa3c-844ce15e0cfa', target_rid))
    drain(store).close()
    assert home_vibe.affinities(store, [target], 'relax') == {target: changed}
    with store.conn:
        store.conn.execute("UPDATE recording_identifiers SET status='conflicting' WHERE recording_id=?", (target_rid,))
    assert store.prepared_content.has_pending()
    assert home_vibe.affinities(store, [target], 'relax') == {target: .5}
    drain(store).close()
    assert not store.conn.execute('SELECT 1 FROM rec_musical_links WHERE recording_id=? '
                                  'AND acousticbrainz_classifiers IS NOT NULL', (target_rid,)).fetchone()


def test_changed_measurement_invalidates_content_and_subgenre_retracts_old_cohort(
        uncontended_prediction_store, qualified_candidate, relax_joint_channel, fixed_measurement_version):
    from yt_playlist.core import vibe_provenance

    store = uncontended_prediction_store
    assert store._snapshot_revisions.conn is store.conn
    assert not store.conn.execute('PRAGMA foreign_key_check').fetchone()
    tid, own = catalog_track(store, 'one')
    _, target = catalog_track(store, 'target')
    capture(store, 'one')
    drain(store).close()
    content_changes, evidence_changes = store.prepared_content.counters()
    before = fits(store, target)['relax']['score']
    first_generation = store.vibe_predictions.joint_state('relax')[0]
    first_prediction = store.conn.execute("SELECT v.prediction_id FROM recording_vibes v JOIN recordings r "
        "ON r.id=v.recording_id WHERE r.identity_key=? AND v.vibe='relax'", (target,)).fetchone()[0]
    first_inputs = vibe_provenance.read(store.conn, first_prediction)['inputs']
    assert first_generation and first_prediction and first_inputs
    assert fits(store, target)['relax']['recordings'] == 1
    # A replay changes provenance and model identity without adding a recording.
    capture(store, 'one')
    drain(store, batch=1).close()
    equal_prediction = store.conn.execute("SELECT v.prediction_id FROM recording_vibes v JOIN recordings r "
        "ON r.id=v.recording_id WHERE r.identity_key=? AND v.vibe='relax'", (target,)).fetchone()[0]
    assert fits(store, target)['relax']['score'] == before
    assert equal_prediction != first_prediction
    assert store.vibe_predictions.joint_state('relax')[0] != first_generation
    assert vibe_provenance.read(store.conn, equal_prediction)['inputs'] != first_inputs
    assert fits(store, target)['relax']['recordings'] == 1
    assert store.prepared_content.counters() == (content_changes, evidence_changes)
    refresh = store.rec_refresh.revision() or 0

    capture(store, 'one', calm=False)
    drain(store, batch=1).close()
    assert fits(store, target)['relax']['score'] < before
    assert fits(store, target)['relax']['recordings'] == 1
    updated_content, updated_evidence = store.prepared_content.counters()
    assert updated_content > content_changes
    assert updated_evidence == evidence_changes
    assert store.rec_refresh.revision() > refresh

    with store.conn:
        catalog.choose_field(store.conn, tid, 'genre', 'Detroit Techno')
    drain(store, batch=1).close()
    assert fits(store, target)['relax']['source'] == 'artist'
    assert fits(store, own)['relax']['source'] == 'audio_measurement'
    assert store.rec_refresh.revision() is not None


@pytest.mark.parametrize('installed_version', [107])
def test_installed_writes_survive_deferred_fit_restart_correction_and_retraction(
        tmp_path, _pristine_installed_schema, monkeypatch, qualified_candidate, installed_version):
    """Exercise genuine installed layouts, rather than the current empty schema."""
    from yt_playlist.core import vibe_model_files
    from yt_playlist.core import vibe_models

    path = tmp_path / 'installed.db'
    store = Store(path)
    original_fit = joint.fit
    try:
        _pristine_installed_schema(installed_version).backup(store.conn)
        store.init_schema()
        history = [tuple(row) for row in store.conn.execute('SELECT * FROM schema_migrations')]
        tid, own = track(store, 'one')
        _tid, target = track(store, 'target')
        capture(store, 'one')
        def deferred(*_args, **_kwargs):
            raise joint.WorkBudgetExceeded('controlled initial fit deferral')
        with monkeypatch.context() as patch:
            patch.setattr(joint, 'fit', deferred)
            drain(store, batch=1).close()
        assert fits(store, own)['relax']['source'] == 'audio_measurement'
        assert 'relax' not in fits(store, target)
        assert not store.conn.execute('SELECT 1 FROM predictions WHERE model_generation IS NOT NULL').fetchone()

        # Retry eligibility is controlled explicitly, without advancing global
        # clocks or treating the deliberately deferred model as ready.
        store.vibe_predictions.repository.clear_joint_outcomes(vibe_joint_preparation.VIBES)
        drain(store, batch=1).close()
        before = fits(store, target)['relax']
        prediction = store.conn.execute('SELECT v.prediction_id FROM recording_vibes v '
            'JOIN recordings r ON r.id=v.recording_id WHERE r.identity_key=? AND v.vibe=?',
            (target, 'relax')).fetchone()[0]
        assert vibe_models.linked(store.conn, prediction)
        assert vibe_provenance.read(store.conn, prediction)['inputs']
        assert before['score'] > .5
        store.conn.close()
        store = Store(path)
        with monkeypatch.context() as patch:
            patch.setattr(joint, 'fit', lambda *_a, **_k: pytest.fail('Restart refitted the model'))
            store.init_schema()
            patch.setattr(vibe_model_files.VibeModelFiles, 'read', lambda *_a, **_k: pytest.fail('Serving loaded a model'))
            assert fits(store, target)['relax'] == before
            assert store.vibe_predictions.fit_scores([target], 'relax') == {target: before['score']}
        assert joint.fit is original_fit
        capture(store, 'one', calm=False)
        drain(store, batch=1).close()
        assert fits(store, target)['relax']['score'] < before['score']
        assert fits(store, own)['relax']['source'] == 'audio_measurement'
        with store.conn:
            catalog.choose_field(store.conn, tid, 'genre', 'Detroit Techno')
        drain(store, batch=1).close()
        assert fits(store, target)['relax']['source'] == 'artist'
        assert store.conn.execute('SELECT genre FROM catalog_tracks WHERE id=?', (tid,)).fetchone()[0] == 'Detroit Techno'
        with store.conn:
            store.conn.execute('DELETE FROM sample_attributes')
        drain(store, batch=1).close()
        assert 'relax' not in fits(store, target)
        assert not store.conn.execute('SELECT 1 FROM recording_vibes WHERE measurement_id IS NOT NULL').fetchone()
        assert store.conn.execute('SELECT 1 FROM measurements').fetchone()
        assert [tuple(row) for row in store.conn.execute('SELECT * FROM schema_migrations')] == history
        assert not store.conn.execute('PRAGMA foreign_key_check').fetchone()
    finally:
        store.conn.close()


@pytest.mark.parametrize('failure', [WorkBudgetExceeded, fao.FileBudgetExceeded, fao.FileChanged])
def test_ready_joint_vibes_refresh_when_last_channel_defers(prediction_store, monkeypatch, qualified_candidate, failure):
    """One deferred channel must not strand other newly published models."""
    store = prediction_store

    # Keep the real healthy/deferred interaction. Full taxonomy preparation is
    # owned by the ordinary qualified-release case; six unrelated fits add no
    # distinct failure or publication boundary to these three exceptions.
    monkeypatch.setattr(vibe_joint_preparation, 'VIBES', ('relax', vibe_joint_preparation.VIBES[-1]))

    _entry, own = catalog_track(store, 'partial-original')
    _entry, target = catalog_track(store, 'partial-target')
    capture(store, 'partial-original')
    drain(store)

    def generation(key, vibe):
        return store.conn.execute(
            'SELECT hex(p.model_generation) FROM recording_vibes v '
            'JOIN recordings r ON r.id=v.recording_id '
            'JOIN predictions p ON p.id=v.prediction_id '
            'WHERE r.identity_key=? AND v.vibe=?', (key, vibe)).fetchone()[0].lower()

    healthy, deferred = 'relax', vibe_joint_preparation.VIBES[-1]
    previous_own_score = fits(store, own)[healthy]['score']
    previous_healthy = generation(target, healthy)
    previous_deferred = store.vibe_predictions.joint_state(deferred)[0]
    original_fit = joint.fit
    attempts = []

    def defer_last(rows, vibe, **kwargs):
        if vibe == deferred:
            attempts.append(vibe)
            raise failure('controlled single-channel fit deferral')
        return original_fit(rows, vibe, **kwargs)

    monkeypatch.setattr(joint, 'fit', defer_last)
    capture(store, 'partial-original', calm=False)
    drain(store)

    assert attempts == [deferred]
    assert fits(store, own)[healthy]['source'] == 'audio_measurement'
    assert fits(store, own)[healthy]['score'] < previous_own_score
    healthy_state = vibe_joint_preparation.state(store, healthy)
    deferred_state = vibe_joint_preparation.state(store, deferred)
    assert vibe_joint_preparation.current(store, healthy_state)
    assert healthy_state['outcome'] == 'ready'
    assert deferred_state['outcome'] == 'deferred'
    assert deferred_state['retry_at'] > vibe_joint_preparation.time.time()
    assert store.vibe_predictions.joint_state(deferred)[0] == previous_deferred
    current_healthy = store.vibe_predictions.joint_state(healthy)[0]
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
    assert joint_commands.step(store)
    assert attempts == [deferred, deferred]
    assert vibe_joint_preparation.state(store, deferred)['attempts'] == 2
    assert store.vibe_predictions.pending() is None
    assert generation(target, healthy) == current_healthy
    assert generation(target, deferred) == previous_deferred


def test_deferral_and_refresh_queue_roll_back_together(store, monkeypatch):
    from yt_playlist.repos.vibe_predictions import VibePredictionsRepo

    previous = {'outcome': 'deferred', 'retry_at': 0}
    store.vibe_predictions.repository.save_joint_outcome('relax', previous)
    before = [tuple(row) for row in store.conn.execute(
        'SELECT group_key,revision,cursor FROM rec_vibe_pending ORDER BY position')]
    original_queue = VibePredictionsRepo._request_refresh

    def fail_queue(repo, reason):
        original_queue(repo, reason)
        raise RuntimeError('interrupted partial refresh enqueue')

    monkeypatch.setattr(VibePredictionsRepo, '_request_refresh', fail_queue)
    with pytest.raises(RuntimeError, match='interrupted partial refresh enqueue'):
        store.vibe_predictions.defer_joint_model('relax', {**previous, 'retry_at': 60}, True)
    assert vibe_joint_preparation.state(store, 'relax') == previous
    assert [tuple(row) for row in store.conn.execute(
        'SELECT group_key,revision,cursor FROM rec_vibe_pending ORDER BY position')] == before


def test_prediction_queue_retries_after_committed_derived_state_and_restart(
        prediction_input, prediction_schema, monkeypatch, individual_model_rollout, fixed_measurement_version):
    from yt_playlist.repos.vibe_predictions import VibePredictionsRepo

    store = prediction_input
    assert store._snapshot_revisions.conn is store.conn
    assert not store.conn.execute('PRAGMA foreign_key_check').fetchone()
    catalog_track(store, 'one')
    _tid, target = catalog_track(store, 'two')
    capture(store, 'one')
    original = VibePredictionsRepo.publish_prepared
    def fail(repo, *args, **kwargs):
        original(repo, *args, **kwargs)
        raise RuntimeError('interrupted prediction publication')
    with pytest.MonkeyPatch.context() as patch, mock_snapshot_coordinator(store, patch):
        patch.setattr(vibe_joint_preparation, 'qualified', lambda: False)
        patch.setattr(VibePredictionsRepo, 'publish_prepared', fail)
        with pytest.raises(RuntimeError, match='interrupted prediction'):
            Preparation(store).capture(1000)
    committed = {table: [tuple(row) for row in store.conn.execute(f'SELECT * FROM {table} ORDER BY id')]
                 for table in ('measurements', 'predictions')}
    assert committed['measurements']
    assert store.conn.execute('SELECT 1 FROM recording_vibes').fetchone()
    assert store.prepared_content.has_pending()
    assert store.prepared_content.counters()[0] == 0

    path = store._db_path
    store.conn.close()
    reopened = Store(path)
    with closing(reopened.conn), schema_inspection.reuse_tables(reopened.conn):
        reopened.conn.execute('PRAGMA synchronous=OFF')
        with monkeypatch.context() as patch:
            patch.setattr(store_core, 'Revisions', prediction_schema.bind_revisions)
            reopened.init_schema()
        for table, rows in committed.items():
            assert [tuple(row) for row in reopened.conn.execute(f'SELECT * FROM {table} ORDER BY id')] == rows
        assert reopened.prepared_content.has_pending()
        assert reopened.prepared_content.counters()[0] == 0
        with mock_snapshot_coordinator(reopened, monkeypatch):
            drain(reopened, batch=1).close()
        assert 'relax' not in fits(reopened, target)
        assert not reopened.vibe_predictions.pending()
        assert not reopened.prepared_content.has_pending()
        measurements = {tuple(row) for row in reopened.conn.execute('SELECT * FROM measurements')}
        assert set(committed['measurements']) <= measurements


def test_model_version_change_recomputes_all_evidence_in_pages(
        prediction_store, monkeypatch, qualified_candidate, relax_joint_channel):
    store = prediction_store
    catalog_track(store, 'one')
    _tid, target = catalog_track(store, 'two')
    capture(store, 'one')
    drain(store)
    old = fits(store, target)
    monkeypatch.setattr(vibe_preparation.audio_vibe_specialists, 'identity', lambda: 'changed-model')
    assert vibe_preparation.due(store)
    assert vibe_preparation.step(store, batch=1)
    assert store.vibe_predictions.pending()
    drain(store, batch=1)
    refreshed = fits(store, target)
    assert {vibe: (fit['score'], fit['support'], fit['source']) for vibe, fit in refreshed.items()} == {
        vibe: (fit['score'], fit['support'], fit['source']) for vibe, fit in old.items()}
    assert store.conn.execute('SELECT COUNT(DISTINCT recording_id) FROM vibe_recording_fits WHERE instr(inputs_version,?)>0',
                              ('changed-model',)).fetchone()[0] == 2


def test_arithmetic_rollout_preserves_originals_and_reuses_new_identity_after_restart(
        prediction_store, monkeypatch, individual_model_rollout):
    from tests.test_audio_vibe_cruise import samples
    from yt_playlist.rec import audio_vibe_runtime as runtime
    store = prediction_store
    entry, own = catalog_track(store, 'arithmetic-original')
    _entry, target = catalog_track(store, 'arithmetic-target')
    monkeypatch.setenv('YT_PLAYLIST_AUDIO_VIBES', '1')
    from tests.audio_model_support import enable_models
    enable_models(monkeypatch)
    captured = samples('arithmetic-original')
    for row in captured:
        store.audio_vibes.save_sample(row, 100)
    run = captured[0]['run_id']
    store.audio_vibes.save_feedback(run, [row['id'] for row in captured], ['release'], TAXONOMY_VERSION, None, 101)
    feedback = store.audio_vibes.feedback(run)
    identity = runtime.identity
    with monkeypatch.context() as old:
        old.setattr(runtime, 'identity', lambda *args: identity(*args).replace('/' + runtime.ARITHMETIC_VERSION, ''))
        old.setattr(runtime.math, 'sumprod', lambda left, right: sum(a * b for a, b in zip(left, right, strict=True)))
        old_version = vibe_preparation.version()
        drain(store).close()
    original = {tuple(row) for row in store.conn.execute('SELECT * FROM measurements')}
    links = {tuple(row) for row in store.conn.execute('SELECT * FROM measurement_samples')}
    assert original and links
    assert vibe_preparation.version() != old_version
    drain(store, batch=1).close()
    measurements = {tuple(row) for row in store.conn.execute('SELECT * FROM measurements')}
    assert original < measurements
    assert links < {tuple(row) for row in store.conn.execute('SELECT * FROM measurement_samples')}
    assert {row[5] for row in measurements} == {'audio_measurement'}
    current = {row['vibe']: row for row in store.conn.execute('SELECT v.* FROM recording_vibes v JOIN recordings r '
        'ON r.id=v.recording_id WHERE r.identity_key=?', (own,))}
    assert current and all(row['measurement_id'] is not None and row['prediction_id'] is None for row in current.values())
    assert 'cruise' not in fits(store, target)
    assert all(runtime.ARITHMETIC_VERSION in row[0] for row in
               store.conn.execute('SELECT inputs_version FROM vibe_recording_fits'))
    with store.conn:
        content_work.enqueue(store.conn, (own,))
    restarted = Store(store._db_path)
    try:
        restarted.init_schema()
        drain(restarted, batch=1).close()
        assert {tuple(row) for row in restarted.conn.execute('SELECT * FROM measurements')} == measurements
        assert restarted.audio_vibes.feedback(run) == feedback
        for row in samples('arithmetic-original'):
            row['embedding']['values'] = [.4] * 200
            restarted.audio_vibes.save_sample(row, 200)
        drain(restarted, batch=1).close()
        assert measurements < {tuple(row) for row in restarted.conn.execute('SELECT * FROM measurements')}
        with restarted.conn:
            catalog.choose_field(restarted.conn, entry, 'genre', 'Detroit Techno')
        drain(restarted, batch=1).close()
        assert fits(restarted, own)['cruise']['source'] == 'audio_measurement'
        assert original < {tuple(row) for row in restarted.conn.execute('SELECT * FROM measurements')}
        assert links < {tuple(row) for row in restarted.conn.execute('SELECT * FROM measurement_samples')}
        assert restarted.audio_vibes.feedback(run) == feedback
    finally:
        restarted.conn.close()


def test_artist_tags_never_become_recording_measurements_or_propagation_seeds(file_backed_store, monkeypatch):
    store = file_backed_store
    monkeypatch.setattr(vibe_joint_preparation, 'QUALIFICATIONS', {})
    monkeypatch.setattr(joint, 'fit', lambda *a, **k: pytest.fail('Unqualified candidate fit'))
    from yt_playlist.controllers.vibe_predictions import VibePredictions
    monkeypatch.setattr(VibePredictions, 'joint_model', lambda *a, **k: pytest.fail('Unqualified model read'))
    tid, own = track(store, 'one')
    _tid, target = track(store, 'two')
    store.enrichment_state.record_metadata_observations(tid, 'lastfm', [
        MetadataObservation('tags', [{'name': 'Ambient'}], 'artist', 'shared', 'reported',
                            match_evidence={'method': 'title_artist_match'})], run_id='artist-tags', now=1)
    drain(store)
    assert not fits(store, own) and not fits(store, target)
    assert not store.conn.execute('SELECT 1 FROM measurements').fetchone()
    assert not vibe_joint_preparation.due(store)


def test_removed_audio_retracts_predictions_and_leaves_missing_scores_unknown(
        prediction_store, qualified_candidate, relax_joint_channel):
    store = prediction_store
    catalog_track(store, 'one')
    _tid, target = catalog_track(store, 'two')
    capture(store, 'one')
    drain(store)
    assert fits(store, target)['relax']['source'] == 'artist_genre'
    with store.conn:
        store.conn.execute('DELETE FROM sample_attributes')
    drain(store)
    assert 'relax' not in fits(store, target)
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
    from yt_playlist.rec.vibe_score_policy import serving_prefix
    item = {'musical': {'vibes': {'inputs_version': serving_prefix() + 'forceful', 'intensity_fits': {
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


def test_provider_backlog_cannot_starve_vibe_bootstrap_or_dependency_fanout(
        prediction_store, individual_model_rollout):
    store = prediction_store
    # Three rows leave continuing source work after two single-row admissions.
    for index in range(3):
        tid, _key = track(store, f'song{index}')
        store.enrichment_state.record_metadata_observations(tid, 'acousticbrainz', [
            MetadataObservation('mood_relaxed', .9, 'recording', str(index), 'prediction',
                                match_evidence={'method': 'title_artist_match'})], run_id=str(index), now=1)
    worker = Preparation(store, budget=replace(Budget(), batch=1))
    assert worker.capture(1000)  # provider source
    assert worker.capture(1000)  # recordings
    before = store.conn.execute("SELECT cursor FROM rec_vibe_pending WHERE group_key='*'").fetchone()[0]
    # A qualified model cycle may precede fan-out, but cannot return to the
    # continuing provider backlog without delivering the pending vibe work.
    for _ in range(2 * len(vibe_joint_preparation.VIBES) + 4):
        worker.capture(1000)
        row = store.conn.execute("SELECT cursor FROM rec_vibe_pending WHERE group_key='*'").fetchone()
        advanced = row is None or row[0] > before
        if advanced:
            break
    worker.close()
    assert advanced
    assert store.musical_profiles.pending()


@pytest.mark.parametrize('originals', [0, 3])
@pytest.mark.parametrize('qualified', [False, True])
def test_first_new_score_and_reopened_home_do_not_require_joint_fit(
        file_backed_store, monkeypatch, originals, qualified):
    from fastapi.testclient import TestClient

    from tests.musical_support import record_acousticbrainz
    from yt_playlist.core import vibe_model_files
    from yt_playlist.rec import acousticbrainz_vibes
    from yt_playlist.web.app import create_app

    store = file_backed_store
    monkeypatch.setattr(vibe_joint_preparation, 'QUALIFICATIONS', {})
    for index in range(originals):
        track(store, f'startup-original-{index}')
        capture(store, f'startup-original-{index}')
    drain(store).close()
    if qualified:
        monkeypatch.setattr(vibe_joint_preparation, 'QUALIFICATIONS',
            {vibe_joint_preparation.config_identity(): 'controlled-first-output-fixture'})

    def forbidden(*args, **kwargs):
        pytest.fail('First usable output or a reopened Home request required a joint model')

    monkeypatch.setattr(joint, 'fit', forbidden)
    monkeypatch.setattr(vibe_model_files.VibeModelFiles, 'read', forbidden)
    entry, target = track(store, 'startup-new-target')
    record_acousticbrainz(store, entry, dict.fromkeys(acousticbrainz_vibes.FIELDS, .5))
    assert not store.vibe_predictions.fit_scores([target], 'relax')
    worker = Preparation(store)
    try:
        for _ in range(20):
            worker.capture(1000)
            cached = store.vibe_predictions.fit_scores([target], 'relax')
            if cached:
                break
        else:
            pytest.fail('First recording output never became usable')
    finally:
        worker.close()
    assert 0 <= cached[target] <= 1
    assert fits(store, target)['relax']['source'] == acousticbrainz_vibes.SOURCE
    assert not any(vibe_joint_preparation.state(store, vibe) for vibe in vibe_joint_preparation.VIBES)

    reopened = Store(store._db_path)
    try:
        reopened.init_schema()
        assert reopened.vibe_predictions.fit_scores([target], 'relax') == cached
        # Exercise all ordinary Home routers and HTTP handler threads. No
        # readiness override or prebuilt menu makes a fit appear unnecessary.
        with TestClient(create_app(reopened, lambda: {}, background_work=False),
                        base_url='http://127.0.0.1') as client:
            assert client.get('/').status_code == 200
        assert reopened.vibe_predictions.fit_scores([target], 'relax') == cached
    finally:
        reopened.conn.close()


@pytest.mark.parametrize('last_deferred', [False, True])
def test_continuing_evidence_publishes_and_scores_across_worker_restart(
        file_backed_store, monkeypatch, last_deferred):
    store = file_backed_store
    monkeypatch.setattr(vibe_joint_preparation, 'QUALIFICATIONS', {})
    track(store, 'cycle-original')
    _entry, target = track(store, 'cycle-target')
    capture(store, 'cycle-original')
    drain(store)
    monkeypatch.setattr(vibe_joint_preparation, 'QUALIFICATIONS',
        {vibe_joint_preparation.config_identity(): 'controlled-backlog-fixture'})
    monkeypatch.setattr(store.prepared_content, 'has_pending', lambda: True)
    monkeypatch.setattr(store.musical_profiles, 'pending', lambda: True)
    source_turns, fits_seen = [], []
    clock = [time.time()]
    monkeypatch.setattr(vibe_joint_preparation.time, 'time', lambda: clock[0])
    original_fit = joint.fit

    def evidence_changed():
        with store.conn:
            store.conn.execute('UPDATE rec_vibe_pending SET evidence_revision=evidence_revision+1 WHERE position=0')

    def source_step(*args, **kwargs):
        source_turns.append(True)
        evidence_changed()
        return True

    def fit(rows, vibe, **kwargs):
        fits_seen.append(vibe)
        evidence_changed()
        if last_deferred and vibe == vibe_joint_preparation.VIBES[-1]:
            raise WorkBudgetExceeded('Controlled final-channel deferral during a continuing backlog')
        return original_fit(rows, vibe, **kwargs)

    monkeypatch.setattr(musical_preparation, 'source_step', source_step)
    monkeypatch.setattr(joint, 'fit', fit)
    worker = Preparation(store)
    for _ in range(16):
        worker.capture(1000)
        if fits(store, target).get('relax', {}).get('source') == 'artist_genre':
            break
    else:
        pytest.fail('The first compatible fit did not score under continuing evidence writes')
    assert 0 < len(fits_seen) < len(vibe_joint_preparation.VIBES)
    assert not vibe_joint_preparation.current(store, vibe_joint_preparation.state(store, 'relax'))
    assert vibe_joint_preparation.posterior(store, 'relax') is not None
    source_before = len(source_turns)
    worker.close()
    worker = Preparation(store)
    for _ in range(len(vibe_joint_preparation.VIBES) * 6):
        count = len(fits_seen)
        worker.capture(1000)
        assert len(fits_seen) - count <= 1
        if len(fits_seen) == len(vibe_joint_preparation.VIBES):
            break
    assert len(source_turns) > source_before
    assert fits_seen == list(vibe_joint_preparation.VIBES)
    assert all(vibe_joint_preparation.state(store, vibe)['outcome'] ==
               ('deferred' if last_deferred and vibe == vibe_joint_preparation.VIBES[-1] else 'ready')
               for vibe in vibe_joint_preparation.VIBES)
    worker.close()
    heads = {v: store.vibe_predictions.joint_state(v)[0]
             for v in vibe_joint_preparation.VIBES}
    assert store.vibe_predictions.has_joint_scores([head for head in heads.values() if head])
    assert not vibe_joint_preparation.admitted(store, waiting=True)
    assert vibe_joint_preparation.admitted(store, waiting=False)
    clock[0] += 61
    assert vibe_joint_preparation.admitted(store, waiting=True)


@pytest.mark.parametrize('target_changed', [False, True], ids=['same-target', 'corrected-target'])
def test_retained_prediction_requires_current_conditioning(
        uncontended_prediction_store, qualified_candidate, relax_joint_channel,
        fixed_measurement_version, monkeypatch, target_changed):
    store = uncontended_prediction_store
    catalog_track(store, 'retained-original')
    entry, target = catalog_track(store, 'retained-target')
    capture(store, 'retained-original')
    drain(store).close()

    def prediction_id():
        row = store.conn.execute('SELECT v.prediction_id FROM recording_vibes v '
            'JOIN recordings r ON r.id=v.recording_id WHERE r.identity_key=? AND v.vibe=?',
            (target, 'relax')).fetchone()
        return row[0] if row else None

    previous = prediction_id()
    assert previous and fits(store, target)['relax']['source'] == 'artist_genre'
    receipt = vibe_provenance.read(store.conn, previous)
    monkeypatch.setattr(vibe_joint_preparation, 'posterior', lambda _reader, _vibe: None)
    with store.conn:
        if target_changed:
            catalog.choose_field(store.conn, entry, 'genre', 'Detroit Techno')
        else:
            content_work.enqueue(store.conn, (target,))
    drain(store).close()

    assert prediction_id() == (None if target_changed else previous)
    assert fits(store, target).get('relax', {}).get('source') == (None if target_changed else 'artist_genre')
    assert vibe_provenance.read(store.conn, previous) == receipt


@pytest.mark.parametrize('policy', ('measurement', 'masked_profile'))
def test_policy_rollover_replaces_serving_scores_and_preserves_history(
        uncontended_prediction_store, qualified_candidate, relax_joint_channel, monkeypatch, policy):
    from yt_playlist.rec import audio_vibe_prediction, musical_profiles

    store = uncontended_prediction_store
    _original_entry, original = catalog_track(store, 'policy-original')
    _entry, target = catalog_track(store, 'policy-target')
    capture(store, 'policy-original')
    drain(store).close()

    keys = [target]
    before = store.vibe_predictions.retained_joint_fits(keys)[target]['relax']
    receipt = vibe_provenance.read(store.conn, before['prediction_id'])
    assert receipt['inputs']
    inputs = store.vibe_predictions.joint_inputs('relax', 32)
    original_scores = store.vibe_predictions.fit_scores([original], 'relax')
    generation, _posterior = vibe_joint_preparation.posterior(store, 'relax')
    assert inputs and original_scores
    assert home_vibe.affinities(store, keys, 'relax')[target] == before['score']

    if policy == 'measurement':
        monkeypatch.setattr(audio_vibe_prediction, 'VERSION', audio_vibe_prediction.VERSION + '-replacement')
    else:
        monkeypatch.setattr(musical_profiles, 'MASKED_VERSION', musical_profiles.MASKED_VERSION + '-replacement')
        monkeypatch.setattr(vibe_joint_preparation, 'QUALIFICATIONS',
                            {vibe_joint_preparation.config_identity(): 'controlled-replacement'})
    assert vibe_joint_preparation.posterior(store, 'relax') is None
    assert store.vibe_predictions.joint_inputs('relax', 32) == (inputs if policy == 'masked_profile' else [])
    assert store.vibe_predictions.fit_scores([original], 'relax') == (original_scores if policy == 'masked_profile' else {})
    assert not store.vibe_predictions.retained_joint_fits(keys).get(target)
    assert not store.vibe_predictions.fit_scores(keys, 'relax')
    assert 'relax' not in store.musical_profiles.readiness_state(keys)[1][target]['vibes']['fits']
    assert home_vibe.affinities(store, keys, 'relax')[target] == .5

    drain(store).close()
    replacement, _posterior = vibe_joint_preparation.posterior(store, 'relax')
    assert replacement != generation
    after = store.vibe_predictions.retained_joint_fits(keys)[target]['relax']
    assert after['prediction_id'] != before['prediction_id']
    assert home_vibe.affinities(store, keys, 'relax')[target] == after['score']
    assert vibe_provenance.read(store.conn, before['prediction_id']) == receipt


def test_relationship_rollover_retires_unsupported_scores_and_preserves_history(
        uncontended_prediction_store, qualified_candidate, relax_joint_channel, fixed_measurement_version, monkeypatch):
    from yt_playlist.core.audio_vibe_contract import JOINT_AUDIO_POLICY
    from yt_playlist.rec import vibe_score_versions

    store = uncontended_prediction_store
    _entry, original = catalog_track(store, 'relationship-original')
    _entry, related = catalog_track(store, 'relationship-related')
    _entry, unrelated = catalog_track(store, 'relationship-unrelated', None, 'Unrelated artist')
    capture(store, 'relationship-original')
    keys = [related, unrelated]
    current_values = vibe_recording_rules.posterior_values

    def legacy_values(posterior, eligible, recording_ids, tracks, albums, masked, vibe):
        values = current_values(posterior, eligible, recording_ids, tracks, albums, masked, vibe)
        if unrelated in eligible:
            track = tracks[unrelated]
            conditioning = vibe_recording_rules.conditioning(track, albums.get(unrelated), masked[unrelated], vibe)
            values[unrelated] = posterior.predict(json.loads(track['artist']), recording=recording_ids[unrelated],
                                                  **conditioning)
        return values

    with monkeypatch.context() as legacy:
        legacy.setattr(vibe_score_versions, 'joint_prediction_policy', lambda: JOINT_AUDIO_POLICY)
        legacy.setattr(vibe_recording_rules, 'posterior_values', legacy_values)
        drain(store).close()
        before = store.vibe_predictions.retained_joint_fits(keys)
        assert before[unrelated]['relax']['source'] == 'artist'
        generation, _posterior = vibe_joint_preparation.posterior(store, 'relax')

    original_scores = store.vibe_predictions.fit_scores([original], 'relax')
    assert original_scores
    historical = [before[key]['relax']['prediction_id'] for key in keys]
    receipts = [vibe_provenance.read(store.conn, pid) for pid in historical]
    assert all(receipt['inputs'] for receipt in receipts)
    assert not store.vibe_predictions.fit_scores(keys, 'relax')
    assert not store.vibe_predictions.retained_joint_fits(keys)
    readiness = store.musical_profiles.readiness_state(keys)[1]
    assert all('relax' not in readiness[key]['vibes']['fits'] for key in keys)

    drain(store).close()
    after = store.vibe_predictions.retained_joint_fits(keys)
    assert set(after) == {related}
    assert after[related]['relax']['prediction_id'] != historical[0]
    replacement, _posterior = vibe_joint_preparation.posterior(store, 'relax')
    assert replacement == generation
    assert store.vibe_predictions.fit_scores([original], 'relax') == original_scores
    assert store.conn.execute('SELECT 1 FROM recording_vibes WHERE prediction_id=?', (historical[1],)).fetchone() is None
    assert [vibe_provenance.read(store.conn, pid) for pid in historical] == receipts


@pytest.mark.parametrize('interruption', ['complete', 'source-update', 'competing-head', 'policy-change', 'shutdown'])
def test_joint_input_batch_finishes_without_deadline_restarts_and_releases_readers(
        file_backed_store, monkeypatch, caplog, interruption):
    from yt_playlist.core import file_collection
    from yt_playlist.core import activity
    from yt_playlist.rec import audio_vibe_specialists
    from types import SimpleNamespace
    from yt_playlist.core import vibe_models

    store = file_backed_store
    monkeypatch.setattr(vibe_joint_preparation, 'QUALIFICATIONS', {})
    entry, _key = track(store, 'batched-original')
    track(store, 'batched-target')
    capture(store, 'batched-original')
    drain(store)
    monkeypatch.setattr(vibe_joint_preparation, 'QUALIFICATIONS',
        {vibe_joint_preparation.config_identity(): 'controlled-input-batches'})
    # Keep a valid prior generation while a changed revision is prepared.
    for _ in vibe_joint_preparation.VIBES:
        assert joint_commands.step(store)
    heads = {vibe: store.vibe_predictions.joint_state(vibe)[0]
             for vibe in vibe_joint_preparation.VIBES}
    revision = int(store.vibe_predictions.joint_revision())
    with store.conn:
        store.conn.execute('UPDATE rec_vibe_pending SET evidence_revision=? WHERE position=0', (revision + 1,))
    worker = Preparation(store)
    original = vibe_joint_preparation.prior_inputs
    clock, stopping, batches = [100.], [False], []
    monkeypatch.setattr(activity, 'time', SimpleNamespace(monotonic=lambda: clock[0]))

    def inputs(reader, rows, vibe):
        batches.append([row['recording'] for row in rows])
        result = original(reader, rows, vibe)
        clock[0] += worker.budget.capture_seconds + worker.budget.fit_seconds + 1
        if interruption == 'source-update':
            with store.conn:
                catalog.choose_field(store.conn, entry, 'genre', 'Detroit Techno')
        if interruption == 'competing-head':
            store.vibe_predictions.repository.save_joint_outcome(vibe, {})
        if interruption == 'policy-change':
            monkeypatch.setattr(audio_vibe_specialists, 'identity', lambda: 'replacement-policy')
        stopping[0] = interruption == 'shutdown'
        return result

    monkeypatch.setattr(vibe_joint_preparation, 'prior_inputs', inputs)
    if interruption == 'shutdown':
        with pytest.raises(activity.WorkClosing), activity.cancellable_work(lambda: stopping[0]):
            worker._joint_step()
    else:
        with caplog.at_level('DEBUG', logger='yt_playlist.library.vibe_composition'):
            assert worker._joint_step() is (interruption in ('complete', 'source-update'))
    assert batches and len([key for batch in batches for key in batch]) == len(
        {key for batch in batches for key in batch})
    assert worker._capturing is None and worker._capturing_kind is None
    current = {vibe: store.vibe_predictions.joint_state(vibe)[0]
               for vibe in vibe_joint_preparation.VIBES}
    if interruption in ('complete', 'source-update'):
        outcome = vibe_joint_preparation.state(store, vibe_joint_preparation.VIBES[0])
        assert outcome['revision'] == str(revision + 1) and outcome['outcome'] == 'ready'
    else:
        assert heads == current
        rejection = {'competing-head': 'changed-vibe-head', 'policy-change': 'changed-vibe-policy'}.get(interruption)
        if rejection:
            assert 'reason=' + rejection in caplog.text
    with file_collection.collector(vibe_models.directory(store.conn)) as admitted:
        assert admitted
    assert vibe_joint_preparation.due(store)


@pytest.mark.parametrize('interruption', ['complete', 'stale', 'shutdown'])
def test_joint_score_batch_finishes_channels_once_and_preserves_publication_guards(
        file_backed_store, qualified_candidate, monkeypatch, interruption):
    import weakref
    from yt_playlist.core import file_collection
    from yt_playlist.core import activity
    from types import SimpleNamespace
    from yt_playlist.core import vibe_models

    store = file_backed_store
    track(store, 'sliced-original')
    entry, target = track(store, 'sliced-target')
    capture(store, 'sliced-original')
    drain(store)
    before = [tuple(row) for row in store.conn.execute(
        'SELECT * FROM recording_vibes WHERE recording_id='
        '(SELECT id FROM recordings WHERE identity_key=?) ORDER BY vibe', (target,))]
    with store.conn:
        content_work.enqueue(store.conn, (target,))
    worker = Preparation(store)
    posteriors, channels, original = [], [], vibe_joint_preparation.posterior
    clock, stopping = [100.], [False]
    monkeypatch.setattr(activity, 'time', SimpleNamespace(monotonic=lambda: clock[0]))

    def load(reader, vibe):
        assert not any(reference() is not None for reference in posteriors)
        prepared = original(reader, vibe)
        if prepared:
            posteriors.append(weakref.ref(prepared[1]))
        channels.append(vibe)
        clock[0] += worker.budget.fit_seconds + 1
        if len(channels) == 1 and interruption == 'stale':
            with store.conn:
                catalog.choose_field(store.conn, entry, 'genre', 'Detroit Techno')
        stopping[0] = interruption == 'shutdown'
        return prepared

    monkeypatch.setattr(vibe_joint_preparation, 'posterior', load)
    def complete_batch():
        for _ in range(5):
            worker.capture(1000)
            if posteriors:
                break

    if interruption == 'shutdown':
        with pytest.raises(activity.WorkClosing), activity.cancellable_work(lambda: stopping[0]):
            complete_batch()
    else:
        complete_batch()
    assert channels and len(channels) == len(set(channels))
    if interruption == 'complete':
        assert channels == list(vibe_joint_preparation.VIBES)
    assert all(reference() is None for reference in posteriors)
    assert worker._capturing is None
    after = [tuple(row) for row in store.conn.execute(
        'SELECT * FROM recording_vibes WHERE recording_id='
        '(SELECT id FROM recordings WHERE identity_key=?) ORDER BY vibe', (target,))]
    if interruption != 'complete':
        assert before == after
    assert store.prepared_content.has_pending() is (interruption != 'complete')
    with file_collection.collector(vibe_models.directory(store.conn)) as admitted:
        assert admitted
    stopping[0] = False
    monkeypatch.setattr(vibe_joint_preparation, 'posterior', original)
    # Changed inputs and shutdown retain the durable queue and the last-good
    # score set. A new admission can publish without retaining dense posteriors.
    worker = Preparation(store)
    for _ in range(100):
        if (not store.prepared_content.has_pending() and not musical_preparation.due(store)
                and not vibe_preparation.due(store)):
            break
        worker.capture(1000)
    else:
        pytest.fail('Interrupted score preparation did not converge')
    assert not store.prepared_content.has_pending()
    assert all(reference() is None for reference in posteriors)
    assert fits(store, target)['relax']['source'] == ('artist' if interruption == 'stale' else 'artist_genre')
    worker.close()
