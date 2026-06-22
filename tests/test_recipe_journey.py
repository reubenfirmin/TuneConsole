from yt_playlist.rec import recommend, journeys


def test_roll_recipe_includes_a_valid_journey(store):
    r = recommend.roll_recipe(store, "comfort", seed=1, now=1.0)
    assert r["journey"] in journeys.JOURNEYS


def test_roll_recipe_journey_weight_biases_selection(store):
    # Boost energy_arc to the cap and floor the rest; a weighted-random sample must land on it far
    # more than the uniform 1/10 baseline, and it must be the modal pick.
    for j in journeys.JOURNEYS:
        if j != "energy_arc":
            store.set_weight(f"journey:{j}", 0.2)
    store.set_weight("journey:energy_arc", 3.0)
    from collections import Counter
    picks = [recommend.roll_recipe(store, "comfort", seed=s, now=1.0)["journey"] for s in range(60)]
    c = Counter(picks)
    assert c.most_common(1)[0][0] == "energy_arc"     # the boosted journey dominates
    assert c["energy_arc"] >= 30                       # >> uniform (~6); robust given p~0.625, n=60
