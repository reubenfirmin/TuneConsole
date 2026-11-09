import json
import logging
from contextlib import closing, nullcontext
from copy import deepcopy
from html.parser import HTMLParser
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from tests.conftest import FakeClient, seed_catalog_track
from tests.group_support import publish, recordings
from tests.genre_catalog_support import bound_catalog
from tests.home_fixture_support import home_page_bytecode, returning_listener, without_metadata_providers
from tests.playlist_fixture_support import library_catalog
from tests.prepared_fixture_support import prepared_template
from tests.rec.test_home_prepared import one_menu_per_attempt
from tests.recommendation_support import publish_bundles, seed_publication_subjects
from tests.store_fixture_support import (
    bounded_consumer_schema, initialize_consumer, mock_snapshot_coordinator, reuse_reporting_reader,
    native_sql_calls, without_query_timing,
)
from yt_playlist.core import mix_choices
from yt_playlist.core import schema_inspection
from yt_playlist.core.store import Store
from yt_playlist.rec import (choice_evidence, group_home, group_serving, group_viz,
                             home_cache, home_cards, home_prepared, taste_viz)
from yt_playlist.web.app import create_app


@pytest.fixture
def adaptive_selection(tmp_path, _pristine_current_schema, monkeypatch,
                       native_sql_calls, without_query_timing):
    """Build the full graph directly below its uncontended Home consumers."""
    from unittest.mock import Mock
    from yt_playlist.core import bounded_json, home_outputs, json_cache
    from yt_playlist.repos.group_ordering import GroupOrderingRepo
    from yt_playlist.rec import measurement, stored_groups

    bound_catalog(monkeypatch, entries=(('house', 'House'), ('jazz', 'Jazz')))
    # The input documents are trusted fixture output. Codec admission has its
    # own tests; retain actual encoding, file publication and full decoding.
    monkeypatch.setattr(json_cache, '_check_profile_expansion', lambda *args, **kwargs: None)
    monkeypatch.setattr(home_outputs, '_validate_numbers', lambda value: None)

    def fixture_json(encoded, *, keys=None, finite=False):
        value = json.loads(encoded)
        return ({key: item for key, item in value.items() if key in keys}
                if keys is not None and isinstance(value, dict) else value)

    monkeypatch.setattr(bounded_json, 'loads', fixture_json)
    # No sound preset is selected here. The group selector never ranks through
    # its lower scratch repository, but otherwise creates twelve disk databases.
    monkeypatch.setattr(GroupOrderingRepo, 'open',
                        lambda: nullcontext(Mock(spec=GroupOrderingRepo)))
    store = Store(tmp_path / 'adaptive-selection.db')
    with closing(store.conn), schema_inspection.reuse_tables(store.conn):
        store.conn.execute('PRAGMA synchronous=OFF')
        initialize_consumer(store, _pristine_current_schema)
        store.conn.execute('PRAGMA wal_autocheckpoint=0')
        monkeypatch.setattr(measurement, 'serving_policy',
                            lambda *args, **kwargs: ('graph-input', {'ranker': 'graph-input'}))
        facts, candidate_facts = {}, type(store.recommendations)._candidate_facts

        def fixed_catalog(controller, keys):
            missing = [key for key in keys if key not in facts]
            if missing:
                facts.update(candidate_facts(controller, missing))
            return {key: facts[key] for key in keys}

        monkeypatch.setattr(type(store.recommendations), '_candidate_facts', fixed_catalog)
        pages, read_pages = {}, stored_groups.pages

        def fixed_pages(reader, generation, version, lane):
            key = generation, version, lane
            if key not in pages:
                pages[key] = tuple(read_pages(reader, generation, version, lane))
            return deepcopy(pages[key])

        # These cases keep their catalog and candidate generation fixed.
        # Supply its complete pages once; graph allocation, rotation and
        # every one of the twelve menus still run through the real code.
        monkeypatch.setattr(stored_groups, 'pages', fixed_pages)
        with reuse_reporting_reader(store, monkeypatch):
            # Publishing fixture inputs does not own source-change races.
            # Restore real guarded snapshots before exercising Home preparation.
            with monkeypatch.context() as patch, mock_snapshot_coordinator(store, patch):
                tracks, head = library(store)
            # These cases own graph menus; independent song-summary producers
            # consume completed output below that boundary.
            monkeypatch.setattr(home_prepared, 'prepare_discovery', lambda *args: None)
            monkeypatch.setattr(home_prepared, 'prepare_revisit', lambda *args: None)
            monkeypatch.setattr(home_cards, '_order_by_journey',
                                lambda owner, items, *args, **kwargs: list(items))
            yield store, tracks, head


@pytest.fixture(scope='module')
def _pristine_current_schema():
    with bounded_consumer_schema((('house', 'House'), ('jazz', 'Jazz'))) as schema:
        yield schema


def test_adaptive_schema_input_keeps_current_history_and_read_only_sql(_pristine_current_schema):
    from yt_playlist.core.migrations import registry, regular

    source = _pristine_current_schema
    assert source.execute('PRAGMA query_only').fetchone()[0] == 1
    assert source.execute('SELECT max(version) FROM schema_migrations').fetchone()[0] == len(registry.MIGRATIONS)
    assert source.execute('SELECT max(version) FROM schema_changes').fetchone()[0] == len(regular.CHANGES)
    assert not source.execute('PRAGMA foreign_key_check').fetchone()


@pytest.fixture(scope='module')
def adaptive_library_template(_pristine_current_schema):
    with prepared_template(_pristine_current_schema, library) as template:
        yield template


@pytest.fixture
def group_pages(monkeypatch, home_page_bytecode, without_metadata_providers):
    from fastapi import APIRouter
    from yt_playlist.web import app as webapp
    from yt_playlist.web.routes import app_update, audio_preview, home, taste

    monkeypatch.setattr(webapp, 'build_all', lambda ctx: [module.build(ctx)
        for module in (app_update, audio_preview, home, taste)])
    monkeypatch.setattr(webapp, 'build_bridge_route', lambda ctx: APIRouter())
    monkeypatch.setattr(webapp, '_install_idle_shutdown', lambda *args: None)
    templates = webapp.Jinja2Templates

    def compiled_templates(*args, **kwargs):
        result = templates(*args, **kwargs)
        result.env.bytecode_cache = home_page_bytecode
        return result

    monkeypatch.setattr(webapp, 'Jinja2Templates', compiled_templates)


def library(store):
    tracks = recordings([('House', 32, .1), ('Jazz', 32, .9)])
    pool = []
    for i, track in enumerate(tracks):
        title, artist, album = f'Song {i}', f'Artist {i // 2}', f'Album {i // 2}'
        pool.append({'key': track.key, 'title': title, 'artist': artist, 'album': album,
                     'video_id': f'v{i}', 'thumbnail': '', 'genre': track.genre, 'year': 2000})
    # Graph selection consumes a completed catalog. Retain all 64 subjects,
    # their genre/album fields and actual artist identities in one input batch.
    from yt_playlist.core import artists
    from yt_playlist.core.db_writes import atomic_write

    seed_publication_subjects(store, [{**track, 'year': None} for track in pool])
    with atomic_write(store):
        for row in store.conn.execute('SELECT id,artist FROM recordings'):
            artists.observe(store.conn, row['id'], row['artist'])
    head = publish(store, tracks)
    publish_bundles(store, {'all': {'wheelhouse': pool, 'explore': pool,
        'comfort': pool, 'fresh': []}, '_meta': {'full_pools': True, 'comfort_pool': 64}}, 1050)
    assert group_serving.prepare(store, 1100)
    return tracks, head


def test_adaptive_library_input_keeps_exact_graph_and_read_only_custody(adaptive_library_template):
    source = adaptive_library_template.source
    tracks, head = adaptive_library_template.result
    assert len(tracks) == 64
    assert source.taste_groups.head_generation() == head['generation']
    assert source.conn.execute('PRAGMA query_only').fetchone()[0] == 1
    assert not source.conn.execute('PRAGMA foreign_key_check').fetchone()


@pytest.fixture
def adaptive_library(store, adaptive_library_template):
    return adaptive_library_template.restore(store)


def service(store):
    return home_cards.HomeCards(store, home_prepared._NoJobs(), logging.getLogger(__name__))


@pytest.fixture(scope='module')
def adaptive_menu_template(_pristine_current_schema, adaptive_library_template):
    def build(source):
        tracks, head = adaptive_library_template.restore(source)
        result = service(source).cards(home_cards.CardsRequest(1200, record=False))
        return tracks, head, result

    # The unchanged initial selection is shared by its exact-membership check
    # and the regrouping consumer. Every consumer still owns private SQL/files.
    with prepared_template(_pristine_current_schema, build) as template:
        yield template


@pytest.fixture
def adaptive_menu(store, adaptive_menu_template):
    return adaptive_menu_template.restore(store)


def test_adaptive_menu_input_keeps_graph_context_and_complete_selections(adaptive_menu_template):
    _tracks, head, result = adaptive_menu_template.result
    assert result.choice_context['group_generation'] == head['generation']
    assert len(result.protos) >= 2
    assert all(len(proto['tracks']) == 12 for proto in result.protos)
    assert adaptive_menu_template.source.conn.execute('PRAGMA query_only').fetchone()[0] == 1


@pytest.fixture(scope='module')
def adaptive_batch_template(_pristine_current_schema, adaptive_menu_template):
    def build(source):
        _tracks, head, completed = adaptive_menu_template.restore(source)
        # Establish the initial learning report before freezing the first menu.
        # Its first publication initializes preference state even with no choices.
        choice_evidence.refresh_report(source, 1200)
        selected = deepcopy(completed)
        clock = [100]

        def one_menu(self, request):
            result = deepcopy(selected)
            result.epoch = request.epoch
            clock[0] += 6
            return result

        with pytest.MonkeyPatch.context() as patch:
            patch.setattr(home_prepared.time, 'monotonic', lambda: clock[0])
            patch.setattr(home_prepared, 'PREPARE_BUDGET_S', 10)
            patch.setattr(home_cards.HomeCards, 'cards', one_menu)
            payload = home_prepared.prepare_mixes(source, 1200)
        assert payload and len(payload['menus']) == 1
        return head, completed, payload

    with prepared_template(_pristine_current_schema, build) as template:
        yield template


@pytest.fixture
def adaptive_batch(adaptive_batch_template):
    with adaptive_batch_template.open(':memory:') as store:
        yield store, deepcopy(adaptive_batch_template.result)


def test_adaptive_batch_input_keeps_usable_checkpoint_and_original_graph(adaptive_batch_template):
    head, _result, payload = adaptive_batch_template.result
    assert home_prepared._valid(payload)
    assert payload['choice_context']['group_generation'] == head['generation']
    assert len(payload['menus']) == 1
    assert adaptive_batch_template.source.conn.execute('PRAGMA query_only').fetchone()[0] == 1


def test_default_home_offers_use_exact_graph_definitions_without_legacy_ids(adaptive_batch):
    store, (head, result, payload) = adaptive_batch
    # Own both the real initial graph selection and its first usable worker
    # checkpoint; history cases consume independent clones of this state.
    assert home_prepared._valid(payload)
    assert home_cache.read(store, home_prepared.SURFACE) == payload
    assert payload['choice_context']['group_generation'] == head['generation']
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


def test_cached_group_headings_are_served_without_rewriting_the_prepared_set(adaptive_selection):
    store, _tracks, head = adaptive_selection
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


def test_prepared_home_and_real_menu_capture_preserve_group_context(store, adaptive_library, group_pages):
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


def test_history_only_refresh_and_retirement_accumulate_all_adaptive_menus(
        store, monkeypatch, adaptive_library):
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

    # Exercise every real partial batch and both attempts after capacity is full.
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
    protos = [{'lane': lane, 'tracks': [{'key': lane}]}
              for lane in ('wheelhouse', 'explore', 'fresh', 'comfort')]
    payload = {'group_policy': 'adaptive', 'modes': [],
               'menus': [{'protos': protos[:3]}], 'play_exposure': {}}

    def stage(name, target, *args, **kwargs):
        calls.append(name)
        if name == 'Home collections':
            return payload
        if name == 'Album choices':
            return {'items': []}
        if name == 'Home Discovery':
            return {}
        if name == 'Home revisit':
            return True
        return None

    tasks = ServingTasks(SimpleNamespace(store=store, now_fn=lambda: 1200,
        client_provider=lambda: {}, logger=logging.getLogger(__name__)), stage)
    assert tasks.home()
    payload['menus'] = [{'protos': protos[:3]} for _ in range(home_prepared.MAX_MENUS)]
    assert tasks.home()
    payload['menus'] = [{'protos': protos}]
    assert not tasks.home()
    assert calls.count('Home collections') == 3


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


def test_active_preparation_freezes_graph_inputs_and_does_not_capture_legacy_models(adaptive_selection, monkeypatch):
    store, _tracks, head = adaptive_selection
    from yt_playlist.controllers.recommendation_proposals import RecommendationProposals
    original = RecommendationProposals.get_proposals
    preview_reads = []

    def read(self, surface):
        if surface == group_serving.SURFACE:
            preview_reads.append(surface)
        return original(self, surface)

    def legacy_capture(*args, **kwargs):
        raise AssertionError('Graph offers must not capture the unused legacy selection model')
    monkeypatch.setattr(RecommendationProposals, 'get_proposals', read)
    monkeypatch.setattr(choice_evidence, 'model_context', legacy_capture)
    prepared = home_prepared.prepare(store, 1200)
    assert prepared and prepared['choice_context']['group_generation'] == head['generation']
    assert len(prepared['menus']) > 1
    assert len(preview_reads) == 1
    context = choice_evidence.resolve_context(store, prepared['choice_context'])
    assert context['at'] == 1200 and abs(sum(context['group_allocation'].values()) - 1) < 1e-9
    assert context['musical_groups'] and all('choice_context' not in m for m in prepared['menus'])


def test_rollback_rejects_cached_adaptive_menus_but_keeps_definitions(adaptive_selection):
    store, _tracks, head = adaptive_selection
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
        store, adaptive_menu, one_menu_per_attempt, monkeypatch):
    tracks, head, assembled = adaptive_menu
    # Reordering selected songs does not establish historical group custody.
    # Keep the real graph selection, card/group framing and cache publication;
    # journey ordering has independent checks in test_journeys.
    monkeypatch.setattr(home_cards, '_order_by_journey',
                        lambda owner, items, *args, **kwargs: list(items))
    # Consume one real assembled menu through the ordinary cache writer.
    # Batch enumeration is independent of retaining an already displayed offer.
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


def test_taste_main_views_show_served_graph_and_hide_fixed_count_controls(store, adaptive_library, group_pages):
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
