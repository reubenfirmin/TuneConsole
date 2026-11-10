from tests.repository_mock_support import no_database, repository_store
from yt_playlist.rec import transient
from yt_playlist.util.matching import identity_key


def test_facets_for_maps_genre_era_artist(store):
    tid = store.tracks.upsert_track("v1", "So What", "Miles Davis", None, None, 1)
    store.tracks.set_track_genre(tid, "cool jazz")
    store.tracks.set_track_year(tid, "1959")
    k = identity_key("So What", "Miles Davis")
    facets = transient.facets_for(store, [k])
    assert facets.get("era:1950") == [k]
    assert facets.get("artist:Miles Davis") == [k]
    assert any(ax.startswith("genre:") and facets[ax] == [k] for ax in facets)


def test_facets_for_empty_and_unknown(no_database):
    store = repository_store(
        rec_query={'track_genres': {}, 'track_decades': {}, 'track_artists': {}},
        genres={'feature_axes': {}}, values={'_read_only': True})
    assert transient.facets_for(store, []) == {}
    store.rec_query.track_genres.assert_not_called()
    store.rec_query.track_decades.assert_not_called()
    store.rec_query.track_artists.assert_not_called()
    assert transient.facets_for(store, ["ghost|x"]) == {}
    store.rec_query.track_genres.assert_called_once_with(['ghost|x'])
    store.rec_query.track_decades.assert_called_once_with(['ghost|x'])
    store.rec_query.track_artists.assert_called_once_with(['ghost|x'])
