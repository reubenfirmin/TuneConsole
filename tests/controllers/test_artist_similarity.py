from unittest.mock import create_autospec

import pytest

from tests.repository_mock_support import no_database
from yt_playlist.controllers.artist_similarity import ArtistSimilarity
from yt_playlist.repos.artist_similarity import ArtistSimilarityRepo

pytestmark = pytest.mark.usefixtures('no_database')


@pytest.mark.parametrize('ttl_days', [14, 3, float('inf')])
def test_freshness_is_an_explicit_repository_bound(ttl_days):
    repository = create_autospec(ArtistSimilarityRepo, instance=True, spec_set=True)
    repository.cached_similar.return_value = [['Neighbor', .8]]
    service = ArtistSimilarity(repository)
    assert service.cached_similar('artist:1', 1500000, ttl_days) == [['Neighbor', .8]]
    repository.cached_similar.assert_called_once_with(
        'artist:1', fetched_since=1500000 - ttl_days * 86400)


def test_missing_evidence_remains_distinct_from_confirmed_empty():
    repository = create_autospec(ArtistSimilarityRepo, instance=True, spec_set=True)
    service = ArtistSimilarity(repository)
    repository.cached_similar.side_effect = [None, []]
    assert service.cached_similar('artist:1', 10) is None
    assert service.cached_similar('artist:1', 10) == []
