import copy

import pytest

from tests.test_home_other_genres import offered, seed_genres
from tests.test_home_tuning import client_at
from tests.conftest import seed_catalog_track
from tests.musical_support import prepare_profiles, prepare_vibes
from yt_playlist.rec import home_vibe, mix_tuning, mode_surfaces, musical_character, musical_profiles, rec_params


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
    store.settings.set_setting(home_vibe.SETTING, preset)
    store.settings.set_setting(mix_tuning.UPDATED_AT_SETTING, "1000")
    for key, audio in [("preferred", preferred), ("other", other)]:
        store.discovery.upsert_discovered_track(key, key, key, key, "", None, None, None, None, 1, audio=audio)
    prepare_profiles(store)
    items = [{"key": "other"}, {"key": "preferred"}]
    before = copy.deepcopy(items)
    assert home_vibe.rank(store, 1000, items)[0]["key"] == "preferred"
    assert items == before


def test_missing_audio_preserves_taste_and_does_not_invent_mood():
    items = [{"key": "a"}, {"key": "b"}]
    assert home_vibe.affinity(items[0], "gentle", {}) == .5
    assert home_vibe.affinity({"key": "a", "genre": "Ambient"}, "gentle", {}) == .5
    ambient = {"key": "a", "musical": musical_character.compact(musical_profiles.resolve([], genre='Ambient'))}
    assert home_vibe.affinity(ambient, 'gentle') > .5
    # Weak genre evidence does not assert vocals or manufacture a strong fit.
    assert home_vibe.affinity(ambient, "focus", {}) < .6
    assert home_vibe.affinity(items[0], "gentle", {"a": {"energy": float("nan")}}) == .5


def test_library_audio_takes_precedence_and_keeps_extra_discovery_features(store):
    from yt_playlist.util.matching import identity_key

    key = identity_key("Song", "Artist")
    store.discovery.upsert_discovered_track(key, "v", "Song", "Artist", "", None, None, None, None, 1,
                                 audio={"energy": .9, "mood_relaxed": .8})
    tid = store.tracks.upsert_track("v", "Song", "Artist", None, None)
    store.tracks.set_track_audio(tid, energy=.1)
    prepare_profiles(store)
    profile = musical_character.load(store, [key])[key]
    assert profile['dimensions']['intensity']['value'] < .5
    assert profile['dimensions']['relaxed']['value'] == .8


def test_vibe_changes_offered_styles_as_well_as_track_order(store):
    seed_sounds(store, by_mode=True)
    client = client_at(store, [1000.0])
    client.post("/home/vibe", data={"vibe": "gentle"})
    gentle = mode_surfaces.assemble_cards(store, 1000, 0)
    client.post("/home/vibe", data={"vibe": "boost"})
    boost = mode_surfaces.assemble_cards(store, 1000, 0)
    assert {t['genre'] for c in gentle for t in c['tracks']} != {t['genre'] for c in boost for t in c['tracks']}
    assert all(c['mode_id'] is None for c in [*gentle, *boost])


def seed_sounds(store, *, by_mode=False):
    seed_genres(store)
    bundles = store.rec_proposals.get_proposals("mode_bundles")
    for mid in range(6):
        for lane, items in bundles[str(mid)].items():
            for i, item in enumerate(items):
                item["artist"] = f"Artist {mid} {i}"
                # Put the soft tracks beyond a full card, so a post-selection reordering fails.
                energy = mid / 5 if by_mode else .1 if i >= 12 else .9
                item['video_id'] = item['key']
                audio = {'energy': energy, 'bpm': 70 if i >= 12 else 160, 'mood_relaxed': 1 - energy}
                if lane == 'fresh':
                    store.discovery.upsert_discovered_track(item['key'], item['video_id'], item['title'], item['artist'],
                        '', None, item['genre'], None, None, 1, audio=audio)
                else:
                    seed_catalog_track(store, item['key'], item['title'], item['artist'], video_id=item['video_id'],
                                       genre=item['genre'], **audio)
    bundles['all'] = {lane: [item for mid in range(6) for item in bundles[str(mid)][lane]]
                      for lane in mode_surfaces.CARD_SURFACES}
    store.rec_proposals.put_proposals("mode_bundles", bundles, 1)
    prepare_vibes(store)


def test_vibe_changes_rendered_tracks_persists_and_expires_without_learning(store):
    seed_sounds(store)
    clock = [1000.0]
    client = client_at(store, clock)
    before = store.rec_proposals.get_proposals("mode_bundles")
    learned = store.rec_model.get_weights()
    response = client.post("/home/vibe", data={"vibe": "gentle"})
    assert response.status_code == 200
    assert 'data-vibe="gentle"' in response.text
    assert '"vibe": "gentle"' in response.text
    assert response.headers["X-Mix-Tuning-Active"] == "true"
    cards = mode_surfaces.assemble_cards(store, clock[0], 0)
    assert cards
    assert all(sum(musical_character.intensity(t) < .5 for t in card['tracks']) >= 8 for card in cards)
    assert store.rec_model.get_weights() == learned and store.rec_model.get_leans() == {}
    assert store.rec_proposals.get_proposals("mode_bundles") == before
    assert 'data-vibe="gentle"' in client.get("/home/cards").text
    assert 'data-vibe="gentle"' in client.post("/home/refresh-cards").text

    clock[0] += mix_tuning.TTL_S
    response = client.get("/home/fingerprint")
    assert 'data-vibe="gentle"' not in response.text
    assert store.settings.get_setting(home_vibe.SETTING) is None
    assert store.rec_model.get_weights() == learned


def test_genre_requests_restrict_vibes_and_survive_clear(store):
    seed_sounds(store)
    client = client_at(store, [1000.0])
    client.post("/home/mix-genres", data={"genres": "jazz"})
    client.post("/home/steer", data={"axis": "genre:jazz", "weight": "1.5"})
    response = client.post("/home/steer", data={"axis": "genre:techno", "weight": "2"})
    assert offered(response) == {"jazz"}  # A dial cannot expand the explicit genre focus.
    assert store.rec_model.get_lean("genre:techno") == 2
    response = client.post("/home/vibe", data={"vibe": "focus"})
    assert 'data-requested-genres=\'["jazz"]\'' in response.text
    assert store.rec_model.get_lean("genre:jazz") == 1.5
    assert offered(response) == {'jazz'}
    assert store.rec_proposals.get_proposals('home_genre_request')['include_genres'] == ['jazz']
    # Vibe rotations keep the explicit genre restriction until clear/expiry.
    store.rec_ledger.refresh_card('cards', max(1, rec_params.get_param(store, 'erosion_view_cap')), 1000)
    assert store.rec_proposals.get_proposals('home_genre_request')['epoch'] != mix_tuning.card_epoch(store)
    assert offered(client.get('/home/cards')) == {'jazz'}
    response = client.post("/home/vibe", data={"vibe": ""})
    assert 'data-vibe=""' in response.text
    assert store.settings.get_setting(home_vibe.SETTING) is None
    assert store.rec_model.get_lean("genre:jazz") == 1.5
    assert offered(response) == {'jazz'}
    client.post("/home/vibe", data={"vibe": "move"})
    client.post("/home/fingerprint/reset")
    assert store.settings.get_setting(home_vibe.SETTING) is None


def test_invalid_vibe_does_not_change_controls_or_deadline(store):
    client = client_at(store, [1000.0])
    assert client.post("/home/vibe", data={"vibe": "unknown"}).status_code == 400
    assert store.settings.get_setting(home_vibe.SETTING) is None
    assert mix_tuning.remaining(store, 1000) == 0
