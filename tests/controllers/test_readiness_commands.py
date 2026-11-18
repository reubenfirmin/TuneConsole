"""Readiness decisions use supplied output, configuration and randomness."""
from copy import deepcopy
import random
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from tests.repository_mock_support import no_database
from yt_playlist.controllers.growth_readiness import GrowthReadiness, OnboardingSamples
from yt_playlist.rec.onboarding_sampling import sample_tracks
from yt_playlist.rec.readiness_policy import foundation_ready, albums_ready

pytestmark = pytest.mark.usefixtures('no_database')


def context(generation='g', fit='f'):
    return {'policy': {'content_generation': generation, 'collaborative_inputs': fit},
        'layers': {'saved': {'mix': {'titles': ['one']}}}}


def test_foundation_requires_current_taste_and_distinct_finite_ranked_capacity():
    tracks = [{'key': 'a', 'score': .3}, {'key': 'b', 'score': .1}]
    assert foundation_ready([], [context()], 'g', 'f', tracks, 2)
    assert not foundation_ready([], [context('old')], 'g', 'f', tracks, 2)
    assert not foundation_ready([], [context(fit='old')], 'g', 'f', tracks, 2)
    assert foundation_ready([{'id': 1}], [], None, None, tracks, 2)
    for invalid in (float('nan'), float('inf'), True, None, '.8'):
        assert not foundation_ready([{}], [], None, None, [tracks[0], {'key': 'b', 'score': invalid}], 2)
    assert not foundation_ready([{}], [], None, None, [tracks[0], tracks[0]], 2)
    assert not albums_ready([{}], 2)
    assert albums_ready([{}, {}], 2)


def test_acquisition_skips_contexts_when_modes_answer_and_stops_after_current_context():
    modes = Mock(return_value=[{}])
    configuration, contexts = Mock(), Mock()
    reserve = Mock(return_value=[{'key': 'a', 'score': .2}])
    command = GrowthReadiness(modes=modes, contexts=contexts, configuration=configuration,
        reserve=reserve, album_menu=lambda: [{}, {}])
    assert command.foundation(1) and command.albums(2)
    configuration.assert_not_called()
    contexts.assert_not_called()
    modes.return_value = []
    configuration.return_value = ('g', 'f')
    def acquired():
        yield context('stale')
        yield context()
        pytest.fail('A second usable context must not be acquired')
    contexts.side_effect = acquired
    assert command.foundation(1)
    assert reserve.call_count == 2


def test_sampling_is_repeatable_diverse_bounded_and_does_not_mutate_rows():
    rows = [dict(vid=str(i), title='Song', artist='Artist', album=None, thumb=None,
        k=str(i), duration=120, genre='Jazz' if i < 5 else 'Rock') for i in range(8)]
    before = deepcopy(rows)
    chosen = sample_tracks(rows, 4, random.Random(19))
    assert chosen == sample_tracks(rows, 4, random.Random(19))
    assert len({track['video_id'] for track in chosen}) == 4
    assert {int(track['video_id']) < 5 for track in chosen[:2]} == {True, False}
    assert rows == before and sample_tracks([], 12, random.Random(0)) == []
    repo = Mock(spec_set=['library_sample'])
    repo.library_sample.return_value = rows
    assert OnboardingSamples(repo, random.Random(19)).sample(4) == chosen
    repo.library_sample.assert_called_once_with(4)
