"""Independent-vibe routing and bounded coefficient-loading requirements."""
import base64
from copy import deepcopy
import hashlib
import json
import zlib

import pytest

from experiments.audio_lab_tools import cruise_specialist as runtime
from yt_playlist.rec import audio_vibe_model


def coefficients():
    stage = {'kind': 'ridge', 'medians': [0.] * 200, 'mean': [0.] * 200,
             'scale': [1.] * 200, 'weights': [[0., 0.] for _ in range(200)],
             'intercept': [0., .8]}
    stage['weights'][0][0] = 1.
    reverse = deepcopy(stage)
    reverse['weights'][0].reverse()
    reverse['intercept'].reverse()
    return {'pmemo': {'source': 'pmemo', 'target_names': ['arousal', 'valence'],
                     'source_expert_sha256': 'a' * 64, 'model': stage},
            'deam': {'source': 'deam', 'target_names': ['valence', 'arousal'],
                     'source_expert_sha256': 'b' * 64, 'model': reverse}}


def write(tmp_path, native=None, **changes):
    payload = coefficients() if native is None else native
    group = json.loads(audio_vibe_model.DEFAULT_PATH.read_text())
    raw = json.dumps(payload).encode()
    artifact = {'format': runtime.FORMAT, 'taxonomy_version': group['taxonomy_version'],
                'feature_version': runtime.audio_embedding.VERSION,
                'feature_names': list(runtime.audio_embedding.FEATURES),
                'embedding_signature': group['embedding_signature'],
                'source_plan_sha256': 'c' * 64,
                'aggregation': 'mean_native_then_map', 'combination': 'equal_pmemo_deam',
                'score_semantics': 'independent_uncalibrated_suitability', 'encoding': 'zlib-json-base64',
                'payload_sha256': hashlib.sha256(raw).hexdigest(),
                'payload': base64.b64encode(zlib.compress(raw)).decode(), **changes}
    path = tmp_path / 'cruise.json'
    path.write_text(json.dumps(artifact))
    return path, group


def test_averaging_native_excerpts_preserves_declared_moderate_activation_mapping(tmp_path):
    path, _group = write(tmp_path)
    model = runtime.load(path)
    quiet, forceful = [.1] + [0.] * 199, [.9] + [0.] * 199
    assert runtime.score(model, [quiet, forceful])['score'] == pytest.approx(.9)
    mean_mapped = (runtime.score(model, [quiet])['score'] + runtime.score(model, [forceful])['score']) / 2
    assert mean_mapped == pytest.approx(.5)
    assert runtime.score(model, [quiet])['sample_count'] == 1


def test_each_native_source_is_exposed_before_equal_combination(tmp_path):
    payload = coefficients()
    payload['deam']['model']['intercept'][0] = .2
    path, _group = write(tmp_path, payload)
    actual = runtime.score(runtime.load(path), [[.1] + [0.] * 199])
    assert actual['source_scores'] == pytest.approx({'pmemo': .5, 'deam': .2})
    assert actual['score'] == pytest.approx(.35)


def test_source_replay_rejects_errors_that_cancel_in_the_ensemble():
    from experiments.audio_lab_tools.cruise_export import source_replay_errors
    expected = {'pmemo': [.2, .6], 'deam': [.8, .4]}
    swapped = {'pmemo': [.8, .4], 'deam': [.2, .6]}
    assert [sum(pair) / 2 for pair in zip(*expected.values())] == [sum(pair) / 2 for pair in zip(*swapped.values())]
    with pytest.raises(ValueError, match='source replay differs'):
        source_replay_errors(expected, swapped)
    assert source_replay_errors(expected, expected) == {'pmemo': 0., 'deam': 0.}


def test_cruise_override_preserves_five_shared_scores_and_overall_classification(tmp_path):
    path, group = write(tmp_path)
    values = [[.1] + [0.] * 199]
    off = runtime.route(group, values, group['embedding_signature'], mode='off')
    shadow = runtime.route(group, values, group['embedding_signature'], specialist_path=path)
    active = runtime.route(group, values, group['embedding_signature'], specialist_path=path, mode='active')
    assert off['suitability'] == shadow['suitability'] == shadow['shared_scores']
    assert active['applied_vibes'] == ['cruise']
    assert active['shared_top_vibe'] == shadow['shared_top_vibe'] == off['shared_top_vibe']
    for vibe in group['labels']:
        if vibe != 'cruise':
            assert active['suitability'][vibe] == off['suitability'][vibe]
    assert active['suitability']['cruise'] == pytest.approx(.5)
    assert sum(active['suitability'].values()) != pytest.approx(1.)


@pytest.mark.parametrize('failure', ['missing', 'payload', 'checksum', 'signature', 'coefficients'])
def test_failed_optional_model_retains_the_entire_shared_result(tmp_path, failure):
    path, group = write(tmp_path)
    if failure == 'missing':
        path.unlink()
    elif failure == 'coefficients':
        payload = coefficients()
        payload['pmemo']['model']['scale'][0] = 1e-320
        path, group = write(tmp_path, payload)
    else:
        mutation = {'payload': {'payload': 'not valid base64'},
                    'checksum': {'payload_sha256': '0' * 64},
                    'signature': {'embedding_signature': '0' * 64}}[failure]
        path, group = write(tmp_path, **mutation)
    values = [[1.] * 200]
    baseline = runtime.route(group, values, group['embedding_signature'], mode='off')
    actual = runtime.route(group, values, group['embedding_signature'], specialist_path=path, mode='active')
    assert actual['suitability'] == baseline['suitability']
    assert actual['individual_models'] == {}


@pytest.mark.parametrize('mutation', ['nan', 'zero_scale', 'width', 'kind', 'feature_order', 'taxonomy', 'aggregation'])
def test_invalid_schema_or_coefficients_are_rejected_before_inference(tmp_path, mutation):
    payload, header = coefficients(), {}
    if mutation == 'nan':
        payload['deam']['model']['weights'][0][0] = float('nan')
    elif mutation == 'zero_scale':
        payload['deam']['model']['scale'][0] = 0
    elif mutation == 'width':
        payload['deam']['model']['weights'][0].pop()
    elif mutation == 'kind':
        payload['deam']['model']['kind'] = 'neighbors'
    elif mutation == 'feature_order':
        header['feature_names'] = list(reversed(runtime.audio_embedding.FEATURES))
    elif mutation == 'taxonomy':
        header['taxonomy_version'] = 'future'
    else:
        header['aggregation'] = 'mean_mapped_chunks'
    path, _group = write(tmp_path, payload, **header)
    with pytest.raises(ValueError):
        runtime.load(path)


def test_payload_expansion_and_additional_compressed_members_are_bounded(tmp_path):
    too_large = zlib.compress(b' ' * (runtime.MAX_PAYLOAD + 1))
    path, _group = write(tmp_path, payload_sha256='a' * 64,
                         payload=base64.b64encode(too_large).decode())
    with pytest.raises(ValueError):
        runtime.load(path)
    raw = json.dumps(coefficients()).encode()
    path, _group = write(tmp_path, payload=base64.b64encode(zlib.compress(raw) + zlib.compress(b'{}')).decode())
    with pytest.raises(ValueError):
        runtime.load(path)


@pytest.mark.parametrize('mutation', ['missing', 'wrong_type', 'invalid_hash'])
def test_source_plan_provenance_is_required_by_the_standalone_loader(tmp_path, mutation):
    path, _group = write(tmp_path, source_plan_sha256='a' * 64)
    runtime.load(path)
    artifact = json.loads(path.read_text())
    if mutation == 'missing':
        artifact.pop('source_plan_sha256')
    elif mutation == 'wrong_type':
        artifact['source_plan_sha256'] = 123
    else:
        artifact['source_plan_sha256'] = 'z' * 64
    invalid = tmp_path / f'{mutation}.json'
    invalid.write_text(json.dumps(artifact))
    with pytest.raises(ValueError):
        runtime.load(invalid)


def sample(number):
    embedding = runtime.audio_embedding
    return {'model': 'msd-musicnn-1', 'model_version': 'controlled-extractor-1',
            'artifact_hashes': {name: 'a' * 64 for name in embedding.ARTIFACTS},
            'embedding': {'version': embedding.VERSION, 'layer': embedding.LAYER,
                          'graph_policy': embedding.GRAPH_POLICY, 'pooling': embedding.POOLING,
                          'patches': 3, 'values': [number] * embedding.DIMENSIONS}}


@pytest.mark.parametrize('count', [1, 2, 3])
def test_capture_boundary_matches_product_shared_scores_for_partial_and_complete_captures(tmp_path, count):
    samples = [sample(number / 10) for number in range(count)]
    path, group = write(tmp_path, embedding_signature=runtime.audio_embedding.signature(samples[0]))
    group['embedding_signature'] = runtime.audio_embedding.signature(samples[0])
    reference = runtime.shared.scores(group, samples)
    active = runtime.route_samples(group, samples, specialist_path=path, mode='active')
    assert active['shared_scores'] == reference
    assert active['applied_vibes'] == ['cruise']
    assert active['individual_models']['cruise']['sample_count'] == count
    assert all(active['suitability'][vibe] == reference[vibe]
               for vibe in group['labels'] if vibe != 'cruise')


@pytest.mark.parametrize('mutation', ['missing', 'mixed', 'unknown', 'empty', 'excessive'])
def test_capture_boundary_rejects_incompatible_excerpt_provenance(tmp_path, mutation):
    samples = [sample(.1), sample(.9)]
    path, group = write(tmp_path, embedding_signature=runtime.audio_embedding.signature(samples[0]))
    group['embedding_signature'] = runtime.audio_embedding.signature(samples[0])
    if mutation == 'missing':
        samples[-1].pop('embedding')
    elif mutation == 'mixed':
        samples[-1]['artifact_hashes'][runtime.audio_embedding.ARTIFACTS[0]] = 'b' * 64
    elif mutation == 'unknown':
        for value in samples:
            value['model_version'] = 'different-extractor'
    elif mutation == 'empty':
        samples = []
    else:
        samples = [samples[0]] * (runtime.MAX_EXCERPTS + 1)
    with pytest.raises(ValueError):
        runtime.route_samples(group, samples, specialist_path=path, mode='active')
