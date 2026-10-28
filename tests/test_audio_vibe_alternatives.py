"""Developer choices are acceptable alternatives to one displayed prediction."""
import json

import numpy as np
import pytest

from experiments.audio_lab_tools import train_vibes as trainer
from tests.test_audio_vibe_model import artifact, examples, excerpts
from yt_playlist.rec import audio_vibe_model as model
from yt_playlist.rec import audio_vibe_preview as heuristic


@pytest.mark.parametrize('predicted', ['cruise', 'concentrate'])
def test_either_acceptable_choice_gets_full_credit(predicted):
    scores = {**dict.fromkeys(model.LABELS, .01), predicted: .9}
    human = ['cruise', 'concentrate']
    decision = trainer.disagreement(scores, human)
    result = trainer.metrics([{'human': human, 'predicted': [predicted], 'disagreement': decision}])
    assert result['accuracy'] == 1.
    assert result['correct'] == 1
    assert result['incorrect'] == 0
    assert decision['class'] == 'agreement'
    assert decision['missed'] == decision['extra'] == []


def test_matching_runner_up_does_not_rescue_wrong_displayed_choice():
    scores = {**dict.fromkeys(model.LABELS, .01), 'energize': .9, 'cruise': .8}
    decision = trainer.disagreement(scores, ['cruise', 'concentrate'])
    assert decision['predicted'] == ['energize']
    result = trainer.metrics([{'human': ['cruise', 'concentrate'], 'predicted': ['energize', 'cruise'],
                              'disagreement': decision}])
    assert result['accuracy'] == 0.
    assert result['incorrect'] == 1


@pytest.mark.parametrize('loss', ['acceptable_nll', 'acceptable_libra'])
def test_joint_loss_gradient_and_all_acceptable_answer(loss):
    design = np.array([[1., .3], [1., -.4], [1., .8]])
    offset = np.array([[.2, -.3, .4], [-.6, .2, .1], [.1, .7, -.2]])
    allowed = np.array([[True, False, True], [False, True, False], [True, True, True]])
    weights = np.array([.1, -.4, .3, -.2, .6, -.1])

    def objective(value):
        return trainer.acceptable_objective(value, design, offset, allowed, 1.2, loss=loss)

    value, gradient = objective(weights)
    numerical = []
    for i in range(len(weights)):
        shift = np.zeros_like(weights)
        shift[i] = 1e-6
        numerical.append((objective(weights + shift)[0] - objective(weights - shift)[0]) / 2e-6)
    assert gradient == pytest.approx(numerical, abs=1e-7)
    # A track accepting every outcome supplies no preference or extra weight.
    without = trainer.acceptable_objective(weights, design[:2], offset[:2], allowed[:2], 1.2, loss=loss)
    assert value == pytest.approx(without[0])
    assert gradient == pytest.approx(without[1])


def test_set_likelihood_does_not_demand_every_alternative():
    allowed = np.array([[True, True, False]])
    design, offset = np.ones((1, 1)), np.zeros((1, 3))
    for weights in (np.array([20., -20., -20.]), np.array([-20., 20., -20.])):
        value, gradient = trainer.acceptable_objective(weights, design, offset, allowed, 0.)
        assert value < 1e-10
        assert np.abs(gradient).max() < 1e-10


def test_legacy_objective_remains_serializable_and_heuristic_export_is_unchanged():
    learned = trainer.fit(examples(), objective='legacy_binary')
    assert learned['training']['objective'] == 'legacy_binary'
    assert learned['score_link'] == 'sigmoid'
    assert model.validate(json.loads(json.dumps(learned))) == learned
    learned['classifiers'] = {}
    learned['score_link'] = 'softmax'
    baseline = {key: .1 + .1 * i for i, key in enumerate(model.LABELS)}
    assert model.scores(learned, [0.] * len(learned['features']), baseline) == baseline


def test_softmax_preserves_order_of_large_logits_and_legacy_scores(tmp_path, monkeypatch):
    samples = excerpts()
    baseline = heuristic.predict(samples, use_model=False)
    learned = artifact(samples)
    learned['classifiers'] = {key: {'intercept': 0., 'weights': [0.] * len(model.FEATURES),
                                   'baseline_weight': 0.} for key in model.LABELS}
    learned['classifiers']['cruise']['intercept'] = 100.
    learned['classifiers']['energize']['intercept'] = 101.
    learned['score_link'] = 'softmax'
    learned['training']['objective'] = 'acceptable_nll'
    path = tmp_path / 'joint.json'
    path.write_text(json.dumps(model.validate(learned)))
    monkeypatch.setenv(model.ENVIRONMENT_KEY, str(path))
    predicted = heuristic.predict(samples)
    assert predicted['vibe'] == 'energize'
    assert predicted['vibes'] == ['energize']
    assert sum(row['score'] for row in predicted['ranking']) == pytest.approx(1.)
    assert predicted['arc'] == baseline['arc']
    # Existing artifacts without the new link retain their exact score formula.
    old = artifact(samples)
    score = model.scores(old, [0.] * len(model.FEATURES), dict.fromkeys(model.LABELS, .3))['energize']
    assert score == round(model.corrected_score(.3, old['classifiers']['energize']['intercept']), 5)


@pytest.mark.parametrize('weight', [0., 1.])
def test_exported_scores_rank_like_training_logits(weight):
    records = examples()
    learned = trainer.fit(records, baseline_weight=weight)
    row = records[0]
    values = trainer.example_vector(row, learned['features'])
    scores = model.scores(learned, values, row['baseline'])
    expected = {}
    for key, classifier in learned['classifiers'].items():
        normalized = (np.array(values) - learned['center']) / learned['scale']
        expected[key] = weight * model.margin(row['baseline'][key]) + classifier['intercept'] + normalized @ classifier['weights']
    assert max(scores, key=scores.get) == max(expected, key=expected.get)
    assert sum(scores.values()) == pytest.approx(1.)
    assert learned['training']['target_interpretation'] == 'acceptable_alternatives'


@pytest.mark.parametrize('mutation', ['link', 'baseline_weight', 'partial_softmax'])
def test_invalid_score_contract_is_rejected(mutation):
    learned = trainer.fit(examples())
    if mutation == 'link':
        learned['score_link'] = 'unknown'
    elif mutation == 'baseline_weight':
        learned['classifiers']['cruise']['baseline_weight'] = float('nan')
    else:
        del learned['classifiers']['cruise']
    with pytest.raises(ValueError):
        model.validate(learned)
