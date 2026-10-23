import inspect
import html
import json
import re
from yt_playlist.web.routes import home


def test_home_logs_ranker_on_impressions_and_recipe(store):
    from tests.test_mode_cards_route import _seed_bundles, _client
    _seed_bundles(store)
    response = _client(store).get("/home/cards")
    recipes = [json.loads(html.unescape(raw)) for raw in re.findall(r"data-recipe='([^']*)'", response.text)]
    expected = {recipe["lane"]: (recipe["mode_id"], recipe["ranker"])
                for recipe in recipes if recipe.get("model") == "mode"}
    assert expected
    assert {row["lane"]: (row["mode_id"], row["ranker"]) for row in
            store.conn.execute("SELECT lane,mode_id,ranker FROM rec_mode_impressions")} == expected
    # the pick reads the ranker back out of the recipe
    assert 'ranker=recipe.get("ranker")' in inspect.getsource(home)
