import json
import logging
from copy import deepcopy
from types import SimpleNamespace

from fastapi.testclient import TestClient

from tests.conftest import FakeClient, seed_catalog_track
from tests.group_support import publish, recordings
from yt_playlist.rec import (choice_evidence, group_home, group_serving, group_viz,
                             home_cards, home_prepared, taste_viz)
from yt_playlist.web.app import create_app


def library(store):
    tracks = recordings([('House', 32, .1), ('Jazz', 32, .9)])
    pool = []
    for i, track in enumerate(tracks):
        title, artist, album = f'Song {i}', f'Artist {i // 2}', f'Album {i // 2}'
        seed_catalog_track(store, track.key, title, artist, video_id=f'v{i}', genre=track.genre, album=album)
        pool.append({'key': track.key, 'title': title, 'artist': artist, 'album': album,
                     'video_id': f'v{i}', 'thumbnail': '', 'genre': track.genre, 'year': 2000})
    head = publish(store, tracks)
    store.rec_proposals.put_proposals('mode_bundles', {'all': {'wheelhouse': pool, 'explore': pool,
        'comfort': pool, 'fresh': []}, '_meta': {'full_pools': True, 'comfort_pool': 64}}, 1050)
    assert group_serving.prepare(store, 1100)
    return tracks, head


def service(store):
    return home_cards.HomeCards(store, home_prepared._NoJobs(), logging.getLogger(__name__))


def test_default_home_offers_use_exact_graph_definitions_without_legacy_ids(store):
    _tracks, head = library(store)
    result = service(store).cards(home_cards.CardsRequest(1200, record=False))
    assert len(result.protos) >= 2
    by_version = {g['version']: g for g in head['groups']}
    for proto in result.protos:
        assert proto['mode_id'] is None
        group = by_version[proto['group_version']]
        assert proto['note'] != group['name']
        assert proto['note'].startswith({
            'wheelhouse': 'Songs close to what you play most',
            'explore': 'Songs you own but hardly ever play',
            'comfort': "Favorites you haven't reached for lately",
        }[proto['lane']])
        assert proto['name'].startswith(proto['label'] + ' - ')
        assert len(proto['tracks']) == 12
        assert {t['key'] for t in proto['tracks']} <= group['members'].keys()
        assert proto['recipe']['group_generation'] == head['generation']
        assert proto['recipe']['model'] == 'musical-group'
    keys = [t['key'] for p in result.protos for t in p['tracks']]
    assert len(set(keys)) == len(keys)
    assert result.choice_context['group_generation'] == head['generation']
    assert not list(store.rec_ledger.evidence().records('opportunity'))


def test_cached_group_headings_are_corrected_without_rewriting_the_prepared_set(store):
    _tracks, head = library(store)
    payload = home_prepared.prepare(store, 1200)
    names = {group['version']: group['name'] for group in head['groups']}
    for menu in payload['menus']:
        for proto in menu['protos']:
            group_name = names[proto['group_version']]
            proto['note'] = group_name
            proto['name'] = f"{group_name} - {proto['name']}"
    original = deepcopy(payload)
    result = home_prepared.cards(store, payload, 1201, service(store), record=False)
    assert result.protos
    for proto in result.protos:
        assert proto['note'] != names[proto['group_version']]
        assert proto['name'].startswith(proto['label'] + ' - ')
        assert proto['recipe']['group_generation'] == head['generation']
    assert payload == original


def test_filters_do_not_turn_small_interests_into_unrelated_mixes(store):
    library(store)
    data = group_home.inputs(store, 1200)
    cards = group_home.assemble(store, 1200, 0, data, include={'jazz'})
    assert cards and all(p['group_name'] == 'Jazz' for p in cards)
    assert all(t['genre'] == 'Jazz' for p in cards for t in p['tracks'])
    assert not group_home.assemble(store, 1200, 0, data, include={'metal'})


def test_rollback_and_old_preview_schema_do_not_activate_graph_serving(store):
    library(store)
    assert group_serving.policy(store) == 'adaptive'
    store.settings.set_setting('rec_group_serving', 'legacy')
    assert group_serving.policy(store) == 'legacy'
    assert group_home.inputs(store, 1200) is None
    store.settings.set_setting('rec_group_serving', 'adaptive')
    preview = store.rec_proposals.get_proposals(group_serving.SURFACE)
    preview['schema'] = 1
    store.rec_proposals.put_proposals(group_serving.SURFACE, preview, 1300)
    assert group_serving.policy(store) == 'legacy'
    assert group_home.inputs(store, 1300) is None


def test_small_initial_groups_remain_visible_without_replacing_usable_startup_mixes(store):
    tracks = recordings([('House', 4, .1)])
    pool = []
    for i, track in enumerate(tracks):
        seed_catalog_track(store, track.key, f'Track {i}', f'Artist {i}', video_id=f'v{i}', genre='House')
        pool.append({'key': track.key, 'title': f'Track {i}', 'artist': f'Artist {i}',
                     'video_id': f'v{i}', 'genre': 'House', 'album': '', 'thumbnail': ''})
    publish(store, tracks)
    store.rec_proposals.put_proposals('mode_bundles', {
        'all': {'wheelhouse': pool, 'explore': pool, 'comfort': [], 'fresh': []},
        '_meta': {'full_pools': True, 'unmodeled': True}}, 1000)
    assert group_serving.prepare(store, 1100)
    assert group_serving.view(store)['counts']['known'] == 4
    assert group_serving.policy(store) == 'legacy'
    prepared = home_prepared.prepare(store, 1200)
    assert prepared and any(m['protos'] for m in prepared['menus'])


def test_prepared_home_and_real_menu_capture_preserve_group_context(store):
    _tracks, head = library(store)
    payload = home_prepared.prepare(store, 1200)
    assert payload and payload['group_policy'] == 'adaptive'
    assert home_prepared._valid(payload)
    assert payload['choice_context']['group_generation'] == head['generation']
    identity = store.identities.upsert_identity('main', 'cred', None, True)
    fake = FakeClient()
    clock = [1300]
    app = create_app(store, lambda: {identity: fake}, now_fn=lambda: clock[0])
    with TestClient(app, base_url='http://127.0.0.1') as client:
        response = client.get('/home/cards')
        assert response.status_code == 200
        assert 'musical-group' in response.text
        assert '<h2 class="gen-theme">Songs ' in response.text
        assert '<h2 class="gen-theme">House</h2>' not in response.text
        assert '<h2 class="gen-theme">Jazz</h2>' not in response.text
        offered = list(store.rec_ledger.evidence().records('opportunity'))
        menu = next(o for o in offered if o['format'] == 'mixes')
        options = menu['options']
        assert all(o['group_version'] for o in options)
        ids = [o['id'] for o in options]
        choice_evidence.respond(store, menu['id'], 'visible', 1301, visible=ids)
        clock[0] = 1302
        recipe = {**options[0]['recipe'], 'opportunity_id': menu['id'], 'option_id': ids[0]}
        generated = client.post('/home/generate', data={'name': 'Group soak mix',
            'tracks': json.dumps(options[0]['tracks']), 'recipe': json.dumps(recipe)})
        assert generated.status_code == 200 and 'Saved' in generated.text
        assert fake.created and fake.added
        choice_evidence.daily(store, 1303)
        state = store.rec_proposals.get_proposals('group_choice_bandit')
        chosen = next(r for r in state['rows'] if r['version'] == options[0]['group_version'])
        assert chosen['positive'] > 0 and chosen['picked'] == 1
        assert state['excluded']['unmapped_definition'] == 0
        assert taste_viz.mix_choices(store, 1303)['picked'] == 1
        choices = client.get('/taste/panels/generated')
        assert choices.status_code == 200
        assert 'Mix options confirmed visible in the browser' in choices.text
        assert 'gives a legacy score' not in choices.text
        assert 'Server-rendered Home offers' not in choices.text


def test_history_only_refresh_and_retirement_accumulate_all_adaptive_menus(store, monkeypatch):
    from yt_playlist.rec import candidate_refresh, rec_params
    library(store)
    choice_evidence.refresh_report(store, 1200)
    pools = ('mode_bundles', 'fresh_pool', group_serving.SURFACE)
    original_pools = {name: store.rec_proposals._proposal_json(name) for name in pools}
    preferences = candidate_refresh.preferences(store, 1200)
    clock = [100]
    original_cards = home_cards.HomeCards.cards

    def one_menu(self, request):
        result = original_cards(self, request)
        clock[0] += 6
        return result

    # Exercise partial, immediately usable batches with the real graph, report,
    # ledger and menu assembler. Each attempt has room for exactly one menu.
    monkeypatch.setattr(home_prepared.time, 'monotonic', lambda: clock[0])
    monkeypatch.setattr(home_prepared, 'PREPARE_BUDGET_S', 10)
    monkeypatch.setattr(home_cards.HomeCards, 'cards', one_menu)
    generation = None
    for index in range(home_prepared.MAX_MENUS + 2):
        at = 1200 + index
        current, expired = f'admission:{index}', f'expired:{index}'
        old = at - (rec_params.get_param(store, 'choice_retention_d') + 1) * 86400
        for oid, offered_at in ((current, at), (expired, old)):
            assert choice_evidence.issue(store, 'albums', [{'id': oid}], offered_at,
                                         context={}, opportunity_id=oid) == oid
        choice_evidence.refresh_report(store, at)
        choice_evidence.maintain_storage(store, at)
        assert store.rec_ledger.evidence().get('opportunity', expired) is None
        assert store.rec_ledger.evidence().get('opportunity', current)
        choice_evidence.refresh_report(store, at)
        payload = home_prepared.prepare(store, at)
        assert payload and payload['group_policy'] == 'adaptive'
        assert any(menu['protos'] for menu in payload['menus'])
        assert len(payload['menus']) == min(index + 1, home_prepared.MAX_MENUS)
        generation = generation or payload['generation']
        assert payload['generation'] == generation
        assert original_pools == {name: store.rec_proposals._proposal_json(name) for name in pools}
        assert candidate_refresh.preferences(store, at) == preferences
    assert {menu['epoch'] for menu in payload['menus']} == set(range(home_prepared.MAX_MENUS))


def test_home_worker_retries_partial_adaptive_menus_until_full(store):
    from yt_playlist.rec.worker_tasks import ServingTasks
    library(store)
    calls = []
    payload = {'group_policy': 'adaptive', 'modes': [], 'menus': [{}], 'play_exposure': {}}

    def stage(name, target, *args, **kwargs):
        calls.append(name)
        if name == 'Home collections':
            return payload
        if name == 'Album choices':
            return {'items': []}
        return None

    tasks = ServingTasks(SimpleNamespace(store=store, now_fn=lambda: 1200,
        client_provider=lambda: {}, logger=logging.getLogger(__name__)), stage)
    assert tasks.home()
    payload['menus'] = [{} for _ in range(home_prepared.MAX_MENUS)]
    assert not tasks.home()
    assert calls.count('Home collections') == 2


def test_muting_and_suppression_are_checked_again_after_group_preparation(store):
    tracks, _head = library(store)
    store.rec_model.set_weight('genre:jazz', 0, lo=0, now=1200)
    store.rec_model.record_feedback('for_you', tracks[0].key, 'dislike', now=1200)
    with store.conn:
        store.conn.execute("UPDATE youtube_items SET available=0 WHERE video_id='v1'")
    data = group_home.inputs(store, 1200)
    cards = group_home.assemble(store, 1200, 0, data)
    assert cards and all(t['genre'] != 'Jazz' for p in cards for t in p['tracks'])
    assert all(t['key'] not in {tracks[0].key, tracks[1].key} for p in cards for t in p['tracks'])


def test_active_preparation_freezes_graph_inputs_and_does_not_capture_legacy_models(store, monkeypatch):
    _tracks, head = library(store)
    from yt_playlist.repos.rec_proposals import RecProposalsRepo
    original = RecProposalsRepo.get_proposals
    preview_reads = []

    def read(self, surface):
        if surface == group_serving.SURFACE:
            preview_reads.append(surface)
        return original(self, surface)

    def legacy_capture(*args, **kwargs):
        raise AssertionError('Graph offers must not capture the unused legacy selection model')
    monkeypatch.setattr(RecProposalsRepo, 'get_proposals', read)
    monkeypatch.setattr(choice_evidence, 'model_context', legacy_capture)
    prepared = home_prepared.prepare(store, 1200)
    assert prepared and prepared['choice_context']['group_generation'] == head['generation']
    assert len(prepared['menus']) > 1
    assert len(preview_reads) == 1
    context = choice_evidence.resolve_context(store, prepared['choice_context'])
    assert context['at'] == 1200 and abs(sum(context['group_allocation'].values()) - 1) < 1e-9
    assert context['musical_groups'] and all('choice_context' not in m for m in prepared['menus'])


def test_rollback_rejects_cached_adaptive_menus_but_keeps_definitions(store):
    _tracks, head = library(store)
    assert home_prepared.prepare(store, 1200)
    store.settings.set_setting('rec_group_serving', 'legacy')
    scheduled = []
    assert home_prepared.load(store, 1300, lambda: scheduled.append(True)) is None
    assert scheduled and store.taste_groups.head_generation() == head['generation']


def test_recent_listening_is_mapped_to_exact_graph_memberships_without_vectors(store):
    tracks, _head = library(store)
    identity = store.identities.upsert_identity('main', 'cred', None, True)
    for i, track in enumerate(tracks[:6]):
        store.history.record_play_event(identity, track.key, f'v{i}', 1200 + i)
    reading = group_viz.mode_layers(store, 1300, group_viz.prepared(store))
    assert reading['now']['n'] == 6
    assert reading['now']['segments'] and all(s['label'] == 'House' for s in reading['now']['segments'])


def test_definitions_survive_regrouping_and_held_offers_keep_their_context(store):
    tracks, head = library(store)
    first = home_prepared.prepare(store, 1200)
    original = home_prepared.cards(store, first, 1300, service(store))
    historical = deepcopy(original.choice_context)
    publish(store, tracks[:-4], 1400, head)
    assert group_serving.prepare(store, 1450)
    replacement = home_prepared.prepare(store, 1500)
    retained = home_prepared.cards(store, replacement, 1600, service(store))
    assert retained.choice_context == historical
    assert [p['group_version'] for p in retained.protos] == [p['group_version'] for p in original.protos]
    assert store.taste_groups.definition(original.protos[0]['group_version'])


def test_taste_main_views_show_served_graph_and_hide_fixed_count_controls(store):
    _tracks, head = library(store)
    view = group_viz.mode_layers(store, 1200, group_viz.prepared(store))
    assert view['adaptive']
    assert sum(m['size'] for m in view['modes']) == 64
    assert abs(sum(s['share'] for s in view['permanent']['segments']) - 1) < 1e-9
    app = create_app(store, lambda: {}, now_fn=lambda: 1200)
    with TestClient(app, base_url='http://127.0.0.1') as client:
        groups = client.get('/taste/panels/groups')
        assert 'Home’s Mix tapes now draw from these groups' in groups.text
        assert head['generation'] in groups.text
        assert 'Find a group or artist' in groups.text
        profile = client.get('/taste/panels/profile')
        assert profile.status_code == 200 and 'dynamic music groups used by Home' in profile.text
        overview = client.get('/taste/panels/overview')
        assert overview.status_code == 200 and 'Your music groups' in overview.text
        controls = client.get('/taste/panels/recommendations')
        assert 'id="param-modes_k"' not in controls.text
        assert 'id="param-ppr_ab_share"' not in controls.text
        generated = client.get('/taste/panels/generated')
        assert generated.status_code == 200 and 'Dynamic group allocation' in generated.text
