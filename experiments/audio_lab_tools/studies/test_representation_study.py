"""Negative controls for identity-bound caches and incomplete ranking evidence."""
import numpy as np
import pytest

from experiments.audio_lab_tools.datasets.dataset_windows import content_hash
from experiments.audio_lab_tools.studies.representation_study import advancement, check_chunk


def test_cache_cannot_reuse_changed_interval_or_hidden_state():
    identity = {'start': 10, 'states': [5, 6]}
    result = {'embedding': [0.] * 1536, 'final_embedding': [0.] * 768, 'hidden_states': 13}
    record = {'input': identity, 'result': result, 'result_sha256': content_hash(result)}
    check_chunk(record, identity, np.zeros(768))
    with pytest.raises(ValueError, match='identity'):
        check_chunk(record, {**identity, 'start': 20}, np.zeros(768))
    with pytest.raises(ValueError, match='parity'):
        check_chunk(record, identity, np.ones(768))
    result['embedding'][0] = 1
    with pytest.raises(ValueError, match='content'):
        check_chunk(record, identity, np.zeros(768))


def test_one_missing_primary_domain_cannot_establish_advancement():
    def case(difference, interval):
        return {'vibe': 'relax', 'primary': True, 'rows': 32, 'components': 20,
                'paired': {'middle__vs__mert': {
                    'ndcg': {'difference': difference, 'conditional_95_percent_interval': interval},
                    'top_lift': {'difference': difference}, 'mse': {'difference': 0}}}}
    cells = {'large_gain': case(.2, [.1, .3]), 'unavailable': case(None, None)}
    result = advancement('relax', cells, 'middle__vs__mert')
    assert result['recommendation'] == 'retain'
    assert result['primary_cases'] == ['large_gain', 'unavailable']
