import pytest

from tests.repository_mock_support import no_database, transient_repository_store
from yt_playlist.rec import recipes, journeys


@pytest.fixture
def recipe_store(no_database):
    return transient_repository_store(
        rec_query={'genre_play_distribution': {}, 'era_play_distribution': {}},
    )


def test_roll_recipe_includes_a_valid_journey(recipe_store):
    store = recipe_store
    r = recipes.roll_recipe(store, "comfort", seed=1, now=1.0)
    assert r["journey"] in journeys.JOURNEYS


def test_roll_recipe_journey_weight_biases_selection(recipe_store):
    # Boost energy_arc to the cap and floor the rest; a weighted-random sample must land on it far
    # more than the uniform 1/10 baseline, and it must be the modal pick.
    store = recipe_store
    store.rec_model.get_weights.return_value = {
        f'journey:{j}': 3.0 if j == 'energy_arc' else .2 for j in journeys.JOURNEYS}
    from collections import Counter
    picks = [recipes.roll_recipe(store, "comfort", seed=s, now=1.0)["journey"] for s in range(60)]
    c = Counter(picks)
    assert c.most_common(1)[0][0] == "energy_arc"     # the boosted journey dominates
    assert c["energy_arc"] >= 30                       # >> uniform (~6); robust given p~0.625, n=60
