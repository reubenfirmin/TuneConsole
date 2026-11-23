import copy
from contextlib import ExitStack, closing
from dataclasses import replace
from types import SimpleNamespace

import pytest

from tests.conftest import seed_catalog_track
from tests.home_fixture_support import home_routes_only, without_metadata_providers
from tests.home_prepared_support import publication_inputs
from tests.test_home_other_genres import genre_bundles, offered
from tests.test_home_tuning import home_clients, prepare_tuning_home
from tests.musical_support import prepare_profiles, prepare_vibes, publish_intensities, publish_measured_vibes
from tests.prepared_fixture_support import prepared_template
from tests.recommendation_support import publish_bundles, seed_library_favorites, seed_publication_subjects
from tests.store_fixture_support import (bounded_consumer_schema, initialize_consumer,
                                         mock_snapshot_coordinator)
from yt_playlist.providers.base import MetadataObservation
from yt_playlist.rec import home_vibe, mix_tuning, mode_surfaces, musical_character, musical_profiles, rec_params


VIBE_HTTP_GENRES = (('jazz', 'Jazz'), ('techno', 'Techno'), ('classical', 'Classical'),
                   ('ambient', 'Ambient'), ('trance', 'Trance'), ('indie-rock', 'Indie Rock'),
                   ('house', 'House'))


def select_vibe(client, value):
    response = client.post('/home/vibe', data={'vibe': value})
    assert response.status_code == 200
    assert 'X-Home-Vibe-Request' not in response.headers
    return response


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


def sound_inputs(*, by_mode=False):
    """Detached lower inputs for real selection, without unrelated producers."""
    from yt_playlist.rec.vibe_recording_rules import version

    genres = ('Jazz', 'Techno', 'Trance', 'Indie Rock', 'Ambient', 'Classical')
    stamp = version()
    profiles, pools = {}, {}
    for lane in mode_surfaces.CARD_SURFACES:
        items = []
        for mid, genre in enumerate(genres):
            for index in range(20):
                energy = mid / 5 if by_mode else .1 if index >= 12 else .9
                if energy not in profiles:
                    profile = musical_profiles.resolve([])
                    activation = {'score': energy, 'support': 1., 'source': 'audio_measurement'}
                    profile['vibes'] = {'inputs_version': stamp,
                        'fits': {'relax': {**activation, 'score': 1 - energy}, 'energize': activation},
                        'intensity_fits': {'energize': activation}}
                    profiles[energy] = musical_character.compact(profile)
                key = f'{lane}-{mid}-{index}'
                items.append({'key': key, 'video_id': key, 'title': key, 'artist': f'Artist {mid}-{index}',
                              'genre': genre, 'musical': profiles[energy]})
        pools[lane] = items
    return pools


def select_sounds(pools, preset, monkeypatch):
    from yt_playlist.rec import vibe_pools

    # SQL membership/feedback and native score/profile reads have separate
    # owners in test_vibe_serving_batch. All selection/order/caps stay real.
    owned = {item['key']: item for lane, items in pools.items() if lane != 'fresh' for item in items}
    discovered = {item['key']: item for item in pools['fresh']}
    axes = {item['genre']: (item['genre'].lower(), None) for items in pools.values() for item in items}
    monkeypatch.setattr(vibe_pools, 'eligibility_inputs', lambda *_args, **_kwargs:
                        (set(), owned, discovered, {}, {}, 3600, set(), axes))
    reader = SimpleNamespace(settings=SimpleNamespace(get_setting=lambda _key: None),
                             memoized_read=lambda _name, prepare: prepare(),
                             musical_profiles=SimpleNamespace(by_keys=lambda _keys: {}),
                             rec_candidates=SimpleNamespace(comfort_facts=lambda keys: [
                                 {'key': key, 'liked': True, 'plays': 0, 'last_played': None} for key in keys]))
    bundles = {'_meta': {'comfort_pool': 100},
               '_vibes': {'version': home_vibe.VERSION, 'pools': {preset: pools}}}
    return vibe_pools.assemble(reader, 1000, preset, bundles=bundles)


def test_relax_selects_measured_tail_before_limiting_each_card(monkeypatch):
    pools = sound_inputs()
    before = copy.deepcopy(pools)
    cards = select_sounds(pools, 'relax', monkeypatch)
    assert {card['lane'] for card in cards} == set(mode_surfaces.CARD_SURFACES)
    for card in cards:
        assert len(card['tracks']) == 12
        assert all(musical_character.intensity(track) < .5 for track in card['tracks'])
        assert all(int(track['key'].rsplit('-', 1)[1]) >= 12 for track in card['tracks'])
    assert pools == before


def vibe_http_calculation(completed, selection):
    from yt_playlist.rec import home_cards
    def cards(calculation, request):
        vibe = home_vibe.current(calculation.store, request.now)
        result = completed(calculation, replace(request,
            epoch=home_cards._epoch(calculation.store, 'cards') if request.epoch is None else request.epoch,
            mode_order=selection.order if vibe else None))
        result.vibe = vibe
        for proto in result.protos:
            proto['recipe'] = {'lane': proto['lane'], 'vibe': vibe, 'vibe_policy': home_vibe.VERSION,
                               'musical_version': musical_character.VERSION}
            proto['experiment_id'] = home_cards.experiment_id(proto)
        return result

    return cards


@pytest.fixture(scope='module')
def vibe_http_schema(tmp_path_factory):
    # HTTP consumers exercise real catalog/SQL/files with their six input
    # genres plus the ordinary menu's House; full vocabulary is owned by admission.
    with pytest.MonkeyPatch.context() as patch, bounded_consumer_schema(VIBE_HTTP_GENRES) as schema:
        patch.setenv('YT_PLAYLIST_HOME', str(tmp_path_factory.mktemp('vibe-http-home')))
        yield schema


def test_vibe_http_schema_keeps_every_offered_family_and_snapshot_guards(vibe_http_schema):
    assert {row[0] for row in vibe_http_schema.execute('SELECT name FROM genre_family')} == {
        'Jazz', 'Techno', 'Classical', 'Ambient', 'Trance', 'Indie Rock', 'House'}
    assert vibe_http_schema.execute('PRAGMA query_only').fetchone()[0] == 1
    assert vibe_http_schema._revision_script


@pytest.fixture
def vibe_http_inputs(journey_store, monkeypatch, home_routes_only, without_metadata_providers):
    from yt_playlist.rec import home_cache, home_cards, home_prepared, measurement

    # One lifecycle consumes this private input once. Seed it directly instead
    # of building, inventorying and restoring a second prepared Store first.
    store = journey_store
    publication_inputs(store, monkeypatch, stable_options=True)
    store.rec_model.set_weight('genre:jazz', 1.3, now=1000)
    completed = home_cards.HomeCards.cards
    selection = SimpleNamespace(order=(4, 5, 0, 1, 2, 3))
    cards = vibe_http_calculation(completed, selection)
    monkeypatch.setattr(home_cards.HomeCards, 'cards', cards)
    monkeypatch.setattr(home_cards.HomeCards, 'audit', lambda *_args, **_kwargs: None)
    with store.read_view() as reader:
        baseline = cards(home_cards.HomeCards(reader, home_prepared._NoJobs(), None),
                         home_cards.CardsRequest(1000, record=False))
    home_prepared.remember(store, None, baseline, 1000)
    payload = home_cache.read(store, 'home_tuned')
    assert home_prepared._valid(payload)
    home_cache.put(store, home_prepared.SURFACE, payload, 1000)
    home_cache.put(store, 'home_tuned', {}, 1000)
    home_cache.put(store, 'home_choice', {}, 1000)
    # A genuine typed head detects unintended candidate publication. Its policy
    # is a completed lower input; SQL and published files remain real.
    with monkeypatch.context() as patch:
        patch.setattr(measurement, 'serving_policy', lambda _store, _now, ranker='lane':
                      ('controlled', {'schema': 4, 'algorithm': 'vibe-http-fixture', 'ranker': ranker}))
        publish_bundles(store, {'all': {'wheelhouse': baseline.protos[0]['tracks']}}, 1000)
    return SimpleNamespace(store=store, payload=payload, cards=cards, selection=selection)


@pytest.fixture
def vibe_clients(vibe_http_inputs, vibe_template_bytecode):
    from fastapi.testclient import TestClient
    from tests.conftest import FakeClient
    from yt_playlist.web.app import create_app

    store = vibe_http_inputs.store
    with ExitStack() as clients:
        def open_client(clock):
            identity = store.accounts.upsert_account('main', None, True)
            app = create_app(store, lambda: {identity: FakeClient()},
                             now_fn=lambda: clock[0], background_work=False)
            # Reuse compiled source only. Every app retains its own environment,
            # store-bound globals, requests and rendered response data.
            app.state.ctx.templates.env.bytecode_cache = vibe_template_bytecode
            client = TestClient(app, base_url='http://127.0.0.1', backend_options={'use_uvloop': True})
            clients.callback(client.close)
            return clients.enter_context(client)

        yield open_client


@pytest.fixture(scope='module')
def vibe_template_bytecode(tmp_path_factory):
    from jinja2 import FileSystemBytecodeCache

    return FileSystemBytecodeCache(str(tmp_path_factory.mktemp('vibe-template-bytecode')))


def test_saved_vibe_requires_a_configured_access_policy(store, monkeypatch):
    store.settings.set_setting(home_vibe.SETTING, 'relax')
    store.settings.set_setting(mix_tuning.UPDATED_AT_SETTING, '1000')
    monkeypatch.setattr(home_vibe, '_access', None)
    with pytest.raises(RuntimeError, match='access policy'):
        home_vibe.current(store, 1000)
    with home_vibe.access_policy(lambda _store, _now: True):
        assert home_vibe.current(store, 1000) == 'relax'
        with home_vibe.access_policy(lambda _store, _now: False):
            assert home_vibe.current(store, 1000) == ''
        assert home_vibe.current(store, 1000) == 'relax'
    with pytest.raises(RuntimeError, match='access policy'):
        home_vibe.current(store, 1000)
    # Natural readiness never applies the remembered control or invokes a gate.
    with home_vibe.natural():
        assert home_vibe.current(store, 1000) == ''


@pytest.mark.parametrize('preset', ('relax', 'concentrate', 'cruise', 'energize', 'release', 'uplift'))
def test_vibes_favor_their_measured_sound_before_selection(file_backed_store, preset):
    store = file_backed_store
    for key in ('preferred', 'other'):
        store.discovery.upsert_discovered_track(key, key, key, key, '', None, None, None, None, 1)
    prepare_profiles(store)
    publish_measured_vibes(store, preset, {'preferred': .9, 'other': .1}, now=1)
    items = [{'key': 'other', 'score': .9}, {'key': 'preferred', 'score': .1}]
    before = copy.deepcopy(items)
    with home_vibe.authorized(store, preset):
        rank = home_vibe.ranker(store, 1000)
        assert [rank.affinity(item) for item in items] == [.1, .9]
        assert [item['key'] for item in rank(items)] == ['preferred', 'other']
    assert items == before


def test_missing_audio_preserves_taste_and_does_not_invent_mood():
    items = [{"key": "a", "score": .1}, {"key": "b", "score": .9}]
    assert home_vibe.affinity(items[0], "relax", {}) == .5
    assert home_vibe.affinity({"key": "a", "genre": "Ambient"}, "relax", {}) == .5
    ambient = {"key": "a", "musical": musical_character.compact(musical_profiles.resolve([], genre='Ambient'))}
    assert home_vibe.affinity(ambient, 'relax') == .5
    assert home_vibe.affinity(ambient, 'release') == .5
    assert home_vibe.affinity(items[0], "relax", {"a": {"energy": float("nan")}}) == .5
    assert home_vibe.affinity(items[0], "cruise", {}) == .5
    assert home_vibe.Ranker('relax')(items) == items[::-1]


def test_linked_native_evidence_takes_precedence_over_catalog_scalars(journey_store):
    from yt_playlist.util.matching import identity_key

    store = journey_store
    key = identity_key("Song", "Artist")
    store.discovery.upsert_discovered_track(key, "v", "Song", "Artist", "", None, None, None, None, 1,
                                 audio={"mood_relaxed": .2})
    tid = store.tracks.upsert_track("v", "Song", "Artist", None, None)
    store.enrichment_state.record_metadata_observations(tid, 'acousticbrainz', [
        MetadataObservation('mood_relaxed', .8, 'recording', 'mbid', 'prediction',
                            match_evidence={'method': 'supplied_recording_mbid'})], run_id='native', now=1)
    prepare_profiles(store)
    profile = musical_character.load(store, [key])[key]
    assert profile['dimensions']['intensity']['value'] < .5
    assert profile['dimensions']['relaxed']['value'] == .8


@pytest.fixture
def journey_store(tmp_path, journey_schema, monkeypatch, native_sql_calls):
    from yt_playlist.core.store import Store

    store = Store(tmp_path / 'journey.db')
    with closing(store.conn):
        store.conn.execute('PRAGMA synchronous=OFF')
        initialize_consumer(store, journey_schema, snapshot_guards=False)
        store.conn.execute('PRAGMA wal_autocheckpoint=0')
        # Keep real SQL and files beneath successful snapshot coordination.
        with mock_snapshot_coordinator(store, monkeypatch):
            yield store


@pytest.fixture(scope='module')
def journey_schema():
    with bounded_consumer_schema(VIBE_HTTP_GENRES, snapshot_guards=False) as schema:
        yield schema


def test_mix_journey_uses_profiles_and_keeps_visible_choice_after_preparation_changes(journey_store, monkeypatch):
    from yt_playlist.rec import choice_evidence, home_cards, home_prepared

    store = journey_store
    key = 'measured|artist'
    older_key = 'older|artist'
    seed_publication_subjects(store, [{'key': key, 'video_id': 'measured', 'title': 'Measured', 'artist': 'Artist'},
                                      {'key': older_key, 'video_id': 'older', 'title': 'Older', 'artist': 'Artist'}])
    publish_intensities(store, {key: .9, older_key: .1}, now=1000)
    profile = musical_character.load(store, [key])[key]
    older = musical_character.load(store, [older_key])[older_key]
    publish_intensities(store, {older_key: .8}, now=1001)
    assert musical_character.intensity({'musical': musical_character.load(store, [older_key])[older_key]}) == .8
    items = [{'key': key, 'video_id': 'measured'},
             {'key': older_key, 'video_id': 'older', 'musical': older},
             {'key': 'absent', 'video_id': 'absent'}]
    ordered = home_cards._order_by_journey(store, items, 'energy_arc', 1)
    served = {item['key']: item for item in ordered}
    assert served[key]['musical'] == profile
    assert served[key]['serving_context']['musical_digest'] == profile['digest']
    assert served[key]['serving_context']['intensity'] == .9
    assert served[older_key]['musical'] == older
    assert served[older_key]['serving_context']['intensity'] == .1
    assert served['absent']['serving_context']['intensity'] is None
    assert served['absent']['serving_context']['musical_digest'] is None
    oid = choice_evidence.issue(store, 'mixes', [{'id': 'chosen', 'tracks': ordered,
        'label': 'Energy arc', 'intent': 'energy_arc', 'genres': ['House'],
        'recipe': {'vibe': None, 'vibe_policy': home_vibe.VERSION,
                   'musical_version': musical_character.VERSION}}], 1000, context={})
    captured = store.rec_ledger.evidence().get('opportunity', oid)
    publish_intensities(store, {key: .2}, now=1002)
    assert musical_character.load(store, [key])[key] != profile
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


def test_vibe_changes_offered_styles_as_well_as_track_order(monkeypatch):
    pools = sound_inputs(by_mode=True)
    gentle = select_sounds(pools, 'relax', monkeypatch)
    boost = select_sounds(pools, 'energize', monkeypatch)
    for menu in (gentle, boost):
        assert {card['lane'] for card in menu} == set(mode_surfaces.CARD_SURFACES)
        assert len(menu) == 4
        assert all(len(card['tracks']) == 12 for card in menu)
    assert {t['genre'] for c in gentle for t in c['tracks']} != {t['genre'] for c in boost for t in c['tracks']}
    assert all(c['mode_id'] is None for c in [*gentle, *boost])


def seed_sounds(store, *, by_mode=False):
    # These prepared consumers use completed catalog/audio inputs. Provider
    # ingestion and measured-source attribution have independent owners in
    # test_musical_preparation and test_vibe_prediction. Keep all six genres,
    # four lanes and twenty tracks, plus actual profile/file publication.
    bundles = genre_bundles(store)
    intensities = {}
    for mid in range(6):
        for lane, items in bundles[str(mid)].items():
            for i, item in enumerate(items):
                # Put the soft tracks beyond a full card, so a post-selection reordering fails.
                energy = mid / 5 if by_mode else .1 if i >= 12 else .9
                item['video_id'] = item['key']
                intensities[item['key']] = energy
    bundles['all'] = {lane: [item for mid in range(6) for item in bundles[str(mid)][lane]]
                      for lane in mode_surfaces.CARD_SURFACES}
    seed_publication_subjects(store, [item for items in bundles['all'].values() for item in items],
                              discoveries=[item['key'] for item in bundles['all']['fresh']])
    seed_library_favorites(store, bundles['all']['comfort'])
    prepare_profiles(store)
    publish_intensities(store, intensities, now=1)
    # Ordinary Fresh reads have a separate owner from Vibes' candidate pools.
    publish_bundles(store, bundles, 1, fresh=bundles['all']['fresh'], vibes=True)


@pytest.fixture
def ordinary_menu_client(vibe_http_schema, vibe_template_bytecode,
                         home_routes_only, without_metadata_providers, monkeypatch):
    from fastapi.testclient import TestClient
    from tests.home_fixture_support import seed_menu_cards
    from yt_playlist.core.store import Store
    from yt_playlist.rec import home_cards
    from yt_playlist.web.app import create_app

    # Exercise capture and HTTP rendering with a controlled assembler result.
    store = Store(':memory:')
    try:
        initialize_consumer(store, vibe_http_schema)
        protos = seed_menu_cards(store, tracks_per_card=1)
        for proto in protos:
            proto['experiment_id'] = home_cards.experiment_id(proto)
        ready = home_cards.CardsResult(protos, [], None, None, [], [], 0, '')
        ready.slots = [{'proto': proto, 'pool': None} for proto in protos]
        monkeypatch.setattr(home_cards.HomeCards, 'cards', lambda *_: copy.deepcopy(ready))
        app = create_app(store, lambda: {}, now_fn=lambda: 1000, background_work=False)
        app.state.ctx.templates.env.bytecode_cache = vibe_template_bytecode
        with TestClient(app, base_url='http://127.0.0.1',
                        backend_options={'use_uvloop': True}) as client:
            yield store, client
    finally:
        store.conn.close()


def test_ordinary_menu_capture_has_no_vibe_purpose(ordinary_menu_client, caplog):
    import html
    import json
    import re
    store, client = ordinary_menu_client
    ordinary = client.get('/home/cards')
    assert ordinary.status_code == 200
    ordinary_recipes = [json.loads(html.unescape(value)) for value in
                        re.findall(r"data-recipe='([^']*)'", ordinary.text)]
    assert len(ordinary_recipes) == 4
    from yt_playlist.rec import choice_evidence
    for displayed in ordinary_recipes:
        captured = choice_evidence.opportunity(store, displayed['opportunity_id'])
        assert captured and 'vibe' not in captured
        assert any(str(row['id']) == displayed['option_id'] for row in captured['options'])
    assert not any('Mix choice capture failed' in record.getMessage() for record in caplog.records)


def test_vibe_renders_changed_selection_with_visible_choice_evidence(vibe_http_inputs, vibe_clients, monkeypatch):
    import html
    import json
    import re
    from yt_playlist.rec import choice_evidence, home_prepared

    store = vibe_http_inputs.store
    clock = [1000.0]
    client = vibe_clients(clock)
    before = store.recommendations.head('home')
    learned = store.rec_model.get_weights()
    with monkeypatch.context() as patch:
        def old_board(*args, **kwargs):
            pytest.fail('Returning the just-built Vibe cards must not reopen the Home board')
        patch.setattr(home_prepared, 'load', old_board)
        response = select_vibe(client, 'relax')
    assert response.status_code == 200
    assert 'data-vibe="relax"' in response.text
    assert '"vibe": "relax"' in response.text
    # Mix outcomes retain the visible theme/raw genres after a later temporary
    # vibe change; #688 deliberately retires hidden recipe/profile precision.
    recipes = [json.loads(html.unescape(value)) for value in re.findall(r"data-recipe='([^']*)'", response.text)]
    assert len(recipes) == 4 and all(recipe['vibe'] == 'relax' for recipe in recipes)
    offered = choice_evidence.opportunity(store, recipes[0]['opportunity_id'])
    assert offered and offered['format'] == 'mixes' and offered['vibe'] == 'relax'
    rendered_genres = [json.loads(html.unescape(value)) for value in
                       re.findall(r"data-genres='([^']*)'", response.text)]
    from yt_playlist.core import mix_choices
    options = [mix_choices.expand(store.conn, option) for option in offered['options']]
    assert all(recipe['opportunity_id'] == offered['id'] for recipe in recipes)
    assert all('descriptor_id' in row and not {'recipe', 'tracks', 'keys'} & row.keys() for row in offered['options'])
    assert [option['genres'] for option in options] == [sorted(set(g)) for g in rendered_genres]
    assert all(option['label'] and option['intent'] and 'recipe' not in option
               and 'tracks' not in option for option in options)
    baseline_keys = {track['key'] for proto in vibe_http_inputs.payload['menus'][0]['protos']
                     for track in proto['tracks']}
    rendered_keys = set(re.findall(r'data-key="([^"]*)"', response.text))
    assert rendered_keys and rendered_keys != baseline_keys
    assert store.rec_model.get_weights() == learned and store.rec_model.get_leans() == {}
    assert store.recommendations.head('home') == before

    # Continue the same temporary selection through refresh, policy replacement
    # and expiry. Each stage preserves the evidence already shown to the user.
    assert 'data-vibe="relax"' in client.post("/home/refresh-cards").text
    assert store.rec_model.get_weights() == learned
    assert store.recommendations.head('home') == before
    assert store.rec_ledger.evidence().get('opportunity', offered['id']) == offered
    monkeypatch.setattr(home_vibe, 'VERSION', home_vibe.VERSION + '/changed-policy')
    # Supply the changed policy's new visible menu below the request controller.
    # A private version stamp alone cannot create different visible evidence.
    vibe_http_inputs.selection.order = (5, 4, 1, 0, 2, 3)
    response = select_vibe(client, 'relax')
    match = re.search(r"hx-vals='([^']*opportunity_id[^']*)'", response.text)
    assert match
    revised = choice_evidence.opportunity(store, json.loads(html.unescape(match[1]))['opportunity_id'])
    assert revised['id'] != offered['id']
    assert revised['options'] != offered['options']
    assert all('recipe' not in option and 'tracks' not in option for option in revised['options'])
    assert store.rec_ledger.evidence().get('opportunity', offered['id']) == offered
    clock[0] += mix_tuning.TTL_S
    response = client.get("/home/fingerprint")
    assert 'data-vibe="relax"' not in response.text
    assert store.settings.get_setting(home_vibe.SETTING) is None
    assert store.rec_model.get_weights() == learned and store.rec_model.get_leans() == {}
    assert store.recommendations.head('home') == before
    assert store.rec_ledger.evidence().get('opportunity', offered['id']) == offered
    assert store.rec_ledger.evidence().get('opportunity', revised['id']) == revised


@pytest.fixture(scope='module')
def genre_vibe_candidates(vibe_http_schema):
    from tests.test_home_other_genres import install_genre_http_inputs

    def seed(source):
        bundles = genre_bundles(source, genres=['Jazz', 'Techno', 'Trance', 'Ambient'], tracks_per_lane=8)
        # Only Jazz needs distinct full Vibe reserves. The three other themes
        # need four ordinary choices each, and share those catalog subjects.
        for mid in ('1', '2', '3'):
            shared = bundles[mid]['wheelhouse'][:4]
            for lane in mode_surfaces.CARD_SURFACES:
                bundles[mid][lane] = [{**track, 'lane': lane} for track in shared]
        for lane in ('comfort', 'fresh'):
            bundles['0'][lane] = bundles['0'][lane][:4]
        bundles['all'] = {lane: [track for mid in ('0', '1', '2', '3')
                                for track in bundles[mid][lane]]
                          for lane in mode_surfaces.CARD_SURFACES}
        tracks = {track['key']: track for lane in bundles['all'].values() for track in lane}
        for track in tracks.values():
            track['video_id'] = track['key']
        fresh = bundles['0']['fresh']
        seed_publication_subjects(source, tracks.values(), discoveries=[track['key'] for track in fresh])
        seed_library_favorites(source, bundles['all']['comfort'])
        profile = musical_character.compact(musical_profiles.resolve([], overrides={
            'intensity': .7, 'mood_relaxed': .3, 'mood_happy': .7}))
        profiles = {key: profile for key in tracks}
        # Completed musical candidates are below the genre request controller.
        # Its real serving selector still applies current SQL eligibility,
        # family filtering, per-card capacities and persisted focus. Numeric
        # Vibe preparation has independent model and pool owners.
        pools = {lane: [{**track, 'musical': profile} for track in items]
                 for lane, items in bundles['all'].items()}
        library = {track['key']: track for lane in ('wheelhouse', 'explore') for track in pools[lane]}
        for lane in ('wheelhouse', 'explore'):
            pools[lane] = list(library.values())
        bundles['_vibes'] = {
            'version': home_vibe.VERSION, 'profile_version': musical_character.VERSION,
            'pools': {preset: copy.deepcopy(pools) for preset in ('release', 'cruise')},
            'genres': {}}
        with pytest.MonkeyPatch.context() as patch:
            patch.setattr(musical_character, 'load', lambda _store, keys:
                          {key: profiles[key] for key in keys if key in profiles})
            install_genre_http_inputs(patch)
            publish_bundles(source, bundles, 1, fresh=fresh)
        return profiles

    with prepared_template(vibe_http_schema, seed) as template:
        yield template


def test_genre_vibe_candidates_keep_real_catalog_and_four_themes(genre_vibe_candidates):
    source = genre_vibe_candidates.source
    assert source.conn.execute('SELECT count(*) FROM recordings').fetchone()[0] == 36
    assert len(source.modes.list_modes(active_only=True)) == 4
    assert source.recommendations.head('vibe:release')
    assert source.conn.execute('PRAGMA query_only').fetchone()[0] == 1


@pytest.fixture(scope='module')
def genre_vibe_template(genre_vibe_candidates):
    from tests.prepared_fixture_support import capture_template
    from tests.test_home_other_genres import _publish_menu, install_genre_http_inputs

    with genre_vibe_candidates.open(':memory:') as source, pytest.MonkeyPatch.context() as patch:
        install_genre_http_inputs(patch)
        _publish_menu(source, 1000)
        yield capture_template(source, genre_vibe_candidates.result)


def test_genre_vibe_input_keeps_real_prepared_home(genre_vibe_template):
    from yt_playlist.rec import home_cache, home_prepared

    assert home_prepared._valid(home_cache.read(genre_vibe_template.source, home_prepared.SURFACE))
    assert genre_vibe_template.source.recommendations.head('home')


@pytest.fixture
def genre_vibe_client(genre_vibe_session, genre_vibe_template, vibe_template_bytecode, monkeypatch,
                      home_routes_only, without_metadata_providers, request):
    from fastapi.testclient import TestClient
    from tests.conftest import FakeClient
    from tests.test_home_other_genres import install_genre_http_inputs
    from yt_playlist.web.app import create_app

    install_genre_http_inputs(monkeypatch)
    profiles = genre_vibe_template.result
    # Musical calculation is below genre controls. Real family matching,
    # candidate selection, persisted focus and all HTTP rendering remain real.
    monkeypatch.setattr(musical_character, 'load', lambda _store, keys:
                        {key: profiles[key] for key in keys if key in profiles})
    from yt_playlist.rec import home_cards
    monkeypatch.setattr(home_cards.HomeCards, 'audit', lambda *_args, **_kwargs: None)
    source = genre_vibe_session
    account = source.accounts.upsert_account('main', None, True)
    app = create_app(source, lambda: {account: FakeClient()}, now_fn=lambda: 1000,
                     background_work=False)
    app.state.ctx.templates.env.bytecode_cache = vibe_template_bytecode
    with TestClient(app, base_url='http://127.0.0.1', backend_options={'use_uvloop': True}) as client:
        steps = [test_explicit_genre_focus_keeps_its_learned_lean,
                 test_steering_another_genre_cannot_expand_explicit_focus,
                 test_vibe_selection_preserves_explicit_genre_request_and_lean,
                 test_vibe_rotation_preserves_explicit_genre_restriction,
                 test_genre_requests_restrict_vibes_and_survive_clear,
                 test_reset_clears_vibe_after_preserving_explicit_genre_focus]
        target = next(index for index, owner in enumerate(steps) if owner.__name__ == request.node.originalname)
        while source._genre_completed < target:
            steps[source._genre_completed]((source, client))
            source._genre_completed += 1
        yield source, client
        source._genre_completed = target + 1


@pytest.fixture(scope='module')
def genre_vibe_session(genre_vibe_template, tmp_path_factory):
    path = tmp_path_factory.mktemp('genre-vibe-session') / 'source.db'
    with genre_vibe_template.open(path) as source:
        source.conn.execute('PRAGMA synchronous=NORMAL')
        source._genre_completed = 0
        yield source


def test_explicit_genre_focus_keeps_its_learned_lean(genre_vibe_client):
    store, client = genre_vibe_client
    response = client.post("/home/mix-genres", data={"genres": "jazz"})
    assert offered(response) == {'jazz'}
    response = client.post("/home/steer", data={"axis": "genre:jazz", "weight": "1.5"})
    assert offered(response) == {'jazz'}
    assert store.rec_model.get_lean('genre:jazz') == 1.5


def test_steering_another_genre_cannot_expand_explicit_focus(genre_vibe_client):
    store, client = genre_vibe_client
    response = client.post("/home/steer", data={"axis": "genre:techno", "weight": "2"})
    assert offered(response) == {"jazz"}  # A dial cannot expand the explicit genre focus.
    assert store.rec_model.get_lean("genre:techno") == 2


def test_vibe_selection_preserves_explicit_genre_request_and_lean(genre_vibe_client):
    store, client = genre_vibe_client
    response = select_vibe(client, 'release')
    assert 'data-requested-genres=\'["jazz"]\'' in response.text
    assert store.rec_model.get_lean("genre:jazz") == 1.5
    assert offered(response) == {'jazz'}
    assert store.rec_proposals.get_proposals('home_genre_request')['include_genres'] == ['jazz']


def test_vibe_rotation_preserves_explicit_genre_restriction(genre_vibe_client):
    store, client = genre_vibe_client
    # Vibe rotations keep the explicit genre restriction until clear/expiry.
    store.rec_ledger.refresh_card('cards', max(1, rec_params.get_param(store, 'erosion_view_cap')), 1000)
    assert store.rec_proposals.get_proposals('home_genre_request')['epoch'] != mix_tuning.card_epoch(store)
    assert offered(client.get('/home/cards')) == {'jazz'}


def test_genre_requests_restrict_vibes_and_survive_clear(genre_vibe_client):
    store, client = genre_vibe_client
    response = select_vibe(client, '')
    assert 'data-vibe=""' in response.text
    assert store.settings.get_setting(home_vibe.SETTING) is None
    assert store.rec_model.get_lean("genre:jazz") == 1.5
    assert response.headers['X-Mix-Tuning-Active'] == 'true'
    assert offered(response) == {'jazz'}


def test_reset_clears_vibe_after_preserving_explicit_genre_focus(genre_vibe_client):
    store, client = genre_vibe_client
    client.post("/home/vibe", data={"vibe": "cruise"})
    client.post("/home/fingerprint/reset")
    assert store.settings.get_setting(home_vibe.SETTING) is None


def test_clearing_the_only_vibe_clears_tuning_indicator_and_deadline(
        vibe_http_schema, home_routes_only, without_metadata_providers, monkeypatch):
    from fastapi import Response
    from fastapi.testclient import TestClient
    from tests.conftest import FakeClient
    from yt_playlist.core.store import Store
    from yt_playlist.web.app import create_app
    from yt_playlist.web import app as webapp
    from yt_playlist.web.routes import app_update, home_tuning
    from yt_playlist.web.routes.home_views import HomeViews

    # Clearing persisted controls does not require a fitted catalog or menu.
    # Keep the actual request controller, SQL settings and rendered fingerprint.
    # Card composition has separate HTTP owners.
    monkeypatch.setattr(HomeViews, 'cards_safe', lambda self, request, now, *, remaining=None, **kwargs:
                        self.tuning_response(Response(), now, remaining=remaining))
    monkeypatch.setattr(webapp, 'build_all', lambda ctx: [
        app_update.build(ctx), home_tuning.build(ctx, HomeViews(ctx))])
    store = Store(':memory:')
    try:
        initialize_consumer(store, vibe_http_schema)
        identity = store.accounts.upsert_account('main', None, True)
        app = create_app(store, lambda: {identity: FakeClient()}, now_fn=lambda: 1000.,
                         background_work=False)
        with TestClient(app, base_url='http://127.0.0.1',
                        backend_options={'use_uvloop': True}) as client:
            selected = client.post('/home/vibe', data={'vibe': 'relax'})
            assert selected.status_code == 200
            assert selected.headers['X-Mix-Tuning-Active'] == 'true'
            assert float(selected.headers['X-Mix-Tuning-Remaining']) > 0
            response = client.post('/home/vibe', data={'vibe': ''})
            assert response.status_code == 200
            assert response.headers['X-Mix-Tuning-Active'] == 'false'
            assert float(response.headers['X-Mix-Tuning-Remaining']) == 0
            assert 'data-tuning-active="false"' in client.get('/home/fingerprint').text
    finally:
        store.conn.close()


def test_invalid_vibe_does_not_change_controls_or_deadline(store, prepared_client):
    client = prepared_client([1000.0])
    assert client.post("/home/vibe", data={"vibe": "unknown"}).status_code == 400
    assert store.settings.get_setting(home_vibe.SETTING) is None
    assert mix_tuning.remaining(store, 1000) == 0


def test_vibe_selection_survives_render_failure(store, monkeypatch, prepared_client):
    from yt_playlist.web.routes.home_views import HomeViews

    client = prepared_client([1000.0])

    def fail(*args, **kwargs):
        raise RuntimeError('controlled rendering failure')

    monkeypatch.setattr(HomeViews, 'card_context', fail)
    response = client.post('/home/vibe', data={'vibe': 'relax'})
    assert response.status_code == 503
    assert not response.content
    assert client.get('/home/vibe/status').status_code == 404
    assert client.get('/home/cards').status_code == 503
    # Read recovery can observe the accepted action without replaying its POST.
    assert store.settings.get_setting(home_vibe.SETTING) == 'relax'


@pytest.mark.parametrize('removed', ['focus', 'bright', 'unwind', 'gentle', 'boost', 'intense', 'move'])
def test_removed_presets_are_not_reinterpreted(store, removed, prepared_client):
    client = prepared_client([1000.0])
    assert client.post('/home/vibe', data={'vibe': removed}).status_code == 400
    store.settings.set_setting(home_vibe.SETTING, removed)
    assert home_vibe.current(store, 1000) == ''
