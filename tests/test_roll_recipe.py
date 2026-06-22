"""roll_recipe: preference-weighted sampling of a per-playlist theme (genre/era) from your taste."""
from collections import Counter

from yt_playlist import recommend


def _seed_two_genres(store):
    iid = store.upsert_identity("main", "cred", None, True)
    # lots of techno, a little folk, all played so they have mass in the distributions
    tech = [store.upsert_track(f"t{i}", f"T{i}", "TB", None, None) for i in range(8)]
    folk = [store.upsert_track(f"f{i}", f"F{i}", "FB", None, None) for i in range(2)]
    for t in tech:
        store.set_track_genre(t, "Techno"); store.set_track_year(t, "2015")
    for f in folk:
        store.set_track_genre(f, "Folk"); store.set_track_year(f, "1972")
    # play techno heavily so it dominates the preference-weighted roll (genre mass = 1 + plays/song)
    for _ in range(6):
        store.add_history_snapshot(iid, 1.0, ["t0|tb"])
    return iid


def test_roll_recipe_shapes_and_facets(store):
    _seed_two_genres(store)
    r = recommend.roll_recipe(store, "fresh", seed=1)
    assert r["model"] == "fresh"
    assert "genres" in r["facets"] and "eras" in r["facets"]      # fresh rolls genre + era
    assert 0.0 <= r["dj"]["stickiness"] <= 1.0                    # a DJ stickiness is rolled in
    assert "seed" in r["dj"]


def test_roll_recipe_is_preference_weighted(store):
    _seed_two_genres(store)
    from yt_playlist import genre_map
    techno_fam = genre_map.family("Techno")
    picks = Counter(recommend.roll_recipe(store, "fresh", seed=s)["facets"]["genres"][0] for s in range(60))
    # techno is played far more, so it should be rolled far more often than folk
    assert picks[techno_fam] > picks.get(genre_map.family("Folk"), 0)


def test_roll_recipe_respects_muted_genre(store):
    _seed_two_genres(store)
    from yt_playlist import genre_map
    folk_fam = genre_map.family("Folk")
    store.set_weight(f"genre:{folk_fam}", 0.0, lo=0.0, hi=2.0)    # mute folk
    picks = Counter(recommend.roll_recipe(store, "fresh", seed=s)["facets"]["genres"][0] for s in range(40))
    assert folk_fam not in picks                                  # a muted genre is never rolled


def test_theme_filter_puts_matching_genre_first(store):
    from yt_playlist import genre_map, recommend
    iid = store.upsert_identity("main", "cred", None, True)
    h = store.upsert_track("v1", "H", "HArt", None, None); store.set_track_genre(h, "House")
    f = store.upsert_track("v2", "F", "FArt", None, None); store.set_track_genre(f, "Folk")
    items = [recommend.ForYouItem("F", "FArt", "", "v2", None, 0, "", "folk|fart"),
             recommend.ForYouItem("H", "HArt", "", "v1", None, 0, "", "h|hart")]
    facets = {"genres": [genre_map.family("House")]}
    out = recommend.theme_filter(store, items, facets)
    assert out[0].title == "H"        # the House track is pulled to the front by the theme
    assert {i.title for i in out} == {"H", "F"}   # nothing dropped (card still fills)
