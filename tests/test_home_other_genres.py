import html
import json
import re

import numpy as np

from tests.test_mode_cards_route import _client
from yt_playlist.rec import home_themes, mode_surfaces
from yt_playlist.util import genre_map
from yt_playlist.util.matching import identity_key
from yt_playlist.web.routes import home


def seed_genres(store):
    genres = ["Jazz", "Techno", "Trance", "Indie Rock", "Ambient", "Classical"]
    store.modes.replace_modes([
        {"mode_id": mid, "label": genre, "families": [[genre_map.family(genre), 80]],
         "centroid": np.eye(len(genres), dtype=np.float32)[mid], "size": 80, "rep_keys": []}
        for mid, genre in enumerate(genres)], retired_ids=[], now=1.0)
    bundles = {"all": {}, "_meta": {"comfort_pool": 100, "year_cuts": None}}
    for mid, genre in enumerate(genres):
        bundles[str(mid)] = {}
        for lane in mode_surfaces.CARD_SURFACES:
            items = [{"key": f"{lane}-{mid}-{i}|artist{i}", "video_id": "v", "title": f"Song {mid} {i}",
                      "artist": f"Artist {i}", "album": "", "thumbnail": None, "genre": genre,
                      "plays": 0, "reason": "", "lane": lane} for i in range(20)]
            bundles[str(mid)][lane] = items
            bundles["all"].setdefault(lane, []).extend(items)
    store.put_proposals("mode_bundles", bundles, 1.0)


def offered(response):
    return {g for value in re.findall(r"data-genres='([^']*)'", response.text)
            for g in json.loads(html.unescape(value))}


def test_other_genres_changes_actual_themes_without_changing_taste(store):
    seed_genres(store)
    client = _client(store)
    before = client.get("/home/cards")
    current = offered(before)
    assert len(current) == 4
    bundles = store.get_proposals("mode_bundles")
    weights, leans = store.get_weights(), store.get_leans()
    response = client.post("/home/other-genres", data={"genres": json.dumps(sorted(current))})
    assert response.status_code == 200
    assert len(offered(response)) == 2
    assert not offered(response) & current
    assert "Different genres for this set" in response.text
    assert "genre_request" in response.text  # saved recipe and diagnostic audit carry the request
    assert store.get_weights() == weights and store.get_leans() == leans
    assert store.get_proposals("mode_bundles") == bundles
    assert home._epoch(store, "cards") == 1

    # Re-fetching and steering preserve this menu's request. A normal refresh ends it.
    assert offered(client.get("/home/cards")) == offered(response)
    preview = client.post("/home/breadth", data={"breadth_bias": "0.5"})
    assert offered(preview) == offered(response)
    regular = client.post("/home/refresh-cards")
    assert len(offered(regular)) == 4
    assert "genre_request" not in regular.text


def test_no_alternative_genres_keeps_usable_mixes_and_explains(store):
    seed_genres(store)
    response = _client(store).post("/home/other-genres", data={
        "genres": json.dumps(["Jazz", "Techno", "Trance", "Indie Rock", "Ambient", "Classical"])})
    assert response.status_code == 200
    assert "No other genre themes are ready yet" in response.text
    assert len(offered(response)) == 4


def test_invalid_genre_request_does_not_advance_rotation(store):
    client = _client(store)
    for value in ["broken", "null", '"jazz"', "[]", "[4]", '[""]', "{}"]:
        assert client.post("/home/other-genres", data={"genres": value}).status_code == 400
    assert home._epoch(store, "cards") == 0
    assert store.get_proposals("home_genre_request") is None


def test_genre_filter_uses_families_and_artist_fallback(store, monkeypatch):
    monkeypatch.setattr(store, "artist_genre_years", lambda: {"A": {"genre": "Jazz"}})
    items = [{"key": "known", "genre": "Jazz"}, {"key": "inferred", "artist": "A"},
             {"key": "unknown"}, {"key": "new", "genre": "Techno"}]
    assert home_themes.other_genres(store, items, {genre_map.family("Jazz")}) == [items[-1]]


def test_other_genres_never_backfills_the_previous_genres(store):
    seed_genres(store)
    bundles = store.get_proposals("mode_bundles")
    for mid in range(1, 6):
        bundles[str(mid)] = {}
    # Thin new-genre buckets used to be topped up with arbitrary general-pool genres.
    for lane in mode_surfaces.CARD_SURFACES:
        bundles["0"][lane] = bundles["0"][lane][:4]
    store.put_proposals("mode_bundles", bundles, 1.0)
    avoid = {genre_map.family(g) for g in ["Techno", "Trance", "Indie Rock", "Ambient", "Classical"]}
    cards = mode_surfaces.assemble_cards(store, 1000, 1, avoid_genres=avoid)
    assert cards
    assert all(t["genre"] == "Jazz" for c in cards for t in c["tracks"])


def test_other_genres_does_not_attribute_a_rethemed_card_to_the_old_mode(store):
    seed_genres(store)
    bundles = store.get_proposals("mode_bundles")
    # The jazz-led mode has enough techno in its bucket to survive a track-only filter.
    # Showing those leftovers under the jazz mode would teach the wrong theme on a pick.
    for lane in mode_surfaces.CARD_SURFACES:
        bundles["0"][lane] += bundles["1"][lane]
    store.put_proposals("mode_bundles", bundles, 1.0)
    cards = mode_surfaces.assemble_cards(store, 1000, 1, avoid_genres={genre_map.family("Jazz")})
    assert cards and all(c["mode_id"] != 0 for c in cards)
    assert all(t["genre"] != "Jazz" for c in cards for t in c["tracks"])


def test_other_genres_uses_current_library_genres_before_rendering(store):
    seed_genres(store)
    bundles = store.get_proposals("mode_bundles")
    # The worker cached Techno; enrichment has since corrected these tracks to Jazz.
    # Leave other alternatives available so this must succeed without the usual-mix fallback.
    for lane, items in bundles["1"].items():
        for i, item in enumerate(items):
            item["title"] = f"{lane} song {i}"
            item["key"] = identity_key(item["title"], item["artist"])
            tid = store.upsert_track(f"{lane}-{i}", item["title"], item["artist"], None, None)
            store.set_track_genre(tid, "Jazz")
        # General backfill carries the same stale rows as the mode bucket.
        bundles["all"][lane] = list(items)
    store.put_proposals("mode_bundles", bundles, 1.0)
    client = _client(store)

    response = client.post("/home/other-genres", data={
        "genres": json.dumps(["Jazz", "Trance", "Indie Rock", "Classical"])})

    assert response.status_code == 200
    assert "Different genres for this set" in response.text
    assert offered(response) == {"ambient"}
    assert "jazz" not in offered(client.get("/home/cards"))
    assert store.get_proposals("mode_bundles") == bundles


def test_genre_filter_resolves_library_metadata_without_mutating_candidates(store):
    tid = store.upsert_track("v", "Song", "Artist", None, None)
    store.set_track_genre(tid, "Techno")
    items = [{"key": identity_key("Song", "Artist"), "genre": "Jazz"}]

    result = home_themes.other_genres(store, items, {genre_map.family("Jazz")})

    assert len(result) == 1 and result[0]["genre"] == "Techno"
    assert items[0]["genre"] == "Jazz"


def test_chosen_genres_constrain_tracks_and_expire_without_changing_taste(store):
    seed_genres(store)
    client = _client(store)
    weights, leans = store.get_weights(), store.get_leans()
    bundles = store.get_proposals("mode_bundles")
    response = client.post("/home/mix-genres", data={"genres": ["jazz", "techno"]})

    assert response.status_code == 200
    assert offered(response) == {"jazz", "techno"}
    assert "Your chosen genres" in response.text
    assert '"kind": "chosen_genres"' in response.text
    assert store.get_weights() == weights and store.get_leans() == leans
    assert store.get_proposals("mode_bundles") == bundles
    assert offered(client.get("/home/cards")) == offered(response)
    assert offered(client.post("/home/breadth", data={"breadth_bias": "0.5"})) == offered(response)
    regular = client.post("/home/refresh-cards")
    assert len(offered(regular)) == 4
    assert '"kind": "chosen_genres"' not in regular.text


def test_chosen_genres_filter_backfill_and_keep_mode_attribution(store):
    seed_genres(store)
    bundles = store.get_proposals("mode_bundles")
    for lane in mode_surfaces.CARD_SURFACES:
        # A jazz-led mode contains techno; a techno-led mode needs general backfill.
        bundles["0"][lane] += bundles["1"][lane]
        bundles["1"][lane] = bundles["1"][lane][:4]
    store.put_proposals("mode_bundles", bundles, 1.0)
    cards = mode_surfaces.assemble_cards(store, 1000, 1, include_genres={"techno"})
    assert cards
    assert all(c["mode_id"] == 1 for c in cards)
    assert all(t["genre"] == "Techno" for c in cards for t in c["tracks"])


def test_chosen_genres_validate_before_advancing_the_menu(store):
    seed_genres(store)
    client = _client(store)
    for genres in [[], [""], ["not-available"], ["jazz", "not-available"], ["jazz"] * 101]:
        assert client.post("/home/mix-genres", data={"genres": genres}).status_code == 400
    assert home._epoch(store, "cards") == 0
    assert store.get_proposals("home_genre_request") is None


def test_chosen_genres_explain_when_the_pool_cannot_fill_a_mix(store):
    seed_genres(store)
    bundles = store.get_proposals("mode_bundles")
    for lane in mode_surfaces.CARD_SURFACES:
        bundles["0"][lane] = bundles["0"][lane][:1]
        bundles["all"][lane] = [t for t in bundles["all"][lane] if t["genre"] != "Jazz"]
    store.put_proposals("mode_bundles", bundles, 1.0)
    response = _client(store).post("/home/mix-genres", data={"genres": ["jazz"]})
    assert response.status_code == 200
    assert "Not enough tracks for those genres yet" in response.text
    assert offered(response)
    assert '"kind": "chosen_genres"' not in response.text


def test_available_genres_follow_current_metadata_and_artist_fallback(store, monkeypatch):
    tid = store.upsert_track("v", "Song", "Artist", None, None)
    store.set_track_genre(tid, "Techno")
    monkeypatch.setattr(store, "artist_genre_years", lambda: {"A": {"genre": "Jazz"}})
    items = [{"key": identity_key("Song", "Artist"), "genre": "Ambient"},
             {"key": "inferred", "artist": "A"}, {"key": "unknown"}]
    store.put_proposals("mode_bundles", {"all": {"wheelhouse": items}}, 1.0)
    assert home_themes.genre_options(store) == [
        {"family": "techno", "label": "techno"}, {"family": "jazz", "label": "jazz"}]
    assert [item["key"] for item in home_themes.matching_genres(store, items, include={"jazz"})] == ["inferred"]
    assert items[0]["genre"] == "Ambient"


def test_available_genres_prioritize_listening_over_alphabet_and_candidate_volume(store):
    seed_genres(store)
    iid = store.upsert_identity("main", "cred", None, True)
    for genre, count in {"Trance": 100, "Techno": 60, "Jazz": 20, "Ambient": 1}.items():
        tid = store.upsert_track(genre, genre, "Artist", None, None)
        store.set_track_genre(tid, genre)
        store.add_history_snapshot(iid, 1.0, [identity_key(genre, "Artist")] * count)
    options = home_themes.genre_options(store)
    assert [option["label"] for option in options[:3]] == ["trance", "techno", "jazz"]
