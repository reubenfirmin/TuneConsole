import pytest

from tests.repository_mock_support import no_database, repository_store
from yt_playlist.rec import taste_analysis


@pytest.fixture
def breadth_store(no_database):
    return repository_store(
        rec_query={'genre_play_distribution': {}},
        genres={'feature_axes': {'Techno': ('techno', None),
                                 'Hard Rock': ('rock', None),
                                 'Jazz': ('jazz', None),
                                 'Hip Hop': ('hip hop', None)}},
        values={'_read_only': True},
    )


def test_narrow_library_has_low_breadth(breadth_store):
    breadth_store.rec_query.genre_play_distribution.return_value = {'Techno': 10}
    assert taste_analysis.taste_breadth(breadth_store)["breadth"] == 0.0


def test_eclectic_library_has_high_breadth(breadth_store):
    breadth_store.rec_query.genre_play_distribution.return_value = {
        'Techno': 5, 'Hard Rock': 5, 'Jazz': 5, 'Hip Hop': 5,
    }
    assert taste_analysis.taste_breadth(breadth_store)["breadth"] > 0.9


def test_breadth_zero_when_untagged(breadth_store):
    assert taste_analysis.taste_breadth(breadth_store)["breadth"] == 0.0
