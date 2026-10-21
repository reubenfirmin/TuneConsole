import copy

import pytest

from tests.test_home_other_genres import offered, seed_genres
from tests.test_home_tuning import client_at
from yt_playlist.rec import home_vibe, mix_tuning, mode_surfaces


@pytest.mark.parametrize("preset,preferred,other", [
    ("gentle", {"energy": .1, "bpm": 75, "mood_relaxed": .9, "loudness": .2},
               {"energy": .9, "bpm": 160, "mood_relaxed": .1, "loudness": .95}),
    ("boost", {"energy": .9, "bpm": 160, "mood_happy": .8},
              {"energy": .1, "bpm": 75, "mood_happy": .2}),
    ("focus", {"instrumental": .95, "mood_relaxed": .8},
              {"instrumental": .05, "mood_relaxed": .2}),
    ("bright", {"mood_happy": .9, "mood_sad": .1},
               {"mood_happy": .1, "mood_sad": .9}),
    ("unwind", {"mood_acoustic": .9, "mood_relaxed": .9, "energy": .1},
               {"mood_acoustic": .1, "mood_relaxed": .1, "energy": .9}),
    ("move", {"danceability": .9, "energy": .8, "bpm": 130},
             {"danceability": .1, "energy": .2, "bpm": 75}),
])
def test_vibes_favor_their_measured_sound_before_selection(store, preset, preferred, other):
    store.set_setting(home_vibe.SETTING, preset)
    store.set_setting(mix_tuning.UPDATED_AT_SETTING, "1000")
    for key, audio in [("preferred", preferred), ("other", other)]:
        store.upsert_discovered_track(key, key, key, key, "", None, None, None, None, 1, audio=audio)
    items = [{"key": "other"}, {"key": "preferred"}]
    before = copy.deepcopy(items)
    assert home_vibe.rank(store, 1000, items)[0]["key"] == "preferred"
    assert items == before


def test_missing_audio_preserves_taste_and_does_not_invent_mood():
    items = [{"key": "a"}, {"key": "b"}]
    assert home_vibe.affinity(items[0], "gentle", {}) == .5
    assert home_vibe.affinity({"key": "a", "genre": "Ambient"}, "gentle", {}) > .5
    # Ambient has no inferred vocals or happiness. Only its energy prior contributes.
    ambient = {"key": "a", "genre": "Ambient"}
    assert home_vibe.affinity(ambient, "focus", {}) < .6
    assert home_vibe.affinity(items[0], "gentle", {"a": {"energy": float("nan")}}) == .5


def test_library_audio_takes_precedence_and_keeps_extra_discovery_features(store):
    from yt_playlist.util.matching import identity_key

    key = identity_key("Song", "Artist")
    store.upsert_discovered_track(key, "v", "Song", "Artist", "", None, None, None, None, 1,
                                 audio={"energy": .9, "mood_relaxed": .8})
    tid = store.upsert_track("v", "Song", "Artist", None, None)
    store.set_track_audio(tid, energy=.1)
    assert home_vibe.audio_features(store)[key] == {"energy": .1, "mood_relaxed": .8}


def test_vibe_changes_offered_styles_as_well_as_track_order(store):
    seed_genres(store)
    bundles = store.get_proposals("mode_bundles")
    for mid in range(6):
        for items in bundles[str(mid)].values():
            for item in items:
                store.upsert_discovered_track(item["key"], item["key"], item["title"], item["artist"], "", None,
                    item["genre"], None, None, 1, audio={"energy": mid / 5, "bpm": 60 + mid * 24})
    client = client_at(store, [1000.0])
    client.post("/home/vibe", data={"vibe": "gentle"})
    gentle = mode_surfaces.assemble_cards(store, 1000, 0)
    client.post("/home/vibe", data={"vibe": "boost"})
    boost = mode_surfaces.assemble_cards(store, 1000, 0)
    assert {c["mode_id"] for c in gentle} == {0, 1, 2, 3}
    assert {c["mode_id"] for c in boost} == {2, 3, 4, 5}


def seed_sounds(store):
    seed_genres(store)
    bundles = store.get_proposals("mode_bundles")
    for mid in range(6):
        for lane, items in bundles[str(mid)].items():
            for i, item in enumerate(items):
                item["artist"] = f"Artist {mid} {i}"
                # Put the soft tracks beyond a full card, so a post-selection reordering fails.
                energy = .1 if i >= 12 else .9
                store.upsert_discovered_track(item["key"], item["key"], item["title"], item["artist"], "", None,
                    item["genre"], None, None, 1,
                    audio={"energy": energy, "bpm": 70 if i >= 12 else 160, "mood_relaxed": 1 - energy})
    store.put_proposals("mode_bundles", bundles, 1)


def test_vibe_changes_rendered_tracks_persists_and_expires_without_learning(store):
    seed_sounds(store)
    clock = [1000.0]
    client = client_at(store, clock)
    before = store.get_proposals("mode_bundles")
    learned = store.get_weights()
    response = client.post("/home/vibe", data={"vibe": "gentle"})
    assert response.status_code == 200
    assert 'data-vibe="gentle"' in response.text
    assert '"vibe": "gentle"' in response.text
    assert response.headers["X-Mix-Tuning-Active"] == "true"
    cards = mode_surfaces.assemble_cards(store, clock[0], 0)
    audio = home_vibe.audio_features(store)
    assert all(sum(audio[t["key"]]["energy"] < .2 for t in card["tracks"]) >= 8 for card in cards)
    assert store.get_weights() == learned and store.get_leans() == {}
    assert store.get_proposals("mode_bundles") == before
    assert 'data-vibe="gentle"' in client.get("/home/cards").text
    assert 'data-vibe="gentle"' in client.post("/home/refresh-cards").text

    clock[0] += mix_tuning.TTL_S
    response = client.get("/home/fingerprint")
    assert 'data-vibe="gentle"' not in response.text
    assert store.get_setting(home_vibe.SETTING) is None
    assert store.get_weights() == learned


def test_vibe_composes_with_genre_requests_and_can_be_cleared_independently(store):
    seed_sounds(store)
    client = client_at(store, [1000.0])
    client.post("/home/mix-genres", data={"genres": "jazz"})
    client.post("/home/steer", data={"axis": "genre:jazz", "weight": "1.5"})
    response = client.post("/home/steer", data={"axis": "genre:techno", "weight": "2"})
    assert offered(response) == {"jazz"}  # A dial cannot expand the explicit genre focus.
    assert store.get_lean("genre:techno") == 2
    response = client.post("/home/vibe", data={"vibe": "focus"})
    assert 'data-requested-genres=\'["jazz"]\'' in response.text
    assert store.get_lean("genre:jazz") == 1.5
    assert offered(response) == {"jazz"}
    response = client.post("/home/vibe", data={"vibe": ""})
    assert 'data-vibe=""' in response.text
    assert store.get_setting(home_vibe.SETTING) is None
    assert store.get_lean("genre:jazz") == 1.5
    client.post("/home/vibe", data={"vibe": "move"})
    client.post("/home/fingerprint/reset")
    assert store.get_setting(home_vibe.SETTING) is None


def test_invalid_vibe_does_not_change_controls_or_deadline(store):
    client = client_at(store, [1000.0])
    assert client.post("/home/vibe", data={"vibe": "unknown"}).status_code == 400
    assert store.get_setting(home_vibe.SETTING) is None
    assert mix_tuning.remaining(store, 1000) == 0
