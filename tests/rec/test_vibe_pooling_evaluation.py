"""Compare frozen heldouts with actual preparation before target observations exist."""
import hashlib
import json

import pytest

from experiments.audio_lab_tools import vibe_pooling_evaluation as evaluation
from experiments.audio_lab_tools import vibe_pooling_labels as categorical
from tests.enrichment_support import seed_enrichment
from tests.rec.test_musical_preparation import observe
from tests.rec.test_vibe_prediction import capture, drain as drain_predictions, fits, track
from yt_playlist.core import catalog, db_handles, fao, file_collection, profile_files, vibe_provenance
from yt_playlist.core.audio_vibe_contract import TAXONOMY_VERSION
from yt_playlist.core.activity import WorkBudgetExceeded
from yt_playlist.providers.base import MetadataObservation
from yt_playlist.rec import home_vibe, musical_preparation, vibe_joint_preparation
from yt_playlist.repos import vibe_evaluation


def bucket(value):
    return int(hashlib.sha256(str(value).encode()).hexdigest(), 16) % 5


def drain(store):
    # This comparison owns final masked numerical parity, not the worker's
    # fairness under provider backlog (test_provider_backlog_cannot_starve_vibe_bootstrap_or_dependency_fanout).
    # Finish real source captures first so 66 arrivals do not reconstruct every
    # recording and refit every model once per source. The ordinary prediction
    # pipeline still computes and publishes all final values, including retries.
    for _ in range(500):
        if store.musical_profiles.pending() is None:
            break
        musical_preparation.source_step(store, 1000, batch=32)
    else:
        pytest.fail('Comparison source inputs did not drain')
    return drain_predictions(store)


@pytest.mark.parametrize('scope,stratum', [
    pytest.param('artist', 'unseen', id='unseen-artist'),
    pytest.param('release', 'known', id='known-release'),
])
def test_frozen_masked_comparison_matches_runtime_and_ignores_target_observations(
        file_backed_store, tmp_path, monkeypatch, scope, stratum):
    store = file_backed_store
    target = store.tracks.upsert_track('heldout', 'heldout', 'Target artist', '', 240)
    seed_enrichment(store, target, 'Psybient', '2000')
    rid, = store.conn.execute('SELECT recording_id FROM catalog_entries WHERE id=?', (target,)).fetchone()
    alternate = store.tracks.upsert_track('heldout-upload', 'heldout', 'Target artist', '', 240)
    with store.conn:
        catalog.associate_item(store.conn, 'heldout-upload', rid, source='verified',
            evidence={'same': True}, now=1, same_recording=True)
    key = store.tracks.identity_key_for_video('heldout')
    artist, = store.conn.execute('SELECT artist_id FROM entry_artists WHERE entry_id=?', (target,)).fetchone()
    fold = bucket(rid if stratum == 'known' else 'artist:' + str(artist))
    observe(store, target, [{'name': 'Ambient'}], scope=scope, entity='allowed-description')
    donors = []
    for index in range(12):
        entry, _ = track(store, 'donor-' + str(index),
            genre='Psybient' if stratum == 'known' else 'Doom Metal',
            artist='Target artist' if stratum == 'known' else 'Other artist ' + str(index))
        recording, = store.conn.execute('SELECT recording_id FROM catalog_entries WHERE id=?', (entry,)).fetchone()
        credit, = store.conn.execute('SELECT artist_id FROM entry_artists WHERE entry_id=?', (entry,)).fetchone()
        if bucket(recording if stratum == 'known' else 'artist:' + str(credit)) != fold:
            capture(store, 'donor-' + str(index), calm=False)
            donors.append(recording)
        if len(donors) == 2:
            break
    assert len(donors) == 2
    # These expectations come from ordinary production preparation on a fixture
    # that has never contained any of the target's numeric observations.
    drain(store)
    incumbent = {vibe: fits(store, key)[vibe]['score'] for vibe in home_vibe.PRESETS}
    assert fits(store, key)['relax']['source'] == ('artist_genre' if stratum == 'known' else 'heuristic')
    monkeypatch.setattr(vibe_joint_preparation, 'QUALIFICATIONS',
        {vibe_joint_preparation.config_identity(): 'controlled-evaluation-fixture'})
    drain(store)
    candidate = {vibe: fits(store, key)[vibe]['score'] for vibe in home_vibe.PRESETS}
    assert fits(store, key)['relax']['source'] == ('artist_genre' if stratum == 'known' else 'artist')
    for prediction_id, in store.conn.execute('SELECT prediction_id FROM recording_vibes '
            'WHERE recording_id=?', (rid,)):
        receipt = vibe_provenance.read(store.conn, prediction_id)
        assert receipt['generation']
        assert receipt['new_track_variance'] > receipt['mean_variance'] > 0
    masked_before = vibe_evaluation.evaluation_profile_inputs(store.conn, [rid])[str(rid)]
    assert [source['scope'] for source in masked_before['sources']] == [scope]
    assert masked_before['truncated'] is False

    assert store.conn.execute('SELECT recording_id FROM catalog_entries WHERE id=?', (alternate,)).fetchone()[0] == rid
    plans, scores = [], []
    for turn, calm in enumerate((False, True)):
        # These target links sort before the allowed artist/release description.
        # Neither their membership nor their values may occupy its bounded slots.
        providers = [f'a-heldout-{index:02d}' for index in range(64)]
        for provider in (*providers, 'first-provider', 'other-provider'):
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
        capture(store, 'heldout', calm=calm)
        capture(store, 'heldout-upload', calm=calm)
        if (scope, stratum, turn) == ('artist', 'unseen', 0):
            # Exercise an actual bounded source deferral, then let ordinary
            # preparation finish every source/recording with its normal budget.
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
                drain(store)
            assert deferred
        else:
            drain(store)
        ordinary, _choices, truncated = store.musical_profiles.recording_inputs([key])[key]
        assert len(ordinary) == 64 and truncated is True
        assert {source['scope'] for source in ordinary} == {'recording'}
        path = tmp_path / ('plan-' + str(turn) + '.json')
        before = list(store.conn.iterdump())
        size, read = profile_files.size, profile_files.read
        def allowed_source(key):
            assert store.conn.execute('SELECT scope FROM rec_musical_sources WHERE source_key=?',
                                      (key,)).fetchone()[0] in {'artist', 'release'}
        def masked_size(conn, kind, key):
            assert kind == 'source'
            allowed_source(key)
            return size(conn, kind, key)
        def masked_read(conn, kind, key, **options):
            assert kind == 'source'
            allowed_source(key)
            return read(conn, kind, key, **options)
        with monkeypatch.context() as patch:
            patch.setattr(profile_files, 'size', masked_size)
            patch.setattr(profile_files, 'read', masked_read)
            evaluation.freeze(store._db_path, path)
        assert list(store.conn.iterdump()) == before
        plan = json.loads(path.read_text())
        assert plan['profile_inputs'][str(rid)] == masked_before
        plans.append(plan)
        assert plan['assignments'][str(rid)][stratum] == fold
        actual = {}
        for vibe in home_vibe.PRESETS:
            output = tmp_path / f'{turn}-{vibe}.json'
            evaluation.run(path, output, stratum, fold, vibe)
            result = json.loads(output.read_text())
            assert result['outcome'] == 'complete'
            row, = result['rows']
            assert row['recording'] == rid and result['training_recordings'] == 2
            assert row['baseline'] == pytest.approx(incumbent[vibe])
            assert row['candidate'] == pytest.approx(candidate[vibe])
            actual[vibe] = {name: row[name] for name in ('baseline', 'candidate', 'interval90')}
        scores.append(actual)
        assert plan['format'] == 2
    assert plans[0]['profile_inputs'][str(rid)] == plans[1]['profile_inputs'][str(rid)]
    assert scores[0] == scores[1]
    first = next(row for row in plans[0]['rows']['relax'] if row['recording'] == rid)
    second = next(row for row in plans[1]['rows']['relax'] if row['recording'] == rid)
    assert first['score'] != second['score']

    if (scope, stratum) == ('artist', 'unseen'):
        # A controlled relational overflow must reject the entire freeze. No
        # unprepared source body may be read or silently treated as absent.
        overflow = [f'overflow-{index}' for index in range(4097)]
        with store.conn:
            store.conn.executemany('INSERT INTO rec_musical_sources '
                '(source_key,provider,scope,entity_id) VALUES (?,\'fixture\',\'recording\',\'overflow\')',
                [(source,) for source in overflow])
            store.conn.executemany('INSERT INTO rec_musical_links VALUES (?,?,?)',
                                  [(source, rid, '') for source in overflow])
        before = list(store.conn.iterdump())
        rejected = tmp_path / 'overflow-plan.json'
        with pytest.raises(WorkBudgetExceeded, match='source links exceed bounded admission'):
            evaluation.freeze(store._db_path, rejected)
        assert not rejected.exists() and list(store.conn.iterdump()) == before
        with store.conn:
            for table in ('rec_musical_links', 'rec_musical_pending', 'rec_musical_sources'):
                store.conn.executemany(f'DELETE FROM {table} WHERE source_key=?',
                                       [(source,) for source in overflow])

        # Close the producer so it cannot supply the replacement fence being
        # tested. Capture must own both lifetime leases itself, then release.
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
        with monkeypatch.context() as patch:
            patch.setattr(vibe_evaluation, 'evaluation_profile_inputs', retained_inputs)
            snapshot = vibe_evaluation.evaluation_snapshot(database, tuple(home_vibe.PRESETS))
        assert snapshot['profile_inputs'] == plans[-1]['profile_inputs']
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
