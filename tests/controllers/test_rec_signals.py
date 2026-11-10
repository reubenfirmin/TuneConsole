from unittest.mock import create_autospec

import pytest

from tests.repository_mock_support import no_database
from yt_playlist.controllers.rec_signals import RecommendationSignals
from yt_playlist.repos.rec_signals import RecSignalsRepo

pytestmark = pytest.mark.usefixtures('no_database')


def test_mood_retention_passes_count_and_explicit_age_without_creating_a_time_purge():
    repository = create_autospec(RecSignalsRepo, instance=True, spec_set=True)
    signals = RecommendationSignals(repository)
    signals.record_mood(['song'], -1, 1000)
    repository.record_mood.assert_called_once_with(['song'], -1, 1000, cap=200, prune_before=None)
    signals.record_mood(['song'], 1, 2000, prune_before=100)
    assert repository.record_mood.call_args.kwargs == {'cap': 200, 'prune_before': 100}


@pytest.mark.parametrize('limit,expected', [(None, 200), (0, 0), (500, 500)])
def test_history_limit_only_defaults_when_unspecified(limit, expected):
    repository = create_autospec(RecSignalsRepo, instance=True, spec_set=True)
    RecommendationSignals(repository).recent_mood_events(limit)
    repository.recent_mood_events.assert_called_once_with(expected)
