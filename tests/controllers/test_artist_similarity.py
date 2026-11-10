from unittest.mock import create_autospec

import pytest

from tests.repository_mock_support import no_database
from yt_playlist.controllers.artist_similarity import ArtistSimilarity
from yt_playlist.controllers.artists import Artists
from yt_playlist.repos.artist_similarity import ArtistSimilarityRepo

pytestmark = pytest.mark.usefixtures('no_database')


@pytest.mark.parametrize('ttl_days', [14, 3, float('inf')])
def test_freshness_is_an_explicit_repository_bound(ttl_days):
    repository = create_autospec(ArtistSimilarityRepo, instance=True, spec_set=True)
    repository.cached_similar.return_value = [['Neighbor', .8]]
    artists = create_autospec(Artists, instance=True, spec_set=True)
    artists.resolve.return_value = 7
    service = ArtistSimilarity(repository, artists)
    assert service.cached_similar('artist:1', 1500000, ttl_days) == [['Neighbor', .8]]
    repository.cached_similar.assert_called_once_with(
        7, fetched_since=1500000 - ttl_days * 86400)


def test_missing_evidence_remains_distinct_from_confirmed_empty():
    repository = create_autospec(ArtistSimilarityRepo, instance=True, spec_set=True)
    service = ArtistSimilarity(repository, create_autospec(Artists, instance=True, spec_set=True))
    repository.cached_similar.side_effect = [None, []]
    assert service.cached_similar('artist:1', 10) is None
    assert service.cached_similar('artist:1', 10) == []


def test_identity_admission_finishes_before_similarity_write():
    repository = create_autospec(ArtistSimilarityRepo, instance=True, spec_set=True)
    artists = create_autospec(Artists, instance=True, spec_set=True)
    artists.state_id.side_effect = [10, 20, 30]
    service = ArtistSimilarity(repository, artists)
    service.cache_similar('Source', iter([['First', .8], ['Second', .4]]), 100)
    assert [call.args for call in artists.state_id.call_args_list] == [('Source',), ('First',), ('Second',)]
    repository.cache_similar.assert_called_once_with(10, [['First', .8], ['Second', .4]], [(20, .8), (30, .4)], 100)


def test_unknown_explicit_artist_cannot_replace_cached_evidence():
    repository = create_autospec(ArtistSimilarityRepo, instance=True, spec_set=True)
    artists = create_autospec(Artists, instance=True, spec_set=True)
    artists.state_id.side_effect = ValueError('Unknown artist reference')
    with pytest.raises(ValueError, match='Unknown artist'):
        ArtistSimilarity(repository, artists).cache_similar('@artist:999', [['Neighbor', .8]], 100)
    repository.cache_similar.assert_not_called()


def test_edge_tokens_are_built_outside_the_repository():
    repository = create_autospec(ArtistSimilarityRepo, instance=True, spec_set=True)
    repository.artist_similar_edges.return_value = [(20, .8), (30, .4)]
    artists = create_autospec(Artists, instance=True, spec_set=True)
    artists.resolve.return_value = 10
    assert ArtistSimilarity(repository, artists).artist_similar_edges('Source') == [('@artist:20', .8), ('@artist:30', .4)]
    repository.artist_similar_edges.assert_called_once_with(10)
