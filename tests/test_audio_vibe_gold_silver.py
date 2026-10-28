"""Fixed silver targets must not leak their teacher's gold training answers."""
from copy import deepcopy
import hashlib
import json

import numpy as np
import pytest

from experiments.audio_lab_tools import gold_silver_vibes as gs, robust_vibes as robust, train_vibes as trainer
from tests.test_audio_vibe_model import examples, excerpts
from tests.test_audio_vibe_semisupervised import unlabeled_examples
from yt_playlist.rec import audio_vibe_model as model, audio_vibe_preview as heuristic


def snapshot():
    silver = []
    for row in unlabeled_examples():
        scores = {key: .8 if key == 'energize' else .1 for key in model.LABELS}
        silver.append({**row, 'labels': ['energize'], 'target_source': 'model_prediction',
            'teacher_scores': scores, 'teacher_model_sha256': 'b' * 64})
    return {'gold': examples(), 'silver': silver,
        'teacher': {'training_video_ids': [f'song-{index}' for index in range(4)],
            'training_songs': 4, 'model_sha256': 'b' * 64}}


@pytest.mark.parametrize('loss', ['acceptable_nll', 'acceptable_libra'])
def test_weighted_alternative_loss_matches_finite_differences_and_removed_zero_weight(loss):
    rng = np.random.default_rng(507)
    design, offset = rng.normal(size=(4, 3)), rng.normal(size=(4, 6))
    allowed = np.array([[True, False, False, False, False, False],
        [False, True, True, False, False, False], [True] * 6, [False] * 5 + [True]])
    importance = np.array([1., .25, .5, 0.])
    weights = rng.normal(size=18)

    def objective(value):
        return trainer.acceptable_objective(value, design, offset, allowed, 2.,
                                             loss=loss, sample_weights=importance)

    value, gradient = objective(weights)
    for index in range(len(weights)):
        shift = np.zeros_like(weights); shift[index] = 1e-6
        difference = (objective(weights + shift)[0] - objective(weights - shift)[0]) / 2e-6
        assert gradient[index] == pytest.approx(difference, abs=1e-8)
    expected = trainer.acceptable_objective(weights, design[:3], offset[:3], allowed[:3], 2.,
                                            loss=loss, sample_weights=importance[:3])
    assert value == pytest.approx(expected[0])
    assert gradient == pytest.approx(expected[1])


@pytest.mark.parametrize('objective', ['legacy_binary', 'acceptable_nll'])
def test_silver_changes_training_with_explicit_weight_and_gold_only_statistics(objective, monkeypatch):
    data = snapshot()
    gold, silver = data['gold'], data['silver']
    silver[0]['values'][-1] = 100.
    silver[0]['subgenre'] = 'silver only genre'
    captured = []
    function = 'logistic_objective' if objective == 'legacy_binary' else 'acceptable_objective'
    original = getattr(trainer, function)

    def observe(*args, **kwargs):
        captured.append(kwargs.get('sample_weights'))
        return original(*args, **kwargs)

    monkeypatch.setattr(trainer, function, observe)
    learned = trainer.fit(gold, objective=objective, silver_examples=silver, silver_weight=.25)
    assert captured
    assert all(np.array_equal(weights, [1.] * 10 + [.25] * 6) for weights in captured)
    plain = trainer.fit(gold, objective=objective)
    assert plain['center'] == learned['center'] and plain['scale'] == learned['scale']
    assert not any('silver only genre' in name for name in learned['features'])
    assert learned['classifiers'] != plain['classifiers']
    assert learned['training']['songs'] == 16
    assert learned['training']['gold_silver']['effective_training_weight'] == 11.5
    assert model.validate(json.loads(json.dumps(learned))) == learned


def test_silver_targets_do_not_manufacture_minimum_gold_class_support():
    data = snapshot()
    learned = trainer.fit(data['gold'][:4], objective='legacy_binary',
                          silver_examples=data['silver'], silver_weight=1.)
    assert learned['classifiers'] == {}


@pytest.mark.parametrize('mutation', ['overlap', 'teacher_overlap', 'duplicate', 'prediction', 'hash', 'source', 'too_few_clean'])
def test_bad_gold_silver_or_teacher_provenance_is_rejected(mutation):
    data = snapshot()
    if mutation == 'overlap':
        data['silver'][0]['video_id'] = data['gold'][-1]['video_id']
    elif mutation == 'teacher_overlap':
        data['teacher']['training_video_ids'][0] = data['silver'][0]['video_id']
    elif mutation == 'duplicate':
        data['silver'].append(data['silver'][0])
    elif mutation == 'prediction':
        data['silver'][0]['labels'] = ['relax']
    elif mutation == 'hash':
        data['silver'][0]['teacher_model_sha256'] = 'c' * 64
    elif mutation == 'source':
        data['silver'][0]['target_source'] = 'human'
    else:
        data['teacher']['training_video_ids'] = [row['video_id'] for row in data['gold'][:8]]
        data['teacher']['training_songs'] = 8
    with pytest.raises(ValueError):
        gs.validate(data)


@pytest.mark.parametrize('weight', [-1., 1.1, True, float('nan'), 0.])
def test_invalid_silver_weight_is_rejected(weight):
    data = snapshot()
    with pytest.raises(ValueError):
        trainer.fit(data['gold'], silver_examples=data['silver'], silver_weight=weight)


def test_nested_validation_excludes_teacher_known_songs_and_held_out_gold(monkeypatch):
    data = snapshot()
    candidates = {'gold': {'regularization': 1.}, 'silver': {'regularization': 1., 'silver_weight': .25}}
    predict = robust.predict
    captured = []
    target = data['gold'][-1]['video_id']
    teacher_ids = set(data['teacher']['training_video_ids'])

    def observe(learned, held_out, training_songs):
        assert held_out['video_id'] not in teacher_ids
        if held_out['video_id'] == target and training_songs == 9:
            captured.append(deepcopy(learned))
        return predict(learned, held_out, training_songs)

    monkeypatch.setattr(robust, 'predict', observe)
    exported, before = gs.train(data, candidates=candidates, inner_folds=2)
    fitted = captured[:]; captured.clear()
    changed = deepcopy(data)
    changed['gold'][-1].update(labels=['release'], subgenre='detroit techno', values=[1e9] * len(model.FEATURES))
    _, after = gs.train(changed, candidates=candidates, inner_folds=2)
    assert captured == fitted
    assert before['outer_selections'][-1] == after['outer_selections'][-1]
    assert before['evaluation_songs'] == 6
    assert set(before['teacher_known_gold_training_only']) == teacher_ids
    assert exported['gold_silver']['training']['songs'] == 16
    assert exported['gold_only']['training']['songs'] == 10
    assert before['silver_dataset_sha256'] == after['silver_dataset_sha256']


@pytest.mark.parametrize('objective', ['legacy_binary', 'acceptable_nll'])
def test_silver_export_replays_in_existing_runtime(objective, tmp_path, monkeypatch):
    data = snapshot()
    samples = excerpts()
    baseline = heuristic.predict(samples, use_model=False)
    for row in data['gold'] + data['silver']:
        row['source_signature'] = model.signature(samples[0])
        row['baseline'] = {entry['vibe']: entry['score'] for entry in baseline['ranking']}
    data['gold'][-1]['values'] = model.vector(samples, baseline)
    learned = trainer.fit(data['gold'], objective=objective, silver_examples=data['silver'], silver_weight=.25)
    path = tmp_path / 'model.json'; path.write_text(json.dumps(learned))
    monkeypatch.setenv(model.ENVIRONMENT_KEY, str(path))
    scores = model.scores(learned, trainer.example_vector(data['gold'][-1], learned['features']), data['gold'][-1]['baseline'])
    actual = heuristic.predict(samples)
    assert {row['vibe']: row['score'] for row in actual['ranking']} == scores
    assert actual['arc'] == baseline['arc']
    assert actual['model']['training_songs'] == 16


def test_freeze_and_cli_preserve_fixed_teacher_and_export_separate_tiers(tmp_path, monkeypatch):
    gold, pool = examples(), unlabeled_examples()
    teacher = trainer.fit(gold[:4])
    teacher['training']['candidate'] = 'teacher'
    teacher['evaluation'] = {'method': 'development_leave_one_song_out', 'selected_candidate': 'teacher', 'results': {}}
    teacher_path = tmp_path / 'teacher.json'; teacher_path.write_text(json.dumps(teacher))
    teacher_hash = hashlib.sha256(teacher_path.read_bytes()).hexdigest()
    teacher_report = tmp_path / 'teacher-report.json'
    teacher_report.write_text(json.dumps({'manifest': gold[:4], 'evaluation': 'development_leave_one_song_out',
                                        'selected_candidate': 'teacher', 'results': {}}))
    source = tmp_path / 'source.json'; source.write_text(json.dumps({'examples': gold, 'unlabeled': pool}))
    predictions = tmp_path / 'predictions.json'
    rows = []
    for row in pool:
        scores = model.scores(teacher, trainer.example_vector(row, teacher['features']), row['baseline'])
        rows.append({'video_id': row['video_id'], 'predicted': trainer.select(scores),
                     'scores': scores, 'model_sha256': teacher_hash})
    predictions.write_text(json.dumps({'model': str(teacher_path), 'model_sha256': teacher_hash, 'predictions': rows}))
    saved = {path: path.read_bytes() for path in (source, predictions, teacher_path, teacher_report)}
    frozen = gs.freeze(source, predictions, teacher_report)
    assert frozen['gold'] == gold
    assert [row['labels'] for row in frozen['silver']] == [row['predicted'] for row in rows]
    monkeypatch.setattr(gs, 'CANDIDATES', {'gold': {'regularization': 1.},
        'silver': {'regularization': 1., 'silver_weight': .25}})
    monkeypatch.setenv(model.ENVIRONMENT_KEY, '')
    output, report_path = tmp_path / 'model.json', tmp_path / 'report.json'
    args = ['--dataset', str(source), '--predictions', str(predictions), '--teacher-report', str(teacher_report),
            '--output', str(output), '--report', str(report_path)]
    gs.main(args)
    assert model.configured_path() is None
    assert all(path.read_bytes() == content for path, content in saved.items())
    assert json.loads(report_path.with_suffix('.dataset.json').read_text()) == frozen
    report = json.loads(report_path.read_text())
    assert report['gold_songs'] == 10 and report['silver_songs'] == 6 and report['evaluation_songs'] == 6
    assert model.validate(json.loads(report_path.with_suffix('.silver-model.json').read_text()))['training']['songs'] == 16
    assert len(report['silver_predictions']) == 6
    assert json.loads(output.read_text())['evaluation']['results'] == {
        key: value for key, value in report['nested'].items() if key != 'predictions'}
    with pytest.raises(SystemExit):
        gs.main(args[:-4] + ['--output', str(teacher_path), '--report', str(report_path)])
    assert teacher_path.read_bytes() == saved[teacher_path]
    bad = json.loads(predictions.read_text()); bad['predictions'][0]['predicted'] = ['release']
    predictions.write_text(json.dumps(bad))
    with pytest.raises(ValueError):
        gs.freeze(source, predictions, teacher_report)
