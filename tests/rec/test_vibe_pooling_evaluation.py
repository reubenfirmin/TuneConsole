"""Compare frozen heldouts with actual preparation before target observations exist."""
from copy import deepcopy
from contextlib import closing
import hashlib
import json
from types import SimpleNamespace

import pytest

from experiments.audio_lab_tools.pooling import vibe_pooling_evaluation as evaluation
from experiments.audio_lab_tools.pooling import vibe_pooling_labels as categorical
from tests.conftest import seed_catalog_track
from tests.rec.test_musical_preparation import observe
from tests.rec.test_vibe_prediction import track
from tests.recommendation_support import seed_publication_subjects
from tests.store_fixture_support import bounded_consumer_schema, initialize_consumer, mock_snapshot_coordinator
from yt_playlist.controllers.vibe_evaluation import VibeEvaluation
from yt_playlist.core import catalog, db_handles, file_collection, profile_files
from yt_playlist.core.audio_vibe_contract import TAXONOMY_VERSION
from yt_playlist.core.activity import WorkBudgetExceeded
from yt_playlist.providers.base import MetadataObservation
from yt_playlist.rec import home_vibe, musical_preparation, musical_profiles, vibe_joint_preparation
from yt_playlist.rec import vibe_prediction, vibe_preparation
from yt_playlist.rec import vibe_evaluation


@pytest.fixture(scope='module')
def _pristine_current_schema():
    # Capture owns source filtering, immutable files and reader lifetimes. Its
    # controlled catalog needs these three genres, rather than full taxonomy
    # admission. Capture below owns its real read snapshot and file leases;
    # successful source preparation is a lower collaborator.
    with bounded_consumer_schema((('psybient', 'Psybient'), ('ambient', 'Ambient'),
                                  ('doom-metal', 'Doom Metal')), snapshot_guards=False) as schema:
        yield schema


@pytest.fixture
def file_backed_store(tmp_path, _pristine_current_schema, monkeypatch):
    import sqlite3

    from yt_playlist.core.db_diagnostics import DiagnosticConnection
    from yt_playlist.core.store import Store

    # Per-statement diagnostics/admission are below this synchronous private
    # capture. Native SQLite still owns SQL, constraints and transactions.
    for operation in ('execute', 'executemany'):
        monkeypatch.setattr(DiagnosticConnection, operation, getattr(sqlite3.Connection, operation))
    database = Store(tmp_path / 'masked-capture.db')
    with closing(database.conn):
        database.conn.execute('PRAGMA synchronous=OFF')
        initialize_consumer(database, _pristine_current_schema, snapshot_guards=False)
        database.conn.execute('PRAGMA wal_autocheckpoint=0')
        yield database


def prepare_source(store, monkeypatch):
    # Release the completed lower preparation reader before capture. Capture
    # itself must acquire and release its real database/file leases.
    with monkeypatch.context() as patch, mock_snapshot_coordinator(store, patch):
        return musical_preparation.source_step(store, 1000)


def bucket(value):
    return int(hashlib.sha256(str(value).encode()).hexdigest(), 16) % 5


def comparison_inputs(scope, stratum):
    """Three detached originals; actual runtime math consumes these lower inputs."""
    artist, recording = 11, 1
    fold = bucket(recording if stratum == 'known' else 'artist:' + str(artist))
    donors = []
    for identity in range(2, 30):
        credit = artist if stratum == 'known' else 20 + identity
        if bucket(identity if stratum == 'known' else 'artist:' + str(credit)) != fold:
            donors.append((identity, credit))
        if len(donors) == 2:
            break
    source = musical_profiles.source_profile({
        'provider': 'fixture', 'scope': scope, 'entity_id': 'allowed-description',
        'observations': {'tags': {'value': [{'name': 'Ambient'}],
            'provenance': {'method': 'title_artist_match', 'kind': 'reported'}}}})
    content = {'library': {'genre': 'Psybient', 'audio': {}}, 'discovered': None}
    profiles = {str(identity): {'content': content, 'sources': [source] if identity == recording else [],
                               'overrides': {}, 'truncated': False}
                for identity in [recording, *(identity for identity, _ in donors)]}
    rows = {vibe: [
        {'recording': identity, 'measurement': identity, 'artists': [credit],
         'genre': 'psybient' if stratum == 'known' or identity == recording else 'doom metal',
         'album': None, 'source': 'audio_measurement/fixture', 'score': .12 + .04 * offset,
         'support': 1., 'input_digest': str(identity), 'version': 'fixture'}
        for offset, (identity, credit) in enumerate([(recording, artist), *donors])]
        for vibe in home_vibe.PRESETS}
    return recording, artist, fold, rows, profiles


def runtime_comparison(rows, profiles, recording, artist, monkeypatch):
    # Keep runtime profile preparation, cohort resolution and joint prediction
    # real. SQL publication/restart belongs to test_vibe_prediction; this owner
    # compares the offline calculation with that exact runtime calculation.
    key = 'heldout|target artist'
    profile = profiles[str(recording)]
    raw = json.dumps(profile['content'])
    page = [(key, 0, raw, None, True)]
    source_inputs = {key: (profile['sources'], profile['overrides'], False)}
    cohorts = {}
    for vibe, originals in rows.items():
        for row in originals:
            if row['recording'] == recording:
                continue
            evidence = {'fits': {vibe: {'score': row['score'], 'support': row['support']}}}
            for group in vibe_prediction.groups(json.dumps(row['artists']), row['genre']):
                cohort = cohorts.setdefault(group, {'stats': {}, 'inputs': {}})
                cohort['stats'] = vibe_prediction.adjust(cohort['stats'], evidence, 1)
                cohort['inputs'].setdefault(vibe, {})[str(row['recording'])] = row['measurement']
    reader = SimpleNamespace(
        musical_profiles=SimpleNamespace(recording_inputs=lambda *_args, **_kwargs: deepcopy(source_inputs)),
        genres=SimpleNamespace(feature_axes=lambda _labels: {'Psybient': ('electronic', 'psybient')}),
        vibe_predictions=SimpleNamespace(
            inputs=lambda _keys: {key: {'recording': recording, 'artist': json.dumps([artist]),
                                       'samples': [], 'metadata': {}, 'old': None}},
            groups=lambda keys: deepcopy({group: cohorts[group] for group in keys if group in cohorts}),
            retained_joint_fits=lambda _keys: {},
            joint_albums=lambda _keys: {}))
    musical = musical_preparation.recordings(reader, page, inputs=source_inputs)
    monkeypatch.setattr(vibe_joint_preparation, 'QUALIFICATIONS', {})
    monkeypatch.setattr(vibe_joint_preparation, 'posterior', lambda *_args: None)
    incumbent, _ = vibe_preparation.recordings(reader, page, musical, source_inputs=source_inputs)
    old = json.loads(incumbent[0][1])['vibes']['fits']
    posteriors = {}
    for vibe, originals in rows.items():
        training = [{**row, 'metadata_prior': home_vibe.heuristic_affinity(
            evaluation.masked_profile(profiles[str(row['recording'])]), vibe)}
            for row in originals if row['recording'] != recording]
        posteriors[vibe] = evaluation.joint.Posterior(evaluation.joint.fit(training, vibe))
    monkeypatch.setattr(vibe_joint_preparation, 'QUALIFICATIONS',
                        {vibe_joint_preparation.config_identity(): 'controlled-evaluation-fixture'})
    monkeypatch.setattr(vibe_joint_preparation, 'posterior', lambda _reader, vibe:
                        ('controlled-generation', posteriors[vibe]) if vibe in posteriors else None)
    candidate, _ = vibe_preparation.recordings(reader, page, musical, source_inputs=source_inputs)
    return old, json.loads(candidate[0][1])['vibes']['fits']


@pytest.mark.parametrize('scope,stratum', [
    pytest.param('artist', 'unseen', id='unseen-artist'),
    pytest.param('release', 'known', id='known-release'),
])
def test_frozen_masked_comparison_matches_runtime_and_ignores_target_observations(
        tmp_path, monkeypatch, scope, stratum):
    recording, artist, fold, rows, profiles = comparison_inputs(scope, stratum)
    old, candidate = runtime_comparison(rows, profiles, recording, artist, monkeypatch)
    assert old['relax']['source'] == ('artist_genre' if stratum == 'known' else 'heuristic')
    assert candidate['relax']['source'] == ('artist_genre' if stratum == 'known' else 'artist')
    assert all(fit['generation'] == 'controlled-generation' and
               fit['new_track_variance'] > fit['mean_variance'] > 0 for fit in candidate.values())
    # Snapshot capture has its real SQL/file owners below. Only its completed
    # lower output is supplied here. Freeze and validate each distinct plan,
    # then reuse that loaded input beneath all six real heldout fits, including
    # both opposite target measurements.
    snapshot = {'rows': rows, 'profile_inputs': profiles}
    monkeypatch.setattr(vibe_evaluation, 'evaluation_snapshot', lambda *_args: deepcopy(snapshot))
    scores, plans = [], []
    for turn, truth in enumerate((.01, .99)):
        for values in rows.values():
            values[0]['score'] = truth
        path = tmp_path / f'plan-{turn}.json'
        evaluation.freeze('controlled-snapshot.db', path)
        plan = evaluation.load_plan(path)
        plans.append(plan)
        assert plan['assignments'][str(recording)][stratum] == fold
        actual = {}
        for vibe in home_vibe.PRESETS:
            output = tmp_path / f'{turn}-{vibe}.json'
            evaluation.run(path, output, stratum, fold, vibe,
                           plan_loader=lambda _path: deepcopy(plan))
            result = json.loads(output.read_text())
            assert result['outcome'] == 'complete'
            row, = result['rows']
            assert row['recording'] == recording and result['training_recordings'] == 2
            assert row['truth'] == truth
            assert row['baseline'] == pytest.approx(old[vibe]['score'])
            assert row['candidate'] == pytest.approx(candidate[vibe]['score'])
            actual[vibe] = {name: row[name] for name in ('baseline', 'candidate', 'interval90')}
        scores.append(actual)
    assert plans[0]['profile_inputs'] == plans[1]['profile_inputs']
    assert scores[0] == scores[1]
    assert plans[0]['rows']['relax'][0]['score'] != plans[1]['rows']['relax'][0]['score']


def test_source_profile_budget_deferral_preserves_pending_and_retries(file_backed_store, monkeypatch):
    store = file_backed_store
    target = seed_catalog_track(store, 'heldout|target artist', 'heldout', 'Target artist', video_id='heldout')
    observe(store, target, [{'name': 'Ambient'}], scope='artist', entity='allowed-description')
    pending = store.musical_profiles.pending()
    assert pending is not None
    source_profile = musical_preparation.profiles.source_profile
    deferred = False
    def defer_once(source):
        nonlocal deferred
        if not deferred:
            deferred = True
            raise WorkBudgetExceeded('Controlled source slice deferral')
        return source_profile(source)
    with monkeypatch.context() as patch:
        patch.setattr(musical_preparation.profiles, 'source_profile', defer_once)
        assert not prepare_source(store, monkeypatch)
    assert deferred and store.musical_profiles.pending() == pending
    assert prepare_source(store, monkeypatch)
    assert store.musical_profiles.pending() is None
    profile = profile_files.read(store.conn, 'source', pending)
    assert json.loads(profile)['scope'] == 'artist'


@pytest.fixture
def masked_source(file_backed_store):
    store = file_backed_store
    key = 'heldout|target artist'
    # Capture consumes catalog identities rather than provider ingestion. Keep
    # both uploads and their actual association on native SQL below capture.
    seed_publication_subjects(store, [
        {'key': key, 'title': 'heldout', 'artist': 'Target artist',
         'video_id': 'heldout', 'genre': 'Psybient'},
        {'key': 'heldout-upload|target artist', 'title': 'heldout-upload',
         'artist': 'Target artist', 'video_id': 'heldout-upload', 'genre': 'Psybient'},
    ])
    entries = dict(store.conn.execute('SELECT video_id,id FROM catalog_entries'))
    target, alternate = entries['heldout'], entries['heldout-upload']
    recording, = store.conn.execute('SELECT recording_id FROM catalog_entries WHERE id=?', (target,)).fetchone()
    with store.conn:
        catalog.associate_item(store.conn, 'heldout-upload', recording, source='verified',
                               evidence={'same': True}, now=1, same_recording=True)
    return store, target, alternate, recording, key


@pytest.mark.parametrize('scope', ['artist', 'release'])
def test_masked_capture_filters_before_source_slots_and_never_reads_target_files(
        masked_source, monkeypatch, scope):
    store, target, alternate, recording, key = masked_source
    observe(store, target, [{'name': 'Ambient'}], scope=scope, entity='allowed-description')
    assert prepare_source(store, monkeypatch)
    before = vibe_evaluation.evaluation_profile_inputs(store.conn, [recording])[str(recording)]
    assert [source['scope'] for source in before['sources']] == [scope]
    assert before['truncated'] is False
    # Slot saturation needs source identities, not 64 duplicate delivery
    # histories. The two native providers below retain real ingestion/changes.
    blocked = [f'000-blocked-{index:02d}' for index in range(64)]
    with store.conn:
        store.conn.executemany("INSERT INTO rec_musical_sources "
            "(source_key,provider,scope,entity_id) VALUES (?,'fixture','recording','heldout-native')",
            [(source,) for source in blocked])
        store.conn.executemany('INSERT INTO rec_musical_links VALUES (?,?,?)',
                               [(source, recording, '') for source in blocked])
    size, read = profile_files.size, profile_files.read
    def allowed_source(source):
        assert store.conn.execute('SELECT scope FROM rec_musical_sources WHERE source_key=?',
                                  (source,)).fetchone()[0] == scope
    def masked_size(conn, kind, source):
        assert kind == 'source'
        allowed_source(source)
        return size(conn, kind, source)
    def masked_read(conn, kind, source, **options):
        assert kind == 'source'
        allowed_source(source)
        return read(conn, kind, source, **options)
    monkeypatch.setattr(profile_files, 'size', masked_size)
    monkeypatch.setattr(profile_files, 'read', masked_read)
    for turn, calm in enumerate((False, True)):
        # Leave the 66 disallowed source bodies unprepared. Reading even one
        # would fail: scope admission must precede file reads and source slots.
        with store._lock:
            for provider in ('first-provider', 'other-provider'):
                store.enrichment_state.record_metadata_observations(alternate, provider, [
                    MetadataObservation('mood_relaxed', .99 if calm else .01, 'recording',
                        'heldout-native', 'prediction', match_evidence={'method': 'title_artist_match'}),
                    MetadataObservation('mood_aggressive', .01 if calm else .99, 'recording',
                        'heldout-native', 'prediction', match_evidence={'method': 'title_artist_match'}),
                ], run_id='mutation-' + str(turn), now=2000 + turn)
            with store.conn:
                for entry in (target, alternate):
                    for field, value in (('energy', .01 if calm else .99), ('bpm', 60 if calm else 240),
                                         ('mood_relaxed', .99 if calm else .01)):
                        catalog.choose_field(store.conn, entry, field, value, now=2000 + turn)
        _references, _choices, truncated = store.musical_profiles.repository.recording_facts([key])[key]
        assert _references == [] and truncated is True
        # Enforce read-only capture, including attempted writes which a final
        # dump could miss after rollback, without serializing the whole schema.
        changes = store.conn.total_changes
        store.conn.execute('PRAGMA query_only=ON')
        try:
            assert vibe_evaluation.evaluation_profile_inputs(store.conn, [recording])[str(recording)] == before
        finally:
            store.conn.execute('PRAGMA query_only=OFF')
        assert store.conn.total_changes == changes
    assert store.conn.execute('SELECT recording_id FROM catalog_entries WHERE id=?',
                              (alternate,)).fetchone()[0] == recording


def test_masked_capture_rejects_relational_overflow_without_reading_source_files(masked_source, tmp_path, monkeypatch):
    store, _target, _alternate, recording, _key = masked_source
    overflow = [f'overflow-{index}' for index in range(4097)]
    with store.conn:
        store.conn.executemany('INSERT INTO rec_musical_sources '
            "(source_key,provider,scope,entity_id) VALUES (?,'fixture','recording','overflow')",
            [(source,) for source in overflow])
        store.conn.executemany('INSERT INTO rec_musical_links VALUES (?,?,?)',
                               [(source, recording, '') for source in overflow])
    # Capture owns SQL and FAO reads; the completed training manifest is a
    # separate lower input, independent of the link-overflow admission rule.
    monkeypatch.setattr(VibeEvaluation, 'training_manifest', lambda *_args, **_kwargs:
                        [{'recording': recording}])
    monkeypatch.setattr(profile_files, 'read', lambda *_args, **_kwargs:
                        pytest.fail('Overflow capture read an unprepared source body'))
    # Freeze opens its own database connection. Any committed write through
    # that connection changes data_version, even a later commit restoring the
    # old rows. Own-connection writes are caught by total_changes as well.
    state = (store.conn.execute('PRAGMA main.data_version').fetchone()[0], store.conn.total_changes)
    rejected = tmp_path / 'overflow-plan.json'
    with pytest.raises(WorkBudgetExceeded, match='source links exceed bounded admission'):
        evaluation.freeze(store._db_path, rejected)
    assert not rejected.exists()
    assert (store.conn.execute('PRAGMA main.data_version').fetchone()[0], store.conn.total_changes) == state


def test_masked_snapshot_holds_database_and_file_lifetimes_until_capture_finishes(masked_source, monkeypatch):
    store, target, _alternate, recording, _key = masked_source
    observe(store, target, [{'name': 'Ambient'}], scope='artist', entity='allowed-description')
    assert prepare_source(store, monkeypatch)
    expected = vibe_evaluation.evaluation_profile_inputs(store.conn, [recording])
    database, root = store._db_path, profile_files.directory(store.conn)
    store.conn.close()
    with db_handles.lease(database, exclusive=True, timeout=0):
        pass
    capture_profiles = vibe_evaluation.evaluation_profile_inputs
    def retained_inputs(conn, recordings, **options):
        with pytest.raises(TimeoutError):
            with db_handles.lease(database, exclusive=True, timeout=0):
                pytest.fail('Capture admitted database replacement')
        with file_collection.collector(root) as admitted:
            assert admitted is False
        return capture_profiles(conn, recordings, **options)
    monkeypatch.setattr(VibeEvaluation, 'training_manifest', lambda *_args, **_kwargs:
                        [{'recording': recording}])
    monkeypatch.setattr(vibe_evaluation, 'evaluation_profile_inputs', retained_inputs)
    snapshot = vibe_evaluation.evaluation_snapshot(database, tuple(home_vibe.PRESETS))
    assert snapshot['profile_inputs'] == expected
    with db_handles.lease(database, exclusive=True, timeout=0):
        pass
    with file_collection.collector(root) as admitted:
        assert admitted is True


@pytest.mark.parametrize('legacy', [
    {'format': 1},
    {'format': 2, 'profile_mask':
        'production profile preparation with artist/release source descriptions and categorical genre choices; '
        'all recording-source observations and numeric selected/override fields removed'},
])
def test_legacy_profile_plan_requires_new_freeze(tmp_path, legacy):
    path = tmp_path / 'old-plan.json'
    path.write_text(json.dumps(legacy))
    with pytest.raises(ValueError, match='freeze a separate plan'):
        evaluation.load_plan(path)


@pytest.mark.parametrize('leaked', [
    {'sources': [{'scope': 'recording'}]},
    {'overrides': {'energy': .9}},
    {'content': {'library': {'audio': {'mood_relaxed': .9}}}},
])
def test_frozen_plan_rejects_target_observations(tmp_path, leaked):
    profile = {'content': {'library': {'audio': {}}}, 'sources': [],
               'overrides': {}, 'truncated': False, **leaked}
    path = tmp_path / 'leaked-plan.json'
    path.write_text(json.dumps({'format': 2, 'profile_mask': evaluation.PROFILE_MASK,
        'code': evaluation.code_identity(), 'criteria': evaluation.CRITERIA,
        'candidate': evaluation.joint.configuration(), 'assignments': {'1': {}},
        'profile_inputs': {'1': profile}}))
    with pytest.raises(ValueError, match='Target observations leaked'):
        evaluation.load_plan(path)


def test_categorical_freeze_keeps_revisions_and_withdrawals_without_samples(file_backed_store, tmp_path):
    store = file_backed_store
    first, _ = track(store, 'judged', artist='First artist', genre='Psybient')
    alternate, _ = track(store, 'judged-upload', artist='First artist', genre='Psybient')
    other, _ = track(store, 'reserved', artist='New artist', genre='Doom Metal')
    recording, = store.conn.execute('SELECT recording_id FROM catalog_entries WHERE id=?', (first,)).fetchone()
    reserved, = store.conn.execute('SELECT recording_id FROM catalog_entries WHERE id=?', (other,)).fetchone()
    with store.conn:
        catalog.associate_item(store.conn, 'judged-upload', recording, source='verified',
            evidence={'same': True}, now=1, same_recording=True)
    for run, video, labels, now in [('one', 'judged', ['relax', 'concentrate'], 1),
                                   ('two', 'judged-upload', ['cruise'], 2),
                                   ('new', 'reserved', ['release'], 3)]:
        store.audio_vibes.save_playback(run, video, video, 'artist', now)
        store.audio_vibes.save_feedback(run, [], labels, TAXONOMY_VERSION, None, now)
    previous = [{'path': 'earlier.json', 'sha256': 'history',
                 'rows': [{'video': 'judged', 'labels': ['relax'], 'updated_at': 0}]}]
    path = tmp_path / 'judgments.json'
    before = list(store.conn.iterdump())
    categorical.freeze(store._db_path, path, previous)
    assert list(store.conn.iterdump()) == before
    plan = categorical.load(path)
    actual = {row['recording']: row for row in plan['human_targets']}
    assert actual[recording]['labels'] == ['cruise']
    assert actual[recording]['partition'] == 'development'
    assert actual[recording]['label_changed'] is True
    assert actual[reserved]['labels'] == ['release']
    assert actual[reserved]['partition'] == 'reserved'
    assert len(plan['annotations']['revisions']) == 3
    assert not store.conn.execute('SELECT 1 FROM sample_attributes').fetchone()
    assert {recording, reserved} == {int(key) for key in plan['profile_inputs']}
    # A cleared newest run cannot resurrect a prior positive from another upload.
    store.audio_vibes.save_feedback('two', [], [], TAXONOMY_VERSION, None, 4)
    cleared = tmp_path / 'cleared.json'
    categorical.freeze(store._db_path, cleared, previous)
    after = categorical.load(cleared)
    assert [row['recording'] for row in after['human_targets']] == [reserved]
    assert any(row.get('recording') == recording and row['state'] == 'withdrawn' for row in after['label_gaps'])
    assert len(after['annotations']['revisions']) == 4
    with pytest.raises(ValueError, match='prospective candidate seal'):
        categorical.run(cleared, tmp_path / 'forbidden.json', 'known', 0, 'relax', 'reserved')
    assert not (tmp_path / 'forbidden.json').exists()


def test_categorical_metrics_count_only_observed_positive_coverage():
    # Both raw-score orderings recover one acceptable alternative first; neither
    # may fabricate five negative labels or a numeric suitability target.
    scores = {vibe: 0. for vibe in home_vibe.PRESETS}
    rows = [{'recording': 1, 'cluster': 'artist:one', 'labels': ['relax', 'concentrate'],
             'baseline': {**scores, 'relax': .8, 'concentrate': .7},
             'candidate': {**scores, 'concentrate': .8, 'relax': .7}}]
    result = categorical.categorical_metrics(rows)
    assert result['baseline'] == result['candidate']
    assert result['candidate']['annotated_choice_coverage']['1'] == 1.
    assert result['candidate']['annotated_vibe_recall']['1'] == .5
    assert result['candidate']['annotated_vibe_recall']['2'] == 1.
    assert result['candidate']['within_vibe_observed_positive_retrieval']['release']['observed_positives'] == 0
    assert result['candidate']['within_vibe_observed_positive_retrieval']['release']['mean_positive_rank'] is None
    assert not any('mae' in key or 'calibration' in key for key in result['candidate'])
