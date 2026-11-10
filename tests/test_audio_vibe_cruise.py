"""Production routing and durable recommendation preparation for Cruise."""
import base64
from copy import deepcopy
import hashlib
import json
import zlib

import pytest

from tests.audio_vibe_fixture_support import disable_periodic_library_sync, specialist_store
from tests.rec.test_vibe_prediction import drain, fits, track
from tests.test_audio_vibe_embeddings import with_embedding
from tests.test_audio_vibe_model import excerpts
from yt_playlist.core import audio_embedding, features
from yt_playlist.core.audio_vibe_contract import INTENSITY_CHANNELS
from yt_playlist.rec import audio_vibe_cruise as cruise
from yt_playlist.rec import audio_vibe_release as release
from yt_playlist.rec import audio_vibe_relax as relax
from yt_playlist.rec import audio_vibe_uplift as uplift
from yt_playlist.rec import audio_vibe_energize as energize
from yt_playlist.rec import audio_vibe_focus as focus
from yt_playlist.rec import audio_vibe_model as shared, audio_vibe_prediction as predictor
from yt_playlist.rec import home_vibe, musical_character, vibe_preparation


@pytest.fixture(autouse=True)
def production_models(isolated_app_home, monkeypatch, tmp_path):
    monkeypatch.setattr(features, 'LOCAL_ENV', tmp_path / '.env.local')
    monkeypatch.setenv(shared.ENVIRONMENT_KEY, str(shared.DEFAULT_PATH))
    monkeypatch.setenv(cruise.ENVIRONMENT_KEY, str(cruise.DEFAULT_PATH))
    monkeypatch.setenv(cruise.MODE_KEY, 'active')
    monkeypatch.setenv(release.MODE_KEY, 'off')
    monkeypatch.setenv(relax.MODE_KEY, 'off')
    monkeypatch.setenv(uplift.MODE_KEY, 'off')
    monkeypatch.setenv(energize.MODE_KEY, 'off')
    monkeypatch.setenv(focus.MODE_KEY, 'off')


def samples(video='observed_song'):
    # Synthetic audio values with the real bundled encoder identity. Actual
    # captured values are replayed separately by the research acceptance command.
    rows = [with_embedding(row, .2) for row in excerpts(video)]
    for row in rows:
        row['artifact_hashes'].update({
            'musicnn/model.json': '2df238355538b680e70f06e30fb04df52f133da6ea1915530ec8c590da073762',
            'musicnn/group1-shard1of1.bin': '7ebfb970c7389b6276ffc34f0d7fc80ba2765e5310829f3618ad5b977326667d',
        })
    return rows


def baseline(rows, monkeypatch):
    with monkeypatch.context() as patch:
        patch.setenv(cruise.MODE_KEY, 'off')
        return predictor.predict(rows, require_complete=False)


def controlled(tmp_path, rows, valence=.8):
    stage = {'kind': 'ridge', 'medians': [0.] * 200, 'mean': [0.] * 200,
             'scale': [1.] * 200, 'weights': [[0., 0.] for _ in range(200)],
             'intercept': [.5, valence]}
    payload = {source: {'source': source, 'source_expert_sha256': 'a' * 64,
                       'target_names': ['arousal', 'valence'], 'model': stage}
               for source in cruise.SOURCES}
    raw = json.dumps(payload).encode()
    header = json.loads(cruise.DEFAULT_PATH.read_text())
    header.update(embedding_signature=audio_embedding.signature(rows[0]),
                  payload_sha256=hashlib.sha256(raw).hexdigest(),
                  payload=base64.b64encode(zlib.compress(raw)).decode())
    path = tmp_path / 'cruise.json'
    path.write_text(json.dumps(header))
    return path


@pytest.mark.parametrize('count', [1, 2, 3])
def test_bundled_model_changes_only_cruise_suitability(count, monkeypatch):
    rows = samples()[:count]
    original = deepcopy(rows)
    before = baseline(rows, monkeypatch)
    after = predictor.predict(rows, require_complete=False)
    assert {k: v for k, v in after.items() if k not in ('suitability', 'individual_models')} == before
    assert after['individual_models']['cruise']['sha256'] == '3927a3294ad7a1670ef7f81d5c6119943991a699de60a7d5a60022851ba304eb'
    candidate = cruise.score(cruise.load(cruise.DEFAULT_PATH), [row['embedding']['values'] for row in rows])
    assert after['suitability']['cruise'] == candidate['score']
    assert after['suitability']['cruise'] != next(r['score'] for r in before['ranking'] if r['vibe'] == 'cruise')
    assert after['individual_models']['cruise']['sample_count'] == count
    assert after['individual_models']['cruise']['score_semantics'] == 'independent_uncalibrated_suitability'
    for row in before['ranking']:
        if row['vibe'] != 'cruise':
            assert after['suitability'][row['vibe']] == row['score']
    assert rows == original


def test_shadow_reports_candidate_but_preparation_uses_shared_scores(monkeypatch):
    rows = samples()
    before = baseline(rows, monkeypatch)
    monkeypatch.setenv(cruise.MODE_KEY, 'shadow')
    after = predictor.predict(rows)
    assert after['suitability'] == {r['vibe']: r['score'] for r in before['ranking']}
    assert after['individual_models']['cruise']['mode'] == 'shadow'
    measured = vibe_preparation.measured({'samples': rows, 'metadata': {}}, [], {}, {})
    assert {v: fit['score'] for v, fit in measured['fits'].items() if v not in INTENSITY_CHANNELS} == after['suitability']
    for channel, vibe in INTENSITY_CHANNELS.items():
        assert measured['fits'][channel]['score'] == after['suitability'][vibe]


@pytest.mark.parametrize('failure', ['missing', 'empty', 'malformed', 'oversize', 'checksum',
                                    'signature', 'taxonomy', 'mode', 'payload', 'coefficient'])
def test_unavailable_or_invalid_optional_model_preserves_shared_prediction(failure, tmp_path, monkeypatch):
    rows = samples()
    before = baseline(rows, monkeypatch)
    path = controlled(tmp_path, rows)
    if failure == 'missing':
        path.unlink()
    elif failure == 'empty':
        monkeypatch.setenv(cruise.ENVIRONMENT_KEY, '')
    elif failure == 'malformed':
        path.write_text('[]')
    elif failure == 'oversize':
        path.write_bytes(b' ' * (cruise.MAX_BYTES + 1))
    elif failure == 'mode':
        monkeypatch.setenv(cruise.MODE_KEY, 'unknown')
    elif failure == 'coefficient':
        path = controlled(tmp_path, rows, valence=float('nan'))
    else:
        value = json.loads(path.read_text())
        field, replacement = {'checksum': ('payload_sha256', '0' * 64),
                              'signature': ('embedding_signature', '0' * 64),
                              'taxonomy': ('taxonomy_version', 'unknown'),
                              'payload': ('payload', 'invalid base64')}[failure]
        value[field] = replacement
        path.write_text(json.dumps(value))
    if failure != 'empty':
        monkeypatch.setenv(cruise.ENVIRONMENT_KEY, str(path))
    assert predictor.predict(rows) == before


def test_missing_or_mixed_capture_provenance_does_not_apply_cruise(monkeypatch):
    rows = samples()
    rows[-1]['artifact_hashes']['musicnn/model.json'] = 'f' * 64
    assert predictor.predict(rows) == predictor.predict(rows, use_model=False)
    rows[-1].pop('embedding')
    assert predictor.predict(rows) == predictor.predict(rows, use_model=False)


def test_shared_model_disable_also_disables_individual_model(monkeypatch):
    monkeypatch.setenv(shared.ENVIRONMENT_KEY, '')
    rows = samples()
    assert predictor.predict(rows) == predictor.predict(rows, use_model=False)


def test_default_configuration_and_local_rollback(monkeypatch):
    monkeypatch.delenv(cruise.ENVIRONMENT_KEY)
    monkeypatch.delenv(cruise.MODE_KEY)
    assert cruise.mode() == 'active'
    assert cruise.configured_path() == str(cruise.DEFAULT_PATH)
    active = cruise.identity()
    features.LOCAL_ENV.write_text(cruise.MODE_KEY + '=off\n')
    assert cruise.mode() == 'off'
    assert cruise.identity() != active
    monkeypatch.setenv(cruise.MODE_KEY, 'active')
    assert cruise.identity() == active
    features.LOCAL_ENV.write_text(cruise.ENVIRONMENT_KEY + '=\n')
    assert cruise.configured_path() is None
    assert cruise.identity() != active


def test_missing_metadata_does_not_block_audio_model_or_require_labels(store):
    store.tracks.upsert_track('bare', 'Bare', '', '', 240)
    key = store.tracks.identity_key_for_video('bare')
    for now, row in enumerate(samples('bare'), 1):
        store.audio_vibes.save_sample(row, now)
    drain(store, batch=1)
    expected = predictor.predict(samples('bare'))['suitability']['cruise']
    assert fits(store, key)['cruise']['score'] == expected
    assert fits(store, key)['cruise']['source'] == 'audio_measurement'


def test_background_rollout_replaces_one_vote_updates_consumers_and_rolls_back(specialist_store, tmp_path, monkeypatch):
    store = specialist_store
    _tid, own = track(store, 'observed_song')
    _tid, neighbour = track(store, 'neighbour')
    rows = samples()
    monkeypatch.setenv(cruise.MODE_KEY, 'off')
    drain(store, batch=1).close()
    assert fits(store, own)['cruise']['source'] == 'heuristic'
    for now, row in enumerate(rows[:2], 1):
        store.audio_vibes.save_sample(row, now)
    drain(store, batch=1).close()
    assert fits(store, own)['cruise']['source'] == 'heuristic'
    store.audio_vibes.save_sample(rows[2], 3)
    store.audio_vibes.save_feedback(rows[0]['run_id'], [], ['release'], predictor.TAXONOMY_VERSION, None, 4)
    feedback = store.audio_vibes.feedback(rows[0]['run_id'])
    drain(store, batch=1).close()
    before, neighbour_before = fits(store, own), fits(store, neighbour)
    stamp = vibe_preparation.version()
    path = controlled(tmp_path, rows)
    monkeypatch.setenv(cruise.ENVIRONMENT_KEY, str(path))
    monkeypatch.setenv(cruise.MODE_KEY, 'active')
    assert vibe_preparation.version() != stamp
    statements = []
    store.conn.set_trace_callback(statements.append)
    drain(store, batch=1).close()
    after, neighbour_after = fits(store, own), fits(store, neighbour)
    assert after['cruise']['source'] == 'audio_measurement'
    assert after['cruise']['score'] == pytest.approx(.9)
    assert neighbour_after['cruise']['recordings'] == 1
    assert neighbour_after['cruise']['score'] > neighbour_before['cruise']['score']
    for vibe in shared.LABELS:
        if vibe != 'cruise':
            assert after[vibe] == before[vibe]
            assert neighbour_after[vibe] == neighbour_before[vibe]
    compact = musical_character.load(store, [own, neighbour])
    assert home_vibe.affinity({'key': own}, 'cruise', compact) == after['cruise']['score']
    # The independent Cruise head cannot alter radio's Energize/Release intensity.
    assert musical_character.intensity_evidence({'musical': compact[own]}) == max(
        ((before[v]['score'], before[v]['support']) for v in ('energize', 'release')), key=lambda v: v[0])
    # A replaced artifact invalidates prepared measurements without a new capture.
    active_stamp = vibe_preparation.version()
    controlled(tmp_path, rows, valence=.2)
    assert vibe_preparation.version() != active_stamp
    drain(store, batch=1).close()
    assert fits(store, own)['cruise']['score'] == pytest.approx(.6)
    assert fits(store, neighbour)['cruise']['recordings'] == 1
    # Continuing complete captures are still one recording in the cohort.
    for now, row in enumerate(samples(), 10):
        store.audio_vibes.save_sample(row, now)
    drain(store, batch=1).close()
    assert fits(store, neighbour)['cruise']['recordings'] == 1
    monkeypatch.setenv(cruise.MODE_KEY, 'off')
    drain(store, batch=1).close()
    assert fits(store, own) == before
    assert fits(store, neighbour) == neighbour_before
    store.conn.set_trace_callback(None)
    assert store.audio_vibes.feedback(rows[0]['run_id']) == feedback
    assert not any('audio_annotation' in sql or 'audio_taxonom' in sql for sql in statements)
