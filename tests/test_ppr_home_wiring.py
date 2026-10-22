import inspect
from yt_playlist.rec import home_cards
from yt_playlist.web.routes import home


def test_home_logs_ranker_on_impressions_and_recipe():
    src = inspect.getsource(home_cards)
    # impressions carry the per-card ranker
    assert '(c["lane"], c["mode_id"], c["ranker"])' in src
    # the recipe round-trips the ranker so the pick can attribute it
    assert '"ranker": card["ranker"]' in src
    # the pick reads the ranker back out of the recipe
    assert 'ranker=recipe.get("ranker")' in inspect.getsource(home)
