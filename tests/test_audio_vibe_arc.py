"""Ordered production inference, including specialist and shared fallbacks."""
from copy import deepcopy
from statistics import mean

import pytest

from tests.test_audio_vibe_cruise import samples
from yt_playlist.controllers.vibe_prediction import PredictionValues
from yt_playlist.rec import audio_vibe_arc as arc
from experiments.audio_lab_tools.legacy import audio_vibe_external as external
from yt_playlist.rec import audio_vibe_cruise as cruise
from experiments.audio_lab_tools.legacy import audio_vibe_model as shared
from yt_playlist.rec import audio_vibe_prediction as predictor
from yt_playlist.rec import audio_vibe_runtime as runtime, vibe_preparation


def trajectory(values):
    rows = samples()
    for row, value in zip(rows, values, strict=True):
        row['embedding']['values'] = [value] * 200
    return rows


def model():
    return {'header': {'embedding_signature': shared.load(shared.DEFAULT_PATH)[0]['embedding_signature']},
            'sha256': 'a' * 64}


def excerpt_scorer(_model, values):
    assert len(values) == 1, 'Production must not average excerpts before temporal scoring'
    value = values[0][0]
    return {'score': value, 'native': {'observed': value}, 'sample_count': 1,
            'aggregation': 'single_excerpt', 'sha256': 'a' * 64}


@pytest.mark.parametrize('vibe', shared.LABELS)
def test_production_retains_each_native_observation_until_the_arc_policy(vibe):
    rows = trajectory((.2, .9, .4))
    # Actual capture times override point names and arrival order.
    rows[0]['position_seconds'] = 200.
    before = deepcopy(rows)
    prediction = predictor.predict(rows, use_model=False)
    result = runtime.apply_loaded(list(reversed(rows)), prediction, selected_mode='active',
                                  model=model(), scorer=excerpt_scorer, vibe=vibe)
    observed = result['individual_models'][vibe]
    assert [row['position_seconds'] for row in observed['excerpts']] == [120.1, 160.1, 200.]
    assert [row['native']['observed'] for row in observed['excerpts']] == [.9, .4, .2]
    assert [row['sample_id'] for row in observed['excerpts']] == [rows[1]['id'], rows[2]['id'], rows[0]['id']]
    assert result['suitability'][vibe] == arc.fit(vibe, [.9, .4, .2])[0]
    assert observed['aggregation'] == 'ordered_arc'
    assert observed['arc_version'] == arc.VERSION
    assert rows == before


@pytest.mark.parametrize('vibe', ['energize', 'uplift'])
def test_production_build_and_fade_have_identical_means_and_different_scores(vibe):
    scores = []
    for values in ((.2, .5, .8), (.8, .5, .2)):
        rows = trajectory(values)
        prediction = predictor.predict(rows, use_model=False)
        result = runtime.apply_loaded(rows, prediction, selected_mode='active',
                                      model=model(), scorer=excerpt_scorer, vibe=vibe)
        scores.append(result['suitability'][vibe])
    assert mean((.2, .5, .8)) == mean((.8, .5, .2))
    assert scores[0] > scores[1]


def test_single_observation_is_not_a_manufactured_progression():
    for vibe in shared.LABELS:
        assert arc.fit(vibe, [.4])[0] == .4
    assert arc.path([.4])['steps'] == []


def test_cruise_does_not_invent_moderation_between_quiet_and_intense_passages():
    rows = trajectory((0., .5, 1.))
    stage = {'kind': 'ridge', 'mean': [0.] * 200, 'scale': [1.] * 200,
             'weights': [[1., 0.]] + [[0., 0.]] * 199, 'intercept': [0., .8]}
    artifact = {**model(), 'experts': {source: {'model': stage, 'target_names': ['arousal', 'valence']}
                                      for source in cruise.SOURCES}}
    baseline = predictor.predict(rows, use_model=False)
    result = runtime.apply_loaded(rows, baseline, selected_mode='active', model=artifact,
                                  scorer=cruise.score, vibe='cruise')
    assert [row['score'] for row in result['individual_models']['cruise']['excerpts']] == [.4, .9, .4]
    assert result['suitability']['cruise'] == .4
    assert cruise.score(artifact, [row['embedding']['values'] for row in rows])['score'] == .9


def test_detached_specialists_keep_ordered_observations_without_shared_inference(monkeypatch):
    rows = trajectory((.2, .5, .8))
    calls = []

    def classifier(_model, excerpts):
        assert len(excerpts) == 1
        calls.append(excerpts[0]['id'])
        value = excerpts[0]['embedding']['values'][0]
        return {vibe: value if vibe in ('energize', 'uplift') else .2 for vibe in shared.LABELS}

    monkeypatch.setattr(external, 'scores', classifier)
    captured = PredictionValues([('energize', 'active', model(), excerpt_scorer)])
    build = captured.predict(list(reversed(rows)))
    assert calls == []
    assert build['suitability']['energize'] == .8
    assert next(row for row in build['ranking'] if row['vibe'] == 'energize')['excerpt_scores'] == [.2, .5, .8]
    assert captured.predict(rows) == build
    for row, value in zip(rows, (.8, .5, .2), strict=True):
        row['embedding']['values'] = [value] * 200
    fade = captured.predict(rows)
    assert fade['suitability']['energize'] < build['suitability']['energize']
    shared_score = lambda result: next(row['score'] for row in result['ranking'] if row['vibe'] == 'energize')
    assert shared_score(fade) < shared_score(build)


def test_policy_changes_queue_bounded_repreparation_without_breaking_installed_reads(monkeypatch):
    version = vibe_preparation.version()
    monkeypatch.setattr(arc, 'VERSION', 'next-temporal-policy')
    assert vibe_preparation.version() != version
    assert vibe_preparation.compatible_version(version)
