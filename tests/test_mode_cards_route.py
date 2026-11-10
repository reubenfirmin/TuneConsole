import json
import re
from html import unescape

import numpy as np
import pytest
from fastapi.testclient import TestClient
from yt_playlist.rec import home_cards, home_prepared, journeys, mode_surfaces
from yt_playlist.web.app import create_app
from tests.conftest import FakeClient
from tests.store_fixture_support import bounded_consumer_schema, uncontended_file_consumer_store as store
from tests.home_fixture_support import home_control_routes, home_routes_only, prepared_client, publish_cards, publish_menu, without_metadata_providers
from tests.mode_fixtures import seed_modes
from tests.recommendation_support import publish_bundles, seed_publication_subjects

pytestmark = pytest.mark.usefixtures('unlocked_home_controls', 'home_control_routes', 'without_metadata_providers',
                                    'native_sql_calls')


@pytest.fixture(scope='session')
def _pristine_current_schema():
    with bounded_consumer_schema((('house', 'House'), ('techno', 'Techno'))) as schema:
        yield schema


def _client(store, now=1000.0):
    iid = store.accounts.upsert_account("main", "cred", None, True)
    prepare_home(store, now)
    return TestClient(create_app(store, lambda: {iid: FakeClient()}, now_fn=lambda: now,
                                 background_work=False),
                      base_url="http://127.0.0.1")


def prepare_home(store, now=1000.0):
    """Seed eligible subjects, then prepare candidate pools before Home as the worker does."""
    if (not store.recommendations.head('home') and not store.recommendations.head('fresh')
            and store.rec_query.library_keys()):
        assert mode_surfaces.prepare_bundles(store, now) is not None
    # The worker continues bounded preparation slices. A cold slice can finish
    # its reusable inputs without yet publishing a menu, especially during
    # tuning expiry; give the fixture the same bounded continuation.
    for _ in range(3):
        if home_prepared.prepare(store, now) is not None:
            break
    else:
        raise AssertionError('Home did not publish after three preparation slices')


def _seed_bundles(store):
    store.modes.replace_modes([
        {"mode_id": 1, "label": "a", "families": [["house", 1]],
         "centroid": np.array([1.0, 0.0], dtype=np.float32), "size": 80, "rep_keys": []},
        {"mode_id": 2, "label": "b", "families": [["techno", 1]],
         "centroid": np.array([0.0, 1.0], dtype=np.float32), "size": 60, "rep_keys": []},
    ], retired_ids=[], now=1.0)
    from yt_playlist.rec import mode_surfaces as ms
    def items(p, g):
        return [{"key": f"{p}{i}", "video_id": "v", "title": f"Song {p}{i}", "artist": f"Art {p}{i}",
                 "album": "", "thumbnail": None, "plays": 0, "reason": "", "lane": "", "genre": g}
                for i in range(20)]
    payload = {"1": {}, "2": {}}
    for surf in ms.CARD_SURFACES:
        payload["1"][surf] = items(f"{surf}h", "house")
        payload["2"][surf] = items(f"{surf}t", "techno")
    # All 160 original candidates remain. Consume completed catalog ingestion
    # without replaying metadata delivery and artist inference for every row.
    subjects = [item for bucket in payload.values() for items in bucket.values() for item in items]
    seed_publication_subjects(store,
        [{**item, 'video_id': 'v' if index == 0 else 'fixture-' + item['key']}
         for index, item in enumerate(subjects)],
        discoveries=[item['key'] for bucket in payload.values() for item in bucket['fresh']])
    publish_bundles(store, payload, 1.0)
    return payload


def test_cards_route_renders_mode_cards(store, home_routes_only):
    seed_modes(store, 1, 2)
    payload = publish_menu(store, prefix='song', count=2, mode_ids=(1, 2))
    # A discovery inside a mix must not acquire individual track exposure when
    # only the mix descriptor becomes visible. Keep this assertion nonempty.
    track = payload['menus'][0]['protos'][0]['tracks'][0]
    store.discovery.upsert_discovered_track(track['key'], track['video_id'], track['title'],
        track['artist'], '', None, track['genre'], None, None, 1000)
    with prepared_client(store, background_work=False) as client:
        r = client.get("/home/cards")
        assert r.status_code == 200
        assert "Song" in r.text
        recipes = [json.loads(unescape(value)) for value in re.findall(r"data-recipe='([^']*)'", r.text)]
        assert recipes
        offers = store.rec_ledger.evidence().records('opportunity')
        assert len(offers) == 1 and offers[0]['format'] == 'mixes'
        assert {recipe['opportunity_id'] for recipe in recipes} == {offers[0]['id']}
        assert not store.rec_ledger.evidence().records('response')
        assert store.modes.impression_counts()
        # A real visibility response records the menu descriptor, without
        # attributing exposure to the tracks hidden inside that mix.
        assert client.post('/home/listening-response', json={
            'opportunity_id': recipes[0]['opportunity_id'],
            'option_id': recipes[0]['option_id'], 'kind': 'visible'}).status_code == 204
    responses = store.rec_ledger.evidence().records('response')
    assert len(responses) == 1 and responses[0]['kind'] == 'visible'
    assert responses[0]['option_id'] == recipes[0]['option_id']
    assert not store.conn.execute('SELECT 1 FROM rec_served_impressions').fetchone()
    discoveries = store.discovery.get_discovered_tracks()
    assert discoveries and all(row['offered_count'] == 0 for row in discoveries)


def test_mix_choice_capture_failure_keeps_playable_cards_and_logs(
        store, monkeypatch, caplog):
    from yt_playlist.rec import choice_evidence

    seed_modes(store, 1, 2)
    publish_menu(store, prefix='song', count=2, mode_ids=(1, 2))
    calls = []

    def unavailable(writer, format, options, now, **kwargs):
        calls.append((format, options))
        raise OSError('controlled mix-choice capture failure')

    monkeypatch.setattr(choice_evidence, 'present', unavailable)
    with prepared_client(store, background_work=False) as client:
        response = client.get('/home/cards')
    assert len(calls) == 1 and calls[0][0] == 'mixes'
    assert calls[0][1] and all(option['id'] for option in calls[0][1])
    assert response.status_code == 200
    assert 'Song' in response.text and 'class="gen-play"' in response.text
    assert re.findall(r'data-vid="([^"]+)"', response.text)
    assert 'controlled mix-choice capture failure' not in response.text
    failures = [record for record in caplog.records if record.message == 'Mix choice capture failed']
    assert len(failures) == 1
    assert isinstance(failures[0].exc_info[1], OSError)
    assert not store.conn.execute('SELECT 1 FROM rec_served_impressions').fetchone()


def _publish_mode_menu(store, now):
    # HTTP consumers use the full original pools and actual card completion.
    # Enumerating every alternative menu belongs to worker preparation tests.
    cards = mode_surfaces.assemble_cards(store, now, 0)
    protos = [home_cards._complete_card(store, card, now, 0, None, None) for card in cards]
    result = home_cards.CardsResult(protos, [{'proto': proto, 'pool': None} for proto in protos],
                                   None, None, [], [], 0, None)
    publish_cards(store, result, now)
    return cards


def test_mix_regeneration_submits_and_closes_the_requesting_views_offer(store):
    from html import unescape
    from yt_playlist.rec import choice_evidence
    _seed_bundles(store)
    _publish_mode_menu(store, 1000)
    with prepared_client(store, background_work=False) as client:
        first = client.get('/home/cards')
        match = re.search(r'hx-vals=\'([^\']*opportunity_id[^\']*)\'', first.text)
        assert match, 'Regeneration must send the ID from the rendered recipe'
        held = json.loads(unescape(match[1]))['opportunity_id']
        other = choice_evidence.present(store, 'mixes', [{'id': 'other'}], 1000,
                                       context={}, window='another-tab')
        assert client.post('/home/refresh-cards', data={'opportunity_id': held}).status_code == 200
        assert client.post('/home/refresh-cards', data={'opportunity_id': held}).status_code == 200
        assert client.post('/home/refresh-cards').status_code == 200
    closes = [row for row in store.rec_ledger.evidence().records('response') if row['kind'] == 'close']
    assert [row['opportunity_id'] for row in closes] == [held]
    assert all(row['opportunity_id'] != other for row in closes)


def test_mode_card_recipe_carries_a_journey(store):
    # A saved mode card must record a DJ journey in its recipe so the playlist page can show + tune
    # the Flow lever (parity with the per-lane _carded cards). Regression: the mode-card recipe was
    # just {"model": "mode", "mode_id": N} with no journey, so the saved playlist showed no Flow.
    seed_modes(store, 1, 2)
    # Keep real card completion and its recipe constructor, then exercise HTTP
    # serialization. Whole-menu ranking and preparation have their own owners.
    tracks = [{'key': f'song {i}|artist {i}', 'video_id': f'v{i}',
               'title': f'Song {i}', 'artist': f'Artist {i}', 'genre': 'house'}
              for i in range(4)]
    seed_publication_subjects(store, tracks)
    protos = [home_cards._complete_card(store, {
        'lane': lane, 'mode_id': mid, 'label': 'Mode mix',
        'ranker': 'cosine', 'tracks': tracks[index*2:index*2+2]}, 1000, 0, None, None)
        for index, (mid, lane) in enumerate(((1, 'wheelhouse'), (2, 'explore')))]
    publish_cards(store, home_cards.CardsResult(protos,
        [{'proto': proto, 'pool': None} for proto in protos], None, None, [], [], 0, None), 1000)
    with prepared_client(store, background_work=False) as client:
        r = client.get("/home/cards")
    assert r.status_code == 200
    recipes = [json.loads(m.replace("&#34;", '"').replace("&quot;", '"'))
               for m in re.findall(r"data-recipe='([^']*)'", r.text)]
    assert recipes, "no mode cards rendered"
    for rec in recipes:
        if rec["model"] == "mode":
            assert rec.get("mode_id") is not None
            assert rec.get("lane") in {"wheelhouse", "explore", "comfort", "fresh", "temporal"}
        else:
            assert rec["model"] == "fresh"  # a thin menu can add a prepared Fresh fallback
        assert rec.get("journey") in journeys.JOURNEYS


def test_fresh_mode_card_recipe_retains_lane(store):
    bundles = _seed_bundles(store)
    publish_bundles(store, {
        **{mid: {'fresh': bucket['fresh']} for mid, bucket in bundles.items()},
        '_meta': {'full_pools': True},
    }, 1)
    # Preserve real mode selection, recipe completion and HTTP serialization.
    # Preparing every Home menu is a separate worker obligation.
    cards = _publish_mode_menu(store, 1)
    assert cards and all(card['lane'] == 'fresh' for card in cards)
    with prepared_client(store, now=1, background_work=False) as client:
        response = client.get("/home/cards")
    assert response.status_code == 200
    recipe = json.loads(re.search(r"data-recipe='([^']*)'", response.text)[1])
    assert recipe["model"] == "mode" and recipe["lane"] == "fresh"


def test_cards_route_ok_when_empty(store):
    with _client(store) as client:
        r = client.get("/home/cards")
    assert r.status_code == 200      # falls back / empty, never 500


def test_explicit_tuning_with_object_tracks_is_cached_without_request_fallback(store, monkeypatch):
    # Explicit tuning can still produce ForYouItem objects. Ordinary reads must use the
    # prepared result instead of invoking the legacy ranker when mode bundles are missing.
    from html import unescape
    from yt_playlist.rec import choice_evidence, stored_home
    from yt_playlist.rec.surfaces import ForYouItem
    proto = {"lane": "wheelhouse", "label": "More in your wheelhouse", "name": "Mix", "note": "",
             "tracks": [ForYouItem(f"Song {i}", f"Artist {i}", "", f"v{i}", None, 0, "reason", key=f"k{i}")
                        for i in range(4)],
             "feedback_surface": None}
    seed_publication_subjects(store, [dict(key=f'k{i}', title=f'Song {i}', artist=f'Artist {i}',
                                          video_id=f'v{i}') for i in range(4)])
    calls = []

    def candidates(store, now, **kwargs):
        calls.append('wheelhouse')
        return [proto]

    monkeypatch.setattr(stored_home, "unmodeled_cards", candidates)
    context = choice_evidence.freeze_context(store, choice_evidence.model_context(store, 1000))
    empty = home_cards.CardsResult([], [], None, None, [], [], 0, None, choice_context=context)
    publish_cards(store, empty, 1000)
    with prepared_client(store, background_work=False) as client:
        assert client.get("/home/cards").status_code == 200
        assert calls == []
        r = client.post("/home/breadth", data={"breadth_bias": "0.5"})
        assert r.status_code == 200
        assert "Song" in r.text and "Mix" in r.text
        assert "wheelhouse" in calls
        tuned = store.rec_proposals.get_proposals('home_tuned')
        held = store.rec_proposals.get_proposals('home_choice')
        assert home_prepared._valid(tuned)
        cached = tuned['menus'][0]['protos'][0]
        assert cached['tracks'] == held['menu']['protos'][0]['tracks']
        assert [track['key'] for track in cached['tracks']] == [f'k{i}' for i in range(4)]
        assert all(track['reason'] == 'reason' for track in cached['tracks'])
        recipe = json.loads(unescape(re.search(r"data-recipe='([^']*)'", r.text)[1]))
        offered = choice_evidence.opportunity(store, recipe['opportunity_id'])
        assert recipe['option_id'] == recipe['experiment_id'] == cached['experiment_id']
        assert [option['id'] for option in offered['options']] == [cached['experiment_id']]
        assert offered['options'][0]['assignment'] is held['assignment'] is None
        assert tuned['choice_context'] == held['choice_context']
        frozen = choice_evidence.resolve_context(store, tuned['choice_context'])
        captured = choice_evidence.resolve_context(store, {'artifact': offered['context']})
        # Mix offers retain compact correction timing; the prepared cache separately
        # keeps the complete frozen model inputs. Both describe this same choice.
        assert captured['at'] == frozen['at'] == 1000
        assert 'comparison' not in captured
        tuning_calls = calls.copy()
        r = client.get("/home/cards")
        assert r.status_code == 200
        assert "Song" in r.text and "Mix" in r.text
        assert calls == tuning_calls


def test_prepared_mode_card_copies_keep_sql_and_files_private(
        store, _pristine_current_schema, mode_cards_inputs_template, mode_cards_home_template):
    from contextlib import closing
    from pathlib import Path
    from tests.store_fixture_support import initialize_consumer
    from yt_playlist.core.store import Store

    template = mode_cards_home_template
    bundles = mode_cards_inputs_template.result
    assert set(bundles) == {'1', '2'}
    assert all(set(bucket) == set(mode_surfaces.CARD_SURFACES) for bucket in bundles.values())
    assert all(len(items) == 20 for bucket in bundles.values() for items in bucket.values())
    template.restore(store)
    other = Store(':memory:')
    with closing(other.conn):
        initialize_consumer(other, _pristine_current_schema)
        template.restore(other)
        assert len({id(owner.conn) for owner in (store, other, template.source)}) == 3
        assert len({owner._evidence_key for owner in (store, other, template.source)}) == 3
        expected = template.source.rec_proposals.get_proposals(home_prepared.SURFACE)
        assert home_prepared._valid(expected)
        assert store.rec_proposals.get_proposals(home_prepared.SURFACE) == expected
        assert other.rec_proposals.get_proposals(home_prepared.SURFACE) == expected
        assert {mode['mode_id'] for mode in store.modes.list_modes()} == {1, 2}

        def bodies(owner):
            return {tuple(row[:3]): Path(row[3]) for row in owner.conn.execute(
                'SELECT publication_id,surface,chunk_key,filename FROM home_display_files')}

        original, first, second = (bodies(owner) for owner in (template.source, store, other))
        assert original and original.keys() == first.keys() == second.keys()
        before = {key: path.read_bytes() for key, path in original.items()}
        for key, path in original.items():
            assert first[key].read_bytes() == second[key].read_bytes() == before[key]
            assert not path.samefile(first[key]) and not path.samefile(second[key])
            assert not first[key].samefile(second[key])
        store.settings.set_setting('mode_cards_private', 'changed')
        store.rec_proposals.put_proposals(home_prepared.SURFACE, {**expected, 'built_at': 1001}, 1001)
        assert store.rec_proposals.get_proposals(home_prepared.SURFACE)['built_at'] == 1001
        for owner in (other, template.source):
            assert owner.settings.get_setting('mode_cards_private') is None
            assert owner.rec_proposals.get_proposals(home_prepared.SURFACE) == expected
        assert {key: path.read_bytes() for key, path in original.items()} == before
        assert {key: path.read_bytes() for key, path in second.items()} == before
