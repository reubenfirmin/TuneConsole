import copy
from tests.enrichment_support import seed_audio

import pytest

from tests.conftest import seed_catalog_track
from tests.test_home_other_genres import genre_bundles, offered
from tests.test_home_tuning import home_clients, prepare_tuning_home
from tests.musical_support import prepare_profiles, prepare_vibes
from tests.recommendation_support import publish_bundles
from yt_playlist.providers.base import MetadataObservation
from yt_playlist.rec import home_vibe, mix_tuning, mode_surfaces, musical_character, musical_profiles, rec_params


@pytest.fixture
def prepared_client(store, home_clients):
    def open_client(clock):
        prepare_tuning_home(store, clock[0])
        return home_clients(clock)

    return open_client


@pytest.fixture(autouse=True)
def established_vibe_access(monkeypatch):
    # This module checks Vibes' ranking and tuning behavior after access has
    # opened. Real menu capacity, stale evidence and locked requests are
    # exercised without readiness overrides in test_growth_readiness and
    # test_growth_journey.
    from yt_playlist.rec import account_growth
    monkeypatch.setattr(account_growth, 'vibe_access', lambda *_args, **_kwargs: True)

    monkeypatch.setattr(account_growth, 'view', lambda *_args, **_kwargs: {
        'vibes_ready': True, 'ready_areas': {'directions': True},
        'ready_formats': {'mixes': True}})


def test_saved_vibe_requires_a_configured_access_policy(store, monkeypatch):
    store.settings.set_setting(home_vibe.SETTING, 'relax')
    store.settings.set_setting(mix_tuning.UPDATED_AT_SETTING, '1000')
    monkeypatch.setattr(home_vibe, '_access', None)
    with pytest.raises(RuntimeError, match='access policy'):
        home_vibe.current(store, 1000)
    # Natural readiness never applies the remembered control or invokes a gate.
    with home_vibe.natural():
        assert home_vibe.current(store, 1000) == ''


@pytest.mark.parametrize("preset,preferred,other", [
    ("relax", {"energy": .1, "bpm": 75, "mood_relaxed": .9, "loudness": .2},
               {"energy": .9, "bpm": 160, "mood_relaxed": .1, "loudness": .95}),
    ("concentrate", {"instrumental": .95, "energy": .3, "mood_relaxed": .8},
                    {"instrumental": .05, "energy": .9, "mood_relaxed": .1}),
    ("cruise", {"energy": .5, "mood_relaxed": .6}, {"energy": .95, "mood_relaxed": .6}),
    ("energize", {"energy": .9, "bpm": 75, "mood_happy": .1},
              {"energy": .1, "bpm": 160, "mood_happy": .9}),
    ("release", {"mood_aggressive": .95, "energy": .2, "bpm": 65},
                {"mood_aggressive": .05, "energy": .9, "bpm": 160}),
    ("uplift", {"mood_happy": .9, "energy": .2}, {"mood_happy": .1, "energy": .9}),
])
def test_vibes_favor_their_measured_sound_before_selection(file_backed_store, monkeypatch, preset, preferred, other):
    store = file_backed_store
    # This two-track unit exercises ranking; the integrated journey checks
    # establish when a complete four-card Vibes menu may be exposed.
    monkeypatch.setattr(home_vibe, 'current', lambda _store, _now: preset)
    store.settings.set_setting(home_vibe.SETTING, preset)
    store.settings.set_setting(mix_tuning.UPDATED_AT_SETTING, "1000")
    for key, audio in [("preferred", preferred), ("other", other)]:
        store.discovery.upsert_discovered_track(key, key, key, key, "", None, None, None, None, 1, audio=audio)
        if 'mood_aggressive' in audio:
            # Aggression is retained source evidence rather than a legacy
            # catalog audio column. Exercise its actual preparation path.
            entry_id = store.conn.execute('SELECT id FROM catalog_tracks WHERE video_id=?', (key,)).fetchone()[0]
            store.enrichment_state.record_metadata_observations(entry_id, 'fixture', [MetadataObservation(
                'mood_aggressive', audio['mood_aggressive'], 'recording', key, 'prediction',
                match_evidence={'method': 'supplied_recording_mbid'})], run_id=key, now=1)
    prepare_profiles(store)
    items = [{"key": "other"}, {"key": "preferred"}]
    before = copy.deepcopy(items)
    assert home_vibe.rank(store, 1000, items)[0]["key"] == "preferred"
    assert items == before


def test_missing_audio_preserves_taste_and_does_not_invent_mood():
    items = [{"key": "a"}, {"key": "b"}]
    assert home_vibe.affinity(items[0], "relax", {}) == .5
    assert home_vibe.affinity({"key": "a", "genre": "Ambient"}, "relax", {}) == .5
    ambient = {"key": "a", "musical": musical_character.compact(musical_profiles.resolve([], genre='Ambient'))}
    assert home_vibe.affinity(ambient, 'relax') > .5
    # Weak genre evidence does not manufacture force or a strong fit.
    assert home_vibe.affinity(ambient, "release", {}) < .6
    assert home_vibe.affinity(items[0], "relax", {"a": {"energy": float("nan")}}) == .5
    assert home_vibe.affinity(items[0], "cruise", {}) == .5


def test_library_audio_takes_precedence_and_keeps_extra_discovery_features(store):
    from yt_playlist.util.matching import identity_key

    key = identity_key("Song", "Artist")
    store.discovery.upsert_discovered_track(key, "v", "Song", "Artist", "", None, None, None, None, 1,
                                 audio={"energy": .9, "mood_relaxed": .8})
    tid = store.tracks.upsert_track("v", "Song", "Artist", None, None)
    seed_audio(store, tid, energy=.1)
    prepare_profiles(store)
    profile = musical_character.load(store, [key])[key]
    assert profile['dimensions']['intensity']['value'] < .5
    assert profile['dimensions']['relaxed']['value'] == .8


def test_mix_journey_uses_profiles_and_keeps_visible_choice_after_preparation_changes(store, monkeypatch):
    from yt_playlist.rec import choice_evidence, home_cards, home_prepared

    tid = store.tracks.upsert_track('measured', 'Measured', 'Artist', '', 180)
    seed_audio(store, tid, energy=.9)
    key = store.tracks.identity_key_for_video('measured')
    prepare_profiles(store)
    profile = musical_character.load(store, [key])[key]
    # A prepared selection can predate the latest profile publication.
    older = copy.deepcopy(profile)
    older['version'], older['digest'] = 'historical-profile', 'historical-digest'
    older['dimensions']['intensity']['value'] = .1
    items = [{'key': key, 'video_id': 'measured'},
             {'key': 'older', 'video_id': 'older', 'musical': older},
             {'key': 'absent', 'video_id': 'absent'}]
    ordered = home_cards._order_by_journey(store, items, 'energy_arc', 1)
    served = {item['key']: item for item in ordered}
    assert served[key]['musical'] == profile
    assert served[key]['serving_context']['musical_digest'] == profile['digest']
    assert served['older']['musical'] == older
    assert served['older']['serving_context']['intensity'] == .1
    assert served['absent']['serving_context']['intensity'] is None
    assert served['absent']['serving_context']['musical_digest'] is None
    oid = choice_evidence.issue(store, 'mixes', [{'id': 'chosen', 'tracks': ordered,
        'label': 'Energy arc', 'intent': 'energy_arc', 'genres': ['House'],
        'recipe': {'vibe': None, 'vibe_policy': home_vibe.VERSION,
                   'musical_version': musical_character.VERSION}}], 1000, context={})
    captured = store.rec_ledger.evidence().get('opportunity', oid)
    seed_audio(store, tid, energy=.2)
    prepare_profiles(store, 1001)
    prior_menu_key = home_prepared.tuning_key(store, 1002)
    monkeypatch.setattr(home_vibe, 'VERSION', 'next-policy')
    assert home_prepared.tuning_key(store, 1002) != prior_menu_key
    assert choice_evidence.respond(store, oid, 'experience', 1002, option_id='chosen',
                                  details={'video_id': 'measured'})
    event = store.rec_ledger.evidence().records('response')[-1]
    assert event['opportunity_id'] == oid
    assert store.rec_ledger.evidence().get('opportunity', event['opportunity_id']) == captured
    from yt_playlist.core import mix_choices
    option = mix_choices.expand(store.conn, captured['options'][0])
    assert (option['label'], option['intent'], option['genres']) == ('Energy arc', 'energy_arc', ['House'])
    assert 'tracks' not in option and 'recipe' not in option


def test_vibe_changes_offered_styles_as_well_as_track_order(store, prepared_client):
    seed_sounds(store, by_mode=True)
    client = prepared_client([1000.0])
    assert client.post("/home/vibe", data={"vibe": "relax"}).status_code == 200
    gentle = mode_surfaces.assemble_cards(store, 1000, 0)
    assert client.post("/home/vibe", data={"vibe": "energize"}).status_code == 200
    boost = mode_surfaces.assemble_cards(store, 1000, 0)
    for menu in (gentle, boost):
        assert {card['lane'] for card in menu} == set(mode_surfaces.CARD_SURFACES)
        assert len(menu) == 4
        assert all(len(card['tracks']) == 12 for card in menu)
    assert {t['genre'] for c in gentle for t in c['tracks']} != {t['genre'] for c in boost for t in c['tracks']}
    assert all(c['mode_id'] is None for c in [*gentle, *boost])


def seed_sounds(store, *, by_mode=False):
    # Both sound patterns ingest final uploads and audio before their first
    # publication. The by-mode consumer still has six genres, four lanes and
    # twenty tracks per lane, without a discarded ordinary generation or
    # unused first Fresh recordings.
    bundles = genre_bundles(store)
    for mid in range(6):
        for lane, items in bundles[str(mid)].items():
            for i, item in enumerate(items):
                # Put the soft tracks beyond a full card, so a post-selection reordering fails.
                energy = mid / 5 if by_mode else .1 if i >= 12 else .9
                item['video_id'] = item['key']
                audio = {'energy': energy, 'bpm': 70 if i >= 12 else 160, 'mood_relaxed': 1 - energy}
                if lane == 'fresh':
                    # A new upload can resolve to a different canonical recording key.
                    item['key'] = store.discovery.upsert_discovered_track(item['key'], item['video_id'], item['title'], item['artist'],
                        '', None, item['genre'], None, None, 1, audio=audio)
                else:
                    seed_catalog_track(store, item['key'], item['title'], item['artist'],
                        video_id=item['video_id'], album='', genre=item['genre'], **audio)
    bundles['all'] = {lane: [item for mid in range(6) for item in bundles[str(mid)][lane]]
                      for lane in mode_surfaces.CARD_SURFACES}
    prepare_profiles(store)
    # Ordinary Fresh reads have a separate owner from Vibes' candidate pools.
    publish_bundles(store, bundles, 1, fresh=bundles['all']['fresh'], vibes=True)


def test_vibe_changes_rendered_tracks_persists_and_expires_without_learning(
        store, measured_home_store, monkeypatch, home_clients):
    import html
    import json
    import re
    from yt_playlist.rec import choice_evidence

    clock = [1000.0]
    client = home_clients(clock)
    before = store.recommendations.head('home')
    learned = store.rec_model.get_weights()
    response = client.post("/home/vibe", data={"vibe": "relax"})
    assert response.status_code == 200
    assert 'data-vibe="relax"' in response.text
    assert '"vibe": "relax"' in response.text
    assert response.headers["X-Mix-Tuning-Active"] == "true"
    # Mix outcomes retain the visible theme/raw genres after a later temporary
    # vibe change; #688 deliberately retires hidden recipe/profile precision.
    opportunities = [row for row in store.rec_ledger.evidence().records('opportunity')
                     if row['format'] == 'mixes']
    assert opportunities
    offered = opportunities[-1]
    rendered_genres = [json.loads(html.unescape(value)) for value in
                       re.findall(r"data-genres='([^']*)'", response.text)]
    from yt_playlist.core import mix_choices
    options = [mix_choices.expand(store.conn, option) for option in offered['options']]
    assert [option['genres'] for option in options] == [sorted(set(g)) for g in rendered_genres]
    assert all(option['label'] and option['intent'] and 'recipe' not in option
               and 'tracks' not in option for option in options)
    cards = mode_surfaces.assemble_cards(store, clock[0], 0)
    assert cards
    assert all(sum(musical_character.intensity(t) < .5 for t in card['tracks']) >= 8 for card in cards)
    assert store.rec_model.get_weights() == learned and store.rec_model.get_leans() == {}
    assert store.recommendations.head('home') == before
    assert 'data-vibe="relax"' in client.get("/home/cards").text
    assert 'data-vibe="relax"' in client.post("/home/refresh-cards").text

    monkeypatch.setattr(home_vibe, 'VERSION', home_vibe.VERSION + '/changed-policy')
    # A policy change is prepared by the worker before its new menu is served.
    publish_bundles(store, store._fixture_bundle_payload, clock[0], vibes=True)
    response = client.post('/home/vibe', data={'vibe': 'relax'})
    match = re.search(r"hx-vals='([^']*opportunity_id[^']*)'", response.text)
    assert match
    revised = choice_evidence.opportunity(store, json.loads(html.unescape(match[1]))['opportunity_id'])
    assert revised['id'] != offered['id']
    assert all('recipe' not in option and 'tracks' not in option for option in revised['options'])
    assert store.rec_ledger.evidence().get('opportunity', offered['id']) == offered

    clock[0] += mix_tuning.TTL_S
    response = client.get("/home/fingerprint")
    assert 'data-vibe="relax"' not in response.text
    assert store.settings.get_setting(home_vibe.SETTING) is None
    assert store.rec_model.get_weights() == learned
    assert store.rec_ledger.evidence().get('opportunity', offered['id']) == offered


def test_genre_requests_restrict_vibes_and_survive_clear(store, measured_home_store, home_clients):
    client = home_clients([1000.0])
    client.post("/home/mix-genres", data={"genres": "jazz"})
    client.post("/home/steer", data={"axis": "genre:jazz", "weight": "1.5"})
    response = client.post("/home/steer", data={"axis": "genre:techno", "weight": "2"})
    assert offered(response) == {"jazz"}  # A dial cannot expand the explicit genre focus.
    assert store.rec_model.get_lean("genre:techno") == 2
    response = client.post("/home/vibe", data={"vibe": "release"})
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
    assert response.headers['X-Mix-Tuning-Active'] == 'true'
    assert offered(response) == {'jazz'}
    client.post("/home/vibe", data={"vibe": "cruise"})
    client.post("/home/fingerprint/reset")
    assert store.settings.get_setting(home_vibe.SETTING) is None


def test_clearing_the_only_vibe_clears_tuning_indicator_and_deadline(
        store, measured_home_store, home_clients):
    client = home_clients([1000.0])
    client.post('/home/vibe', data={'vibe': 'relax'})
    response = client.post('/home/vibe', data={'vibe': ''})
    assert response.status_code == 200
    assert response.headers['X-Mix-Tuning-Active'] == 'false'
    assert float(response.headers['X-Mix-Tuning-Remaining']) == 0
    assert 'data-tuning-active="false"' in client.get('/home/fingerprint').text


def test_invalid_vibe_does_not_change_controls_or_deadline(store, prepared_client):
    client = prepared_client([1000.0])
    assert client.post("/home/vibe", data={"vibe": "unknown"}).status_code == 400
    assert store.settings.get_setting(home_vibe.SETTING) is None
    assert mix_tuning.remaining(store, 1000) == 0


def test_vibe_render_failure_returns_error_instead_of_replacing_ready_cards(store, monkeypatch, prepared_client):
    from yt_playlist.web.routes.home_views import HomeViews

    client = prepared_client([1000.0])

    def fail(*args, **kwargs):
        raise RuntimeError('controlled rendering failure')

    monkeypatch.setattr(HomeViews, 'card_context', fail)
    response = client.post('/home/vibe', data={'vibe': 'relax'})
    assert response.status_code == 503
    assert not response.content
    # The choice was saved; a later retry or reload can finish rendering it.
    assert store.settings.get_setting(home_vibe.SETTING) == 'relax'


@pytest.mark.parametrize('removed', ['focus', 'bright', 'unwind', 'gentle', 'boost', 'intense', 'move'])
def test_removed_presets_are_not_reinterpreted(store, removed, prepared_client):
    client = prepared_client([1000.0])
    assert client.post('/home/vibe', data={'vibe': removed}).status_code == 400
    store.settings.set_setting(home_vibe.SETTING, removed)
    assert home_vibe.current(store, 1000) == ''
