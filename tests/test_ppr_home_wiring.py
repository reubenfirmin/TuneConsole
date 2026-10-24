import html
import json
import re


def test_home_logs_ranker_on_impressions_and_recipe(store):
    from tests.test_mode_cards_route import _seed_bundles, _client
    _seed_bundles(store)
    client = _client(store)
    response = client.get("/home/cards")
    recipes = [json.loads(html.unescape(raw)) for raw in re.findall(r"data-recipe='([^']*)'", response.text)]
    expected = {recipe["lane"]: (recipe["mode_id"], recipe["ranker"])
                for recipe in recipes if recipe.get("model") == "mode"}
    assert expected
    assert {row["lane"]: (row["mode_id"], row["ranker"]) for row in
            store.conn.execute("SELECT lane,mode_id,ranker FROM rec_mode_impressions")} == expected
    # Saving the served recipe must retain its ranker in the actual pick record,
    # regardless of which route module owns playlist generation.
    recipe = next(recipe for recipe in recipes if recipe.get("model") == "mode")
    track = dict(store.conn.execute(
        "SELECT video_id,title,artist FROM catalog_tracks WHERE video_id IS NOT NULL LIMIT 1").fetchone())
    saved = client.post("/home/generate", data={
        "name": "Ranker attribution", "tracks": json.dumps([track]), "recipe": json.dumps(recipe)})
    assert saved.status_code == 200 and "YouTube returned an unexpected response" not in saved.text
    pick, = store.conn.execute("SELECT mode_id,ranker FROM rec_mode_picks")
    assert (pick["mode_id"], pick["ranker"]) == (recipe["mode_id"], recipe["ranker"])
