from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import Mock, create_autospec

import pytest

from tests.repository_mock_support import no_database
from yt_playlist.controllers.genres import Genres
from yt_playlist.controllers.recommendation_evidence import RecommendationEvidence
from yt_playlist.controllers.recommendation_eligibility import RecommendationEligibility
from yt_playlist.repos.rec_evidence import RecEvidenceRepo

pytestmark = pytest.mark.usefixtures('no_database')


@pytest.fixture
def evidence():
    repository = create_autospec(RecEvidenceRepo, instance=True, spec_set=True)
    eligibility = create_autospec(RecommendationEligibility, instance=True, spec_set=True)
    eligibility.maximum_duration.return_value = 1200
    eligibility.generated_only_keys.return_value = set()
    eligibility.excluded_playlist_ids.return_value = {4}
    eligibility.catchall_playlist_ids.return_value = {8}
    genres = create_autospec(Genres, instance=True, spec_set=True)
    genres.feature_axes.return_value = {}
    for operation in ('playlist_memberships', 'album_memberships', 'artist_memberships',
                      'session_memberships', 'dated_session_memberships', 'content_facets'):
        getattr(repository, operation).return_value = []
    repository.eligible_keys.return_value = {'a', 'b', 'c'}
    return RecommendationEvidence(repository, eligibility, genres, read_view=Mock(), read_only=True)


def test_membership_duplicates_and_ineligible_songs_cannot_create_a_basket(evidence):
    evidence.repository.playlist_memberships.return_value = [
        (1, 'a'), (1, 'a'), (1, 'tainted'), (2, 'b'), (2, 'c'), (2, 'absent')]
    evidence.eligibility.generated_only_keys.return_value = {'tainted'}
    evidence.repository.eligible_keys.return_value.add('tainted')
    assert [set(basket) for basket in evidence.rec_baskets()] == [{'b', 'c'}]
    evidence.repository.playlist_memberships.assert_called_once_with(excluded={4, 8})
    evidence.repository.eligible_keys.assert_called_once_with(
        maximum_duration=1200, excluded_video_types=('MUSIC_VIDEO_TYPE_UGC',))


@pytest.mark.parametrize('method,cap,kwargs', [
    ('album_memberships', 30, {}), ('album_memberships', 2, {'max_album': 2}),
    ('artist_memberships', 50, {}), ('session_memberships', 120, {}),
    ('session_memberships', 2, {'max_session': 2}),
])
def test_relationship_caps_count_distinct_eligible_recordings(evidence, method, cap, kwargs):
    keys = [f'key-{index}' for index in range(cap + 1)]
    evidence.repository.eligible_keys.return_value = set(keys)
    getattr(evidence.repository, method).return_value = [
        *((1, key) for key in keys[:cap]), (1, keys[0]), *((2, key) for key in keys)]
    assert [set(basket) for basket in evidence.rec_baskets(**kwargs)] == [set(keys[:cap])]


def test_content_groups_follow_current_families_and_decades(evidence):
    evidence.repository.content_facets.return_value = [
        {'key': 'a', 'genre': 'Renamed', 'year': '1991'},
        {'key': 'b', 'genre': 'Alias', 'year': '1999-01-01'},
        {'key': 'c', 'genre': 'Other', 'year': 'unknown'},
    ]
    evidence.genres.feature_axes.return_value = {
        'Renamed': ('current-family', 'current-id'), 'Alias': ('current-family', 'current-id'),
        'Other': ('other-family', 'other-id')}
    assert [set(basket) for basket in evidence.rec_baskets()] == [{'a', 'b'}, {'a', 'b'}]


def test_historical_existence_proof_preserves_eligibility_and_excludes_undated_relationships(evidence):
    evidence.repository.unchanged_playlists.return_value = {1, 4, 8}
    evidence.repository.playlist_memberships.return_value = [(1, 'a'), (1, 'b'), (1, 'ugc')]
    evidence.repository.dated_keys.return_value = {'c', 'generated'}
    evidence.repository.eligible_keys.return_value = {'a', 'b', 'c', 'generated'}
    evidence.eligibility.generated_only_keys.return_value = {'generated'}
    evidence.repository.dated_session_memberships.return_value = [
        ('day-1', 'a'), ('day-1', 'c'), ('day-2', 'c'), ('day-2', 'generated')]
    evidence.repository.known_playlist_count.return_value = 5
    baskets, coverage = evidence.rec_baskets_as_of(100)
    assert [set(basket) for basket in baskets] == [{'a', 'b'}, {'a', 'c'}]
    assert coverage == {'eligible_tracks': 3, 'safe_playlists': 1, 'known_playlists': 5,
        'session_baskets': 2, 'mutable_content_baskets_excluded': True,
        'undated_relationship_baskets_excluded': ['album', 'artist']}
    evidence.repository.playlist_memberships.assert_called_once_with(included={1})
    evidence.repository.album_memberships.assert_not_called()
    evidence.repository.artist_memberships.assert_not_called()
    evidence.repository.content_facets.assert_not_called()


@pytest.mark.parametrize('historical', [False, True])
def test_foreground_work_uses_and_releases_the_injected_private_reader(evidence, historical):
    evidence.read_only = False
    reader = create_autospec(RecommendationEvidence, instance=True, spec_set=True)
    reader.rec_baskets.return_value = [['a', 'b']]
    reader.rec_baskets_as_of.return_value = ([['a', 'b']], {})
    events = []

    @contextmanager
    def view():
        events.append('opened')
        try:
            yield SimpleNamespace(rec_evidence=reader)
        finally:
            events.append('closed')

    evidence.read_view.side_effect = view
    result = evidence.rec_baskets_as_of(100) if historical else evidence.rec_baskets()
    assert result == (([['a', 'b']], {}) if historical else [['a', 'b']])
    assert events == ['opened', 'closed']
    evidence.repository.eligible_keys.assert_not_called()
