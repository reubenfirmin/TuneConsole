"""Holdout identity closure and paired whole-group ranking uncertainty."""
import numpy as np
import pytest

from experiments.audio_lab_tools.cruise.cruise_baselines import interval, reserve_groups


def test_addition_reserves_older_collaborators_through_transitive_components():
    rows = [{'video_id': 'old-a', 'artist': 'Alpha'},
            {'video_id': 'old-collaboration', 'artist': 'Alpha+Beta'},
            {'video_id': 'new', 'artist': 'Beta'},
            {'video_id': 'unrelated', 'artist': ''},
            {'video_id': 'other', 'artist': 'Gamma'}]
    development, held, groups, additions = reserve_groups(rows, {'old-a', 'old-collaboration', 'unrelated', 'other'})
    assert held == [0, 1, 2]
    assert development == [3, 4]
    assert additions == {'new'}
    assert len({groups[i] for i in held}) == 1
    assert set(groups[i] for i in held).isdisjoint(groups[i] for i in development)


def test_paired_bootstrap_identical_scores_have_zero_difference():
    y = np.asarray([.1, .3, .8, 1.] * 2)
    split = np.asarray([0] * 4 + [1] * 4)
    groups = ['a', 'a', 'b', 'b', 'c', 'c', 'd', 'd']
    actual = interval(y, y, y.copy(), split, groups, 20, 570)
    assert actual['ndcg_gain'] == 0.
    assert actual['conditional_95_percent_interval'] == [0., 0.]
    assert actual['usable_draws'] == 20
    groups[-1] = 'a'
    with pytest.raises(ValueError, match='crosses'):
        interval(y, y, y, split, groups, 20, 570)
