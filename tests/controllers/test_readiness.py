from unittest.mock import create_autospec

import pytest

from tests.repository_mock_support import no_database
from yt_playlist.controllers.readiness import Readiness
from yt_playlist.controllers.recommendation_eligibility import RecommendationEligibility
from yt_playlist.repos.readiness import ReadinessRepo

pytestmark = pytest.mark.usefixtures('no_database')


@pytest.fixture
def readiness():
    repository = create_autospec(ReadinessRepo, instance=True, spec_set=True)
    eligibility = create_autospec(RecommendationEligibility, instance=True, spec_set=True)
    eligibility.maximum_duration.return_value = 600
    return Readiness(repository, eligibility)


def test_catalog_queries_receive_resolved_length_limit(readiness):
    readiness.repository.library_sample.return_value = [{'key': 'song'}]
    readiness.repository.playable_owned.return_value = True
    assert readiness.library_sample(3) == [{'key': 'song'}]
    assert readiness.playable_owned(exclude_generated=True)
    readiness.repository.library_sample.assert_called_once_with(3, maximum_duration=600)
    readiness.repository.playable_owned.assert_called_once_with(exclude_generated=True, maximum_duration=600)


def test_familiarity_is_owned_by_onboarding(readiness):
    readiness.familiar_albums()
    readiness.repository.familiar_albums.assert_called_once_with(minimum_songs=2, minimum_plays=2)


def test_vibe_counts_share_the_usable_evidence_criteria(readiness):
    readiness.repository.has_vibe_evidence.return_value = True
    readiness.repository.vibe_evidence.return_value = (2, 5)
    assert readiness.has_vibe_evidence('v1:')
    assert readiness.vibe_evidence('v1:') == (2, 5)
    readiness.repository.has_vibe_evidence.assert_called_once_with(
        'v1:', sources=('audio_measurement', 'provider_measurement', 'artist_genre', 'artist', 'genre'),
        minimum_support=0)
    readiness.repository.vibe_evidence.assert_called_once_with(
        'v1:', measured_sources=('audio_measurement', 'provider_measurement'),
        proxy_sources=('artist_genre', 'artist', 'genre'), minimum_support=0)


def test_provider_completion_criteria_are_explicit(readiness):
    readiness.journey_history()
    readiness.repository.journey_history.assert_called_once_with(
        provider='lastfm', completed_statuses=('found', 'no_match'))
