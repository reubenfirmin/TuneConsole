import inspect
from yt_playlist.web.routes import home


def test_home_logs_ranker_on_impressions_and_recipe():
    src = inspect.getsource(home)
    # impressions carry the per-card ranker
    assert '(c["lane"], c["mode_id"], c["ranker"])' in src
    # the recipe round-trips the ranker so the pick can attribute it
    assert '"ranker": c["ranker"]' in src
    # the pick reads the ranker back out of the recipe
    assert 'ranker=recipe.get("ranker")' in src
