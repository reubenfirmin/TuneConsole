from unittest.mock import create_autospec

import pytest

from tests.repository_mock_support import no_database
from yt_playlist.controllers.recommendation_candidates import RecommendationCandidates
from yt_playlist.controllers.recommendation_eligibility import RecommendationEligibility
from yt_playlist.rec import candidate_policy
from yt_playlist.repos.rec_candidates import RecCandidatesRepo
from yt_playlist.repos.rec_eligibility import RecEligibilityRepo
from yt_playlist.repos.settings import SettingsRepo

pytestmark = pytest.mark.usefixtures('no_database')


@pytest.fixture
def candidates():
    repository = create_autospec(RecCandidatesRepo, instance=True, spec_set=True)
    eligibility = create_autospec(RecommendationEligibility, instance=True, spec_set=True)
    eligibility.maximum_duration.return_value = 600
    eligibility.excluded_playlist_ids.return_value = {2, 3}
    return RecommendationCandidates(repository, eligibility)


def test_revisit_uses_two_rest_windows_and_preserves_explicit_empty_keys(candidates):
    candidates.revisit_candidates(20000000, keys=[], limit=12)
    args, kwargs = candidates.repository.revisit_candidates.call_args
    criteria, = args
    assert criteria.month == 20000000 - 30 * 86400
    assert criteria.half_year == 20000000 - 183 * 86400
    assert (criteria.repeated_plays, criteria.long_rest_plays) == (2, 20)
    assert kwargs == {'maximum_duration': 600, 'keys': [], 'limit': 12}


@pytest.mark.parametrize('days,seconds', [(0, 1.), (-1, 1.), (2, 172800.)])
def test_comfort_normalizes_recency_before_persistence(candidates, days, seconds):
    candidates.comfort_candidates(100, min_plays=8, recency_full_days=days, limit=7)
    candidates.repository.comfort_candidates.assert_called_once_with(
        100, min_plays=8, full_seconds=seconds, maximum_duration=600,
        order=candidate_policy.COMFORT_ORDER, limit=7)


def test_rotation_resolves_excluded_playlists_outside_repository(candidates):
    candidates.more_like_rotation(seed_limit=8, limit=12)
    candidates.repository.more_like_rotation.assert_called_once_with(
        seed_limit=8, limit=12, maximum_plays=1, order=candidate_policy.ROTATION_ORDER,
        maximum_duration=600, excluded_playlists={2, 3})


def test_playlist_completion_supplies_artist_weight(candidates):
    candidates.complete_playlist(7, limit=12)
    candidates.repository.complete_playlist.assert_called_once_with(
        7, limit=12, artist_weight=2, order=candidate_policy.PLAYLIST_ORDER, maximum_duration=600)


def test_eligibility_resolves_config_and_feedback_semantics_before_querying():
    repository = create_autospec(RecEligibilityRepo, instance=True, spec_set=True)
    settings = create_autospec(SettingsRepo, instance=True, spec_set=True)
    settings.get_setting.return_value = '12'
    eligibility = RecommendationEligibility(repository, settings)
    assert eligibility.maximum_duration() == 720
    eligibility.suppressed_candidate_keys(['song'], 100)
    repository.suppressed_candidate_keys.assert_called_once_with(
        ['song'], 100, surface='for_you', scope='', track_kinds=('dismiss', 'mute', 'not_now'),
        global_kind='dislike', artist_kind='mute')
    eligibility.catchall_playlist_ids()
    repository.catchall_playlist_ids.assert_called_once_with(120)
