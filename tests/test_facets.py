from yt_playlist.rec import transient
from yt_playlist.util.matching import identity_key


def test_facets_for_maps_genre_era_artist(store):
    tid = store.upsert_track("v1", "So What", "Miles Davis", None, None, 1)
    store.set_track_genre(tid, "cool jazz")
    store.set_track_year(tid, "1959")
    k = identity_key("So What", "Miles Davis")
    facets = transient.facets_for(store, [k])
    assert facets.get("era:1950") == [k]
    assert facets.get("artist:Miles Davis") == [k]
    assert any(ax.startswith("genre:") and facets[ax] == [k] for ax in facets)


def test_facets_for_empty_and_unknown(store):
    assert transient.facets_for(store, []) == {}
    assert transient.facets_for(store, ["ghost|x"]) == {}
