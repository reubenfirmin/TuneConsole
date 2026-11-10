import pytest

from tests.repository_mock_support import no_database, transient_repository_store
from yt_playlist.rec import discover


@pytest.fixture
def discovery_store(no_database):
    return transient_repository_store(
        discovery={'get_discovered_albums': [], 'get_discovered_artists': []},
        genres={'feature_axes': {'Techno': ('techno', None),
                                 'House': ('house', None),
                                 'Deep House': ('house', None),
                                 'Ambient': ('ambient', None)}},
    )


def test_pick_artists_excludes_muted_family(discovery_store):
    store = discovery_store
    store.discovery.get_discovered_artists.return_value = [
        {'artist': 'TechA', 'score': 0.9, 'genre': 'Techno'},
        {'artist': 'HouseB', 'score': 0.5, 'genre': 'House'},
    ]
    store.rec_model.get_weights.return_value = {'genre:techno': 0.0}
    names = [p["artist"] for p in discover.pick_discovered_artists(store, 5, now=1.0)]
    assert "TechA" not in names and "HouseB" in names


def test_pick_artists_favored_outranks_neutral_at_equal_score(discovery_store):
    store = discovery_store
    store.discovery.get_discovered_artists.return_value = [
        {'artist': 'NeutralAmb', 'score': 0.5, 'genre': 'Ambient'},
        {'artist': 'FavHouse', 'score': 0.5, 'genre': 'House'},
    ]
    store.rec_model.get_weights.return_value = {'genre:house': 2.0}
    picked = discover.pick_discovered_artists(store, 2, now=1.0)
    assert picked[0]["artist"] == "FavHouse"     # same base score, favored family wins


def test_pick_artists_untagged_survives(discovery_store):
    store = discovery_store
    store.discovery.get_discovered_artists.return_value = [
        {'artist': 'Unknown', 'score': 0.7},
    ]
    store.rec_model.get_weights.return_value = {'genre:techno': 0.0}
    names = [p["artist"] for p in discover.pick_discovered_artists(store, 5, now=1.0)]
    assert "Unknown" in names     # untagged is neutral, never excluded


def test_pick_albums_excludes_muted_family(discovery_store):
    store = discovery_store
    store.discovery.get_discovered_albums.return_value = [
        {'browse_id': 'b1', 'artist': 'TechArtist', 'title': 'Album T',
         'year': '2020', 'genre': 'Techno'},
        {'browse_id': 'b2', 'artist': 'HouseArtist', 'title': 'Album H',
         'year': '2020', 'genre': 'House'},
    ]
    store.rec_model.get_weights.return_value = {'genre:techno': 0.0}
    titles = [a["title"] for a in discover.pick_discovered_albums(store, 5, now=1.0)]
    assert "Album T" not in titles and "Album H" in titles


def test_large_album_and_artist_pools_reuse_family_feedback(discovery_store, monkeypatch):
    store = discovery_store
    albums = [{"browse_id": f"b{i}", "artist": f"A{i}", "year": "2026", "genre": "Deep House"}
              for i in range(300)]
    artists = [{"artist": f"A{i}", "score": 0.5, "genre": "House"} for i in range(300)]
    store.discovery.get_discovered_albums.return_value = albums
    store.discovery.get_discovered_artists.return_value = artists
    calls = []
    monkeypatch.setattr(discover, "discovery_facet_weight",
                        lambda s, family, now: calls.append(family) or 1.0)
    assert len(discover.pick_discovered_albums(store, 15, now=1)) == 15
    assert len(discover.pick_discovered_artists(store, 10, now=1)) == 10
    assert calls == ["house", "house"]
