import json
import logging
from copy import deepcopy
from html.parser import HTMLParser
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from tests.conftest import FakeClient, seed_catalog_track
from tests.group_support import publish, recordings
from tests.home_fixture_support import returning_listener
from tests.playlist_fixture_support import library_catalog
from tests.rec.test_home_prepared import one_menu_per_attempt
from tests.recommendation_support import publish_bundles
from yt_playlist.core import mix_choices
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
    publish_bundles(store, {'all': {'wheelhouse': pool, 'explore': pool,
        'comfort': pool, 'fresh': []}, '_meta': {'full_pools': True, 'comfort_pool': 64}}, 1050)
    assert group_serving.prepare(store, 1100)
    return tracks, head


@pytest.fixture
def adaptive_library(store, adaptive_library_template):
    return adaptive_library_template.restore(store)


def service(store):
    return home_cards.HomeCards(store, home_prepared._NoJobs(), logging.getLogger(__name__))


def test_default_home_offers_use_exact_graph_definitions_without_legacy_ids(store, adaptive_library):
    _tracks, head = adaptive_library
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


def test_cached_group_headings_are_served_without_rewriting_the_prepared_set(store, adaptive_library):
    _tracks, head = adaptive_library
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
        assert proto['note'] == names[proto['group_version']]
        assert proto['name'].startswith(f"{proto['note']} - {proto['label']} - ")
        assert proto['recipe']['group_generation'] == head['generation']
    shown = [(p['name'], p['note'], p['tracks']) for p in result.protos]
    assert any([(p['name'], p['note'], p['tracks']) for p in menu['protos']] == shown
               for menu in original['menus'])
    assert payload == original


def test_filters_do_not_turn_small_interests_into_unrelated_mixes(store, adaptive_library):
    data = group_home.inputs(store, 1200)
    cards = group_home.assemble(store, 1200, 0, data, include={'jazz'})
    assert cards and all(p['group_name'] == 'Jazz' for p in cards)
    assert all(t['genre'] == 'Jazz' for p in cards for t in p['tracks'])
    assert not group_home.assemble(store, 1200, 0, data, include={'metal'})


def test_rollback_and_old_preview_schema_do_not_activate_graph_serving(store, adaptive_library):
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
    publish_bundles(store, {
        'all': {'wheelhouse': pool, 'explore': pool, 'comfort': [], 'fresh': []},
        '_meta': {'full_pools': True, 'unmodeled': True}}, 1000)
    assert group_serving.prepare(store, 1100)
    assert group_serving.view(store)['counts']['known'] == 4
    assert group_serving.policy(store) == 'legacy'
    prepared = home_prepared.prepare(store, 1200)
    assert prepared and any(m['protos'] for m in prepared['menus'])


def test_prepared_home_and_real_menu_capture_preserve_group_context(store, adaptive_library):
    _tracks, head = adaptive_library
    payload = home_prepared.prepare(store, 1200)
    assert payload and payload['group_policy'] == 'adaptive'
    assert home_prepared._valid(payload)
    assert payload['choice_context']['group_generation'] == head['generation']
    identity = store.accounts.upsert_account('main', 'cred', None, True)
    returning_listener(store, 'mixes', now=1200)
    fake = FakeClient(catalog=library_catalog(store))
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
        assert all(o['descriptor_id'] for o in options)
        assert all(not {'group_version', 'recipe', 'tracks'} & o.keys() for o in options)
        ids = [o['id'] for o in options]
        choice_evidence.respond(store, menu['id'], 'visible', 1301, visible=ids)
        clock[0] = 1302
        # The browser submits its operational card. Captured choices retain
        # visible themes/genres, never hidden recipes or track memberships (#688).
        class Cards(HTMLParser):
            def __init__(self):
                super().__init__()
                self.cards = []

            def handle_starttag(self, tag, attrs):
                attrs = dict(attrs)
                if tag == 'div' and attrs.get('data-recipe'):
                    self.cards.append({'recipe': json.loads(attrs['data-recipe']), 'tracks': []})
                if tag == 'li' and attrs.get('class') == 'gen-row':
                    self.cards[-1]['tracks'].append({key: attrs['data-' + attr] for key, attr in (
                        ('key', 'key'), ('video_id', 'vid'), ('title', 'title'), ('artist', 'artist'),
                        ('album', 'album'), ('thumbnail', 'thumb'))})

        rendered = Cards()
        rendered.feed(response.text)
        card = next(card for card in rendered.cards if card['recipe']['option_id'] == ids[0])
        recipe = card['recipe']
        assert recipe['opportunity_id'] == menu['id'] and card['tracks']
        generated = client.post('/home/generate', data={'name': 'Group soak mix',
            'tracks': json.dumps(card['tracks']), 'recipe': json.dumps(recipe)})
        assert generated.status_code == 200
        created, = fake.created
        remote_id, title, _description = created
        expected = [track['video_id'] for track in card['tracks']]
        assert len(expected) == len(set(expected))
        assert fake.added == [(remote_id, expected)]
        assert [track['videoId'] for track in fake.get_playlist(remote_id, limit=None)['tracks']] == expected
        saved = store.playlists.get_playlist_for_account(identity, remote_id)
        assert saved.title == title and saved.track_count == len(expected)
        assert [track['video_id'] for track in store.charts.playlist_tracks_detail(saved.id)] == expected
        assert store.playlists.get_playlist_groups()[remote_id] == 'Generated'
        assert f'data-generated-playlist="{remote_id}"' in generated.text
        assert f'window.location.assign("/playlist/{saved.id}")' in generated.text
        from yt_playlist.util.action_kinds import COPY_PLAYLIST
        action, = [action for action in store.actions.get_actions() if action.kind == COPY_PLAYLIST]
        assert action.status == 'executed'
        undo, params = json.loads(action.undo_json), json.loads(action.params_json)
        assert undo['new_ytm'] == remote_id and undo['target_identity'] == identity
        assert undo['post']['videos'] == expected
        assert params['added'] == len(expected) and params['skipped'] == 0 and not params['partial']
        choice_evidence.daily(store, 1303)
        state = store.rec_proposals.get_proposals('group_choice_bandit')
        genres = {g.casefold() for g in mix_choices.expand(store.conn, options[0])['genres']}
        matches = [g for g in head['groups'] if g.get('leaf') and not g.get('legacy')
                   and genres & {label.casefold() for label, _weight in g['genres']}]
        assert len(matches) == 1
        chosen = next(r for r in state['rows'] if r['version'] == matches[0]['version'])
        assert chosen['positive'] > 0 and chosen['picked'] == 1
        assert state['excluded']['unmapped_definition'] == 0
        assert taste_viz.mix_choices(store, 1303)['picked'] == 1
        choices = client.get('/taste/panels/generated')
        assert choices.status_code == 200
        assert 'Mix options confirmed visible in the browser' in choices.text
        assert 'gives a legacy score' not in choices.text
        assert 'Server-rendered Home offers' not in choices.text


def test_history_only_refresh_and_retirement_accumulate_all_adaptive_menus(store, monkeypatch, adaptive_library):
    from yt_playlist.rec import candidate_refresh, rec_params
    assert store.rec_proposals.get_proposals(home_prepared.SURFACE) is None
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


def test_home_worker_retries_partial_adaptive_menus_until_full(store, adaptive_library):
    from yt_playlist.rec.worker_tasks import ServingTasks
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


def test_muting_and_suppression_are_checked_again_after_group_preparation(store, adaptive_library):
    tracks, _head = adaptive_library
    store.rec_model.set_weight('genre:jazz', 0, lo=0, now=1200)
    store.rec_model.record_feedback('for_you', tracks[0].key, 'dislike', now=1200)
    with store.conn:
        store.conn.execute("UPDATE youtube_items SET available=0 WHERE video_id='v1'")
    data = group_home.inputs(store, 1200)
    cards = group_home.assemble(store, 1200, 0, data)
    assert cards and all(t['genre'] != 'Jazz' for p in cards for t in p['tracks'])
    assert all(t['key'] not in {tracks[0].key, tracks[1].key} for p in cards for t in p['tracks'])


def test_active_preparation_freezes_graph_inputs_and_does_not_capture_legacy_models(store, monkeypatch, adaptive_library):
    _tracks, head = adaptive_library
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


def test_rollback_rejects_cached_adaptive_menus_but_keeps_definitions(store, adaptive_library):
    _tracks, head = adaptive_library
    assert home_prepared.prepare(store, 1200)
    store.settings.set_setting('rec_group_serving', 'legacy')
    scheduled = []
    assert home_prepared.load(store, 1300, lambda: scheduled.append(True)) is None
    assert scheduled and store.taste_groups.head_generation() == head['generation']


def test_recent_listening_is_mapped_to_exact_graph_memberships_without_vectors(store, adaptive_library):
    tracks, _head = adaptive_library
    identity = store.accounts.upsert_account('main', 'cred', None, True)
    for i, track in enumerate(tracks[:6]):
        store.history.record_play_event(identity, track.key, f'v{i}', 1200 + i)
    reading = group_viz.mode_layers(store, 1300, group_viz.prepared(store))
    assert reading['now']['n'] == 6
    assert reading['now']['segments'] and all(s['label'] == 'House' for s in reading['now']['segments'])


def test_definitions_survive_regrouping_and_held_offers_keep_their_context(
        store, adaptive_library, one_menu_per_attempt):
    tracks, head = adaptive_library
    # Consume one real assembled menu through the ordinary cache writer.
    # Batch enumeration is independent of retaining an already displayed offer.
    assembled = service(store).cards(home_cards.CardsRequest(1200, record=False))
    assert assembled.protos and assembled.choice_context['group_generation'] == head['generation']
    assert home_prepared.remember(store, None, assembled, 1200)
    with store.read_view() as reader:
        first = home_prepared.peek(reader, 1300)
        original = home_prepared.cards(reader, first, 1300, service(reader), record=False)
    historical = deepcopy(original.choice_context)
    regrouped = publish(store, tracks[:-4], 1400, head)
    assert regrouped['generation'] != head['generation']
    assert {p['group_version'] for p in original.protos} - {g['version'] for g in regrouped['groups']}
    assert group_serving.prepare(store, 1450)
    replacement = home_prepared.prepare_mixes(store, 1500)
    assert replacement
    with store.read_view() as reader:
        retained = home_prepared.cards(reader, replacement, 1600, service(reader), record=False)
    assert retained.choice_context == historical
    assert [p['group_version'] for p in retained.protos] == [p['group_version'] for p in original.protos]
    assert store.taste_groups.definition(original.protos[0]['group_version'])


def test_taste_main_views_show_served_graph_and_hide_fixed_count_controls(store, adaptive_library):
    _tracks, head = adaptive_library
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
