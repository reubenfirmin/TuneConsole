"""Post-result paired-label permutation diagnostic, separate from frozen gates."""
from collections import Counter
from pathlib import Path
import time

import numpy as np
from threadpoolctl import threadpool_limits

from experiments.audio_lab_tools import encoder_models as models, individual_protocol as protocol
from experiments.audio_lab_tools.chroma_study import DEFAULT, LAB
from experiments.audio_lab_tools.external_experiment import digest, save
from experiments.audio_lab_tools.focus_experiment import arrays, load
from experiments.audio_lab_tools.quality_round import verify

OUTPUT = LAB / 'local/chroma-810-20261101/focus-permutation'
DOCUMENT = LAB / 'CHROMA_FOCUS_DIAGNOSTIC.md'


def test_paired_activity_permutation_diagnostic():
    verify(DEFAULT, required_stages=['extraction', 'predictions', 'evaluation'])
    OUTPUT.mkdir(exist_ok=True)
    assert not (OUTPUT / 'report.json').exists(), 'Preserve completed diagnostics; do not overwrite.'
    frozen = {'document_sha256': digest(DOCUMENT), 'source_sha256': digest(Path(__file__)),
              'plan_sha256': digest(DEFAULT / 'plan.json'), 'draws': 99, 'seed': 8101101,
              'notice': 'Post-result diagnostic; conditions on the repeated component.'}
    save(OUTPUT / 'plan.json', frozen)
    rows = load(DEFAULT / 'extraction.json')['rows']
    ids = np.array([i for i, r in enumerate(rows) if r['source'] == models.CAL])
    groups = np.array([rows[i]['component'] for i in ids])
    outer = np.array([rows[i]['outer'] for i in ids])
    sizes = Counter(groups)
    movable = np.array([i for i, g in enumerate(groups) if sizes[g] == 1])
    assert len(ids) == 32 and len(movable) == 30 and sorted(sizes.values()) == [1] * 30 + [2]
    data = arrays(DEFAULT / 'features.npz')
    counts = data['counts'][ids]
    truth = arrays(DEFAULT / 'inputs/baseline-predictions.npz')['truth__' + models.CAL]
    observed = load(DEFAULT / 'report.json')['tables']['oof__' + models.CAL + '__focus']['paired']['combined__vs__musicnn']['ndcg']['difference']
    rng = np.random.default_rng(frozen['seed'])
    differences = []
    started = time.monotonic()
    with threadpool_limits(limits=1):
        for draw in range(99):
            assert time.monotonic() - started < 600
            y = truth.copy()
            y[movable] = truth[rng.permutation(movable)]
            scores = {}
            for variant in ('musicnn', 'combined'):
                chunks = data[variant][ids]
                native = np.empty_like(y)
                for fold in range(5):
                    train, valid = outer != fold, outer == fold
                    assert not set(groups[train]).intersection(groups[valid])
                    heads, _ = models.choose(chunks[train], counts[train], y[train], groups[train])
                    native[valid] = models.predict({models.CAL: heads}, chunks[valid], counts[valid])[models.CAL]
                scores[variant] = protocol.metrics(y.mean(axis=1), native.mean(axis=1), outer)['ndcg']
            differences.append(scores['combined'] - scores['musicnn'])
    save(OUTPUT / 'report.json', {**frozen, 'seconds': time.monotonic() - started,
        'observed_difference': observed, 'null_differences': differences,
        'tail_fraction': (1 + sum(d >= observed for d in differences)) / 100,
        'null_quantiles': np.quantile(differences, [.025, .5, .975]).tolist()})
    assert np.isfinite(differences).all()
