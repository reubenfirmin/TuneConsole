"""roll_recipe: preference-weighted sampling of a per-playlist theme (genre/era) from your taste."""
from collections import Counter

import pytest

from tests.repository_mock_support import no_database, transient_repository_store
from yt_playlist.rec import recipes, surfaces
from yt_playlist.rec import transient
from yt_playlist.rec.taste_analysis import era_distribution, taste_breadth
from yt_playlist.util.matching import identity_key


@pytest.fixture
def recipe_store(no_database):
    # Eight Techno tracks plus six plays, versus two Folk tracks.
    return transient_repository_store(
        rec_query={'genre_play_distribution': {'Techno': 14, 'Folk': 2},
                   'era_play_distribution': {'2010': 14, '1970': 2}},
        genres={'feature_axes': {'Techno': ('techno', None), 'Folk': ('folk-country', None),
                                 'deep house': ('house', 'house-deep'), 'jazz': ('jazz', None),
                                 'House': ('house', None)}},
    )


def _recipe_inputs(store, *, now=None):
    return {'dists': (taste_breadth(store)['families'], dict(era_distribution(store))),
            'leans': transient.facet_leans(store, now) if now is not None else {}}


def test_roll_recipe_shapes_and_facets(recipe_store):
    store = recipe_store
    r = recipes.roll_recipe(store, "fresh", seed=1)
    assert r["model"] == "fresh"
    assert "genres" in r["facets"] and "eras" in r["facets"]      # fresh rolls genre + era
    assert 0.0 <= r["dj"]["stickiness"] <= 1.0                    # a DJ stickiness is rolled in
    assert "seed" in r["dj"]


def test_roll_recipe_is_preference_weighted(recipe_store):
    store = recipe_store
    from yt_playlist.util import genre_map
    techno_fam = genre_map.family("Techno")
    inputs = _recipe_inputs(store)
    assert recipes.roll_recipe(store, 'fresh', seed=0) == recipes.roll_recipe(store, 'fresh', seed=0, **inputs)
    picks = Counter(recipes.roll_recipe(store, "fresh", seed=s, **inputs)["facets"]["genres"][0] for s in range(60))
    # techno is played far more, so it should be rolled far more often than folk
    assert picks[techno_fam] > picks.get(genre_map.family("Folk"), 0)


def test_roll_recipe_respects_muted_genre(recipe_store):
    store = recipe_store
    from yt_playlist.util import genre_map
    folk_fam = genre_map.family("Folk")
    store.rec_model.get_weights.return_value = {f'genre:{folk_fam}': 0.}
    inputs = _recipe_inputs(store)
    assert recipes.roll_recipe(store, 'fresh', seed=0) == recipes.roll_recipe(store, 'fresh', seed=0, **inputs)
    picks = Counter(recipes.roll_recipe(store, "fresh", seed=s, **inputs)["facets"]["genres"][0] for s in range(40))
    assert folk_fam not in picks                                  # a muted genre is never rolled


def test_roll_recipe_transient_suppresses_disfavored_genre(recipe_store):
    store = recipe_store
    store.rec_query.genre_play_distribution.return_value = {'deep house': 10, 'jazz': 10}
    store.rec_query.era_play_distribution.return_value = {}
    house_keys = [identity_key(f"H{i}", f"DJ{i}") for i in range(5)]
    jazz_keys = [identity_key(f"J{i}", f"Sx{i}") for i in range(5)]
    store.rec_query.track_genres.return_value = {
        **dict.fromkeys(house_keys, 'deep house'), **dict.fromkeys(jazz_keys, 'jazz')}
    store.rec_query.track_artists.return_value = {
        **{key: f'DJ{i}' for i, key in enumerate(house_keys)},
        **{key: f'Sx{i}' for i, key in enumerate(jazz_keys)}}
    store.history.recent_plays_with_ts.return_value = [(key, 1.) for key in house_keys + jazz_keys]
    previous_inputs = _recipe_inputs(store, now=10.0)
    # hammer "less house" so the transient lean is strongly negative
    store.rec_signals.recent_mood_events.return_value = [(10., -2, [key]) for key in house_keys]
    # roll many recipes; house should be picked far less than jazz
    inputs = _recipe_inputs(store, now=10.0)
    genres = [recipes.roll_recipe(store, "wheelhouse", seed=s, now=10.0, **inputs)["facets"].get("genres", [])
              for s in range(200)]
    house = sum("house" in str(sample).lower() for sample in genres)
    jazz = sum(sample == ["jazz"] for sample in genres)
    assert house < jazz                                          # transient "less house" steers generation
    # Find a seed whose actual recipe changes because of the new mood. The
    # default path must capture that new signal rather than reusing old leans.
    changed = next(s for s, genre in enumerate(genres) if recipes.roll_recipe(
        store, 'wheelhouse', seed=s, now=10.0, **previous_inputs)['facets'].get('genres', []) != genre)
    assert recipes.roll_recipe(store, 'wheelhouse', seed=changed, now=10.0) == recipes.roll_recipe(
        store, 'wheelhouse', seed=changed, now=10.0, **inputs)


def test_roll_recipe_fresh_is_always_shuffle(recipe_store):
    # Fresh playlists are unowned proposals with no plays/recency, so data-dependent journeys
    # (rediscovery, deep dive…) must never be rolled. Every seed yields a straight shuffle (#32).
    store = recipe_store
    inputs = _recipe_inputs(store)
    journeys = {recipes.roll_recipe(store, "fresh", seed=s, **inputs)["journey"] for s in range(60)}
    assert journeys == {"shuffle"}


def test_roll_recipe_owned_lanes_can_roll_other_journeys(recipe_store):
    # Non-fresh lanes still roll varied journeys (so the fresh gate is the only thing pinned).
    store = recipe_store
    inputs = _recipe_inputs(store)
    journeys = {recipes.roll_recipe(store, "wheelhouse", seed=s, **inputs)["journey"] for s in range(60)}
    assert journeys - {"shuffle"}                                  # at least one non-shuffle journey


def test_default_recipe_inputs_notice_changed_permanent_weights(recipe_store):
    from yt_playlist.util import genre_map
    store = recipe_store
    techno, folk = genre_map.family('Techno'), genre_map.family('Folk')
    inputs = _recipe_inputs(store)
    store.rec_model.get_weights.return_value = {f'genre:{techno}': 0.}
    before = recipes.roll_recipe(store, 'fresh', seed=1)
    assert before['facets']['genres'] == [folk]
    assert before == recipes.roll_recipe(store, 'fresh', seed=1, **inputs)
    store.rec_model.get_weights.return_value = {f'genre:{techno}': 1., f'genre:{folk}': 0.}
    after = recipes.roll_recipe(store, 'fresh', seed=1)
    assert after['facets']['genres'] == [techno]
    assert after == recipes.roll_recipe(store, 'fresh', seed=1, **inputs)


def test_theme_filter_puts_matching_genre_first(recipe_store):
    from yt_playlist.util import genre_map
    store = recipe_store
    store.rec_query.track_genres.return_value = {'h|hart': 'House', 'folk|fart': 'Folk'}
    items = [surfaces.ForYouItem("F", "FArt", "", "v2", None, 0, "", "folk|fart"),
             surfaces.ForYouItem("H", "HArt", "", "v1", None, 0, "", "h|hart")]
    facets = {"genres": [genre_map.family("House")]}
    out = recipes.theme_filter(store, items, facets)
    assert out[0].title == "H"        # the House track is pulled to the front by the theme
    assert {i.title for i in out} == {"H", "F"}   # nothing dropped (card still fills)
