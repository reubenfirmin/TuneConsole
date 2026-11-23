"""Complete Home commands over bounded collaborators, with SQLite forbidden."""
from contextlib import contextmanager, nullcontext
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from tests.repository_mock_support import no_database, repository_store
from yt_playlist.controllers.album_menu import AlbumMenu
from yt_playlist.controllers.home_board import HomeBoard
from yt_playlist.controllers.home_navigation import HomeNavigation
from yt_playlist.controllers.home_tuning import HomeTuning
from yt_playlist.rec import album_choices, album_tuning, home_cards, mix_tuning, vibe_readiness

pytestmark = pytest.mark.usefixtures('no_database')


def board(result):
    calls = []
    def effect(name, value=None):
        def run(*args, **kwargs):
            calls.append((name, args, kwargs))
            return value
        return run
    command = HomeBoard(load=effect('load', {'genre_options': [{'family': 'jazz'}]}),
        cards=effect('cards', result), starter=lambda value, now: value,
        present=effect('present'), audit=effect('audit'), tuning=lambda: True, remaining=lambda now: 12,
        fingerprint=lambda payload, now: {}, modes=lambda: True, current_vibe=lambda now: 'relax',
        discovery=lambda payload, now: {}, artist_images=effect('images'), busy=lambda: False,
        pool=effect('pool'), refresh=effect('refresh', result.protos[0]), ledger=Mock(spec_set=['refresh_card']),
        parameter=lambda name: 5, close=effect('close'), epoch=lambda: 4, start_pool=effect('start_pool', 'job-1'))
    return command, calls


def test_live_board_captures_exposure_and_audit_for_an_explicit_adjustment():
    result = home_cards.CardsResult([{'lane': 'wheelhouse', 'tracks': [{'key': 'exact recording'}]}], [],
                                   None, None, [], [], 0, '')
    command, calls = board(result)
    model = command.query(None, 1000, changed=True, path='/home/vibe')
    assert [name for name, _, _ in calls] == ['cards', 'present', 'audit']
    assert calls[1][1] == (result, 1000)
    assert calls[-1][1] == (result.protos, 1000, '/home/vibe')
    assert model['protos'] == result.protos


def test_rotation_closes_current_window_and_rejects_stale_pool_epoch_without_starting_work():
    command, calls = board(home_cards.CardsResult([{'lane': 'comfort'}], [], None, None, [], [], 0, ''))
    command.refresh_all(1000, 'held')
    command.ledger.refresh_card.assert_called_once_with('cards', 5, 1000)
    assert calls[0] == ('close', (1000,), {'opportunity_id': 'held'})
    parameters = {'lane': 'comfort', 'epoch': 3, 'include_genres': ['jazz'], 'avoid_genres': []}
    with pytest.raises(ValueError):
        command.request_pool('library_pool', parameters, [], 1000)
    assert all(name != 'start_pool' for name, _, _ in calls)
    assert command.request_pool('library_pool', {**parameters, 'epoch': 4}, ['exact recording'], 1000) == 'job-1'
    assert calls[-1][1][2] == ['exact recording']


def test_album_query_selects_real_menu_and_captures_evidence_notes_listening():
    items = [{'browse_id': f'album-{i}', 'title': f'Album {i}', 'artist': f'Artist {i}',
              'genre': 'Jazz', 'score': 1, 'features': {}} for i in range(8)]
    payload = {'items': items, 'context': {'score_generation': 'ordinary'}}
    store = repository_store(rec_proposals={'get_proposals': {}, 'put_proposals': None},
        notes={'request': None}, ledger={}, genres={'feature_axes': {'Jazz': ('jazz', None)}}, settings={})
    present, schedule, listening = Mock(return_value='held-menu'), Mock(), Mock(side_effect=lambda ids: {
        key: {'complete': True, 'tracks': 2, 'plays': 4} for key in ids})
    def select(pool, now, controls, epoch, genres, **kwargs):
        selected, policy = album_choices.menu(pool, controls, epoch, genres,
            exclude=kwargs['exclude'], genre_axes={'Jazz': ('jazz', None)})
        return pool, selected, policy
    command = AlbumMenu(store.rec_proposals, store.notes, store.ledger, store.genres, store.settings,
        prepared=lambda now: payload, current=lambda: {}, epoch=lambda: 2,
        tuning=lambda now: {'values': {'novelty': .5}, 'genres': [], 'remaining': 25},
        defaults=lambda: {'novelty': .5}, excluded=lambda epoch: [], select=select,
        listening=listening, read_notes=lambda album: 'Prepared note', present=present, close=Mock(),
        opportunity=Mock(), schedule=schedule, worker_available=True, logger=Mock())
    model = command.query(1000)
    assert model['albums'] and len(model['albums']) <= 8
    assert model['opportunity_id'] == 'held-menu'
    assert all(album['familiarity']['level'] == 'familiar' for album in model['albums'])
    assert all(album['notes'] == 'Prepared note' for album in model['albums'])
    offered = present.call_args.args[0]
    assert [row['id'] for row in offered] == [row['browse_id'] for row in model['albums']]
    assert present.call_args.kwargs['context']['album_menu']['temporary_until'] == 1025
    store.notes.request.assert_called_once()
    assert present.call_args.kwargs['context']['score_generation'] == 'ordinary'
    store.rec_proposals.put_proposals.assert_not_called()


def test_tuning_changes_effective_weight_and_pinning_preserves_existing_lean():
    store = repository_store(settings={'get_setting': None}, model={'get_weights': {'genre:jazz': 2.},
        'set_lean': None, 'unhide_facet': None, 'get_lean': 1.4, 'get_leans': {'genre:jazz': 1.4}},
        proposals={}, ledger={}, genres={})
    command = HomeTuning(store.settings, store.model, store.proposals, store.ledger, store.genres,
        adjusting=lambda now: nullcontext(), parameter=lambda name: 30., set_parameter=Mock(), epoch=lambda: 0,
        refresh=Mock(), prepared=Mock(), picker=Mock(), expire=Mock(), reset=Mock(), fingerprint=Mock())
    command.steer('genre:jazz', 3., 1000)
    store.model.set_lean.assert_called_once_with('genre:jazz', 1.5, 1000)
    command.pin('genre:jazz', 1001)
    assert store.model.set_lean.call_count == 1
    assert mix_tuning.expiry_values('0', None, mix_tuning.TTL_S)['vibe']
    independent = mix_tuning.expiry_values('0', str(mix_tuning.TTL_S), mix_tuning.TTL_S)
    assert independent['ordinary'] and not independent['vibe']


def test_navigation_bounds_story_visibility_and_rejects_unconfirmed_playback():
    store = repository_store(settings={'set_setting': None}, proposals={'get_proposals': {
        'cards': [{'subject': 'known'}]}}, ledger={'record_story_views': None})
    respond, refresh = Mock(), Mock()
    command = HomeNavigation(store.settings, store.proposals, store.ledger, retained=lambda panel: False,
        stories=Mock(), epoch=lambda: 0, respond=respond, refresh=refresh)
    command.story_impressions(['known', 'forged'], 10)
    store.ledger.record_story_views.assert_called_once_with({'known'}, 10)
    assert not command.response('held', 'option', 'start', 11)
    respond.assert_not_called()
    assert command.response('held', 'option', 'choice', 12)
    respond.assert_called_once_with('held', 'choice', 12, option_id='option', visible=())
    refresh.assert_called_once()


def test_readiness_rule_requires_full_supported_and_distinct_menus():
    def menu(prefix):
        return [{'tracks': [{'key': f'{prefix}-{i}-{j}', 'supported': ['relax', 'energize']}
                            for j in range(4)]} for i in range(4)]
    first = menu('first')
    assert not vibe_readiness.ready_values([('relax', first), ('energize', first)])
    assert vibe_readiness.ready_values([('relax', first), ('energize', menu('second'))])
    first[0]['tracks'][0]['supported'] = []
    assert not vibe_readiness.ready_values([('relax', first), ('energize', menu('second'))])


def test_complete_home_query_captures_page_then_presenter_renders_supplied_model():
    from pathlib import Path
    from jinja2 import Environment, FileSystemLoader
    from yt_playlist.controllers.home_query import HomeQuery
    store = repository_store(settings={'get_setting': '1'}, accounts={'get_accounts': [1]},
                             model={'muted_artists': {'muted'}})
    feed = dict(protos=[], slots=[], genre_options=[{'family': 'jazz', 'label': 'Jazz'}],
                requested_genres=[], epoch=3, vibe='', vibe_request={}, preparing=False)
    board = Mock(spec_set=['load', 'artist_images', 'feed', 'busy'])
    board.load.return_value = {'generation': 'committed'}
    board.feed.return_value = feed
    board.busy.return_value = False
    albums, stories, radio_tracks = Mock(return_value={'albums': []}), Mock(return_value={'cards': []}), Mock()
    visit, growth, revisit, radio = Mock(), Mock(), Mock(), Mock()
    command = HomeQuery(store.settings, store.accounts, store.model, board,
        artists_required=lambda: False, setup_completed=lambda: True, update=lambda: None,
        recap=lambda now: {'month': 'November'}, spotify=lambda **_: {'dismissed': True},
        radio=radio, radio_tracks=radio_tracks, revisit=revisit, access=lambda panel: False,
        earned=lambda: {'directions', 'album-choices', 'recent'}, visit=visit,
        growth=growth, albums=albums, stories=stories,
        preferred=lambda: 'directions', tv_minutes=lambda: 60, actions=lambda now: [],
        sync=lambda now: {}, writable=lambda: True)
    model = command.page(1000)
    assert model['album_board'] is None and model['story_board'] is None
    assert model['spotify_library_job'] is None and model['muted_count'] == 1
    for hidden_query in (radio, radio_tracks, albums, stories, growth, revisit):
        hidden_query.assert_not_called()
    visit.assert_called_once_with('directions')
    board.feed.assert_called_once_with({'generation': 'committed'}, 1000,
        vibes_ready=None, lazy_tuning=True, record=True, path='/', attempt=0, complete_only=True)
    # Rendering has only detached values. Any accidental query after acquisition fails.
    for repository in (store.settings, store.accounts, store.model, board):
        for operation in repository._mock_children.values():
            operation.side_effect = AssertionError('Presenter acquired facts')
    from yt_playlist.web.routes.home_views import card_view
    supplied = card_view(model)
    templates = Path(__file__).parents[2] / 'src/yt_playlist/web/templates'
    environment = Environment(loader=FileSystemLoader(templates))
    html = environment.get_template('_partials/mode_cards.html').render(supplied)
    assert 'data-epoch="3"' in html and 'Jazz' in html and 'home-mode-cards' in html


def test_home_preparation_keeps_complete_board_and_reports_unpublished_batch():
    import json
    from yt_playlist.controllers.home_preparation import HomePreparation
    result = home_cards.CardsResult([{'lane': str(i), 'label': str(i), 'tracks': []} for i in range(4)],
                                   [], None, None, [], [], 0, '')
    publish = Mock(return_value=False)
    existing = Mock(return_value=True)
    calculate = Mock(return_value=result)
    command = HomePreparation(calculate=calculate, freeze=lambda value: value,
        is_complete=lambda menu: len(menu['protos']) == 4, has_complete=existing, encode=json.dumps, publish=publish,
        check=Mock(), elapsed=lambda: 0, deferred=Mock(), max_menus=2, budget=10, max_bytes=100000, schema=1)
    args = dict(cursor=10, adaptive=True, now=1000,
                tuning='tuning', generation='generation', options=[], fingerprint={},
                collections={'choice_context': {}})
    assert command.prepare(**args) is None
    encoded, now = publish.call_args.args
    payload = json.loads(encoded)
    assert len(payload['menus']) == 2 and all(len(menu['protos']) == 4 for menu in payload['menus'])
    assert [call.args for call in calculate.call_args_list] == [(10,), (11,)]
    existing.assert_not_called()
    assert payload['generation'] == 'generation' and now == 1000
    publish.reset_mock()
    result.protos = result.protos[:2]
    assert command.prepare(**args) is None
    publish.assert_not_called()
    existing.assert_called_once_with()
    command.deferred.assert_called_once_with('incomplete_menus')


def test_clicked_pool_plan_includes_generation_policy_before_releasing_and_publishing():
    from yt_playlist.controllers.pool_preparation import LibraryPoolPreparation
    events, rows = [], {}
    def generation():
        rows['generation'] = {'version': 'generation'}
        return 'generation'
    policies = SimpleNamespace(stamp=lambda tracks, lane: tracks, generation_policy=generation,
                               rows=rows, bases={})
    recommendations = Mock(spec_set=['prepare_clicked_pool', 'claim',
                                     'remember_clicked_pool_parameters', 'stage', 'publish'])
    recommendations.prepare_clicked_pool.return_value = 'plan'
    recommendations.claim.return_value = SimpleNamespace(generation_id='generation')
    capture = SimpleNamespace(calculate=lambda now, parameters: [{'key': 'exact'}],
        policies=lambda now: policies, recommendations=recommendations, serial=lambda: 42,
        release=lambda: events.append('release'), publish=lambda work, **kw: work())
    ledger = Mock(spec_set=['record_prepared_policies'])
    ledger.record_prepared_policies.side_effect = lambda *args: events.append('policies')
    recommendations.publish.side_effect = lambda *args: events.append('publish') or True
    command = LibraryPoolPreparation(ledger, recommendations, vibe=lambda now: '',
        scheduled=Mock(), snapshot=lambda: nullcontext(capture))
    parameters = {'lane': 'comfort'}
    assert command.prepare(1000, parameters)
    assert events == ['release', 'policies', 'publish']
    assert recommendations.prepare_clicked_pool.call_args.kwargs['pending_policies'] == [{'version': 'generation'}]
    assert recommendations.prepare_clicked_pool.call_args.kwargs['input_revision'] == 42


@pytest.mark.parametrize('accept', [True, False])
def test_joint_preparation_fits_detached_inputs_and_cleans_staging_on_stale_publication(accept):
    from yt_playlist.controllers.vibe_joint_preparation import JointPreparation
    from yt_playlist.rec import vibe_joint_model as joint
    events = []
    rows = [dict(recording=i, measurement=i, artists=[i], genre='jazz', album=str(i),
                 score=.3 + i / 10, metadata_prior=.5, support=1., source='audio_measurement/test')
            for i in range(1, 5)]
    predictions = Mock(spec_set=['joint_inputs', 'joint_revision', 'stage_joint_model', 'publish_joint_model'])
    predictions.joint_revision.return_value = 'revision'
    predictions.joint_inputs.return_value = rows
    staged = Mock(spec_set=['close'])
    predictions.stage_joint_model.return_value = 'generation', staged
    def fit(captured, vibe, *, memory_bytes):
        assert captured == rows and events == ['capture']
        events.append('fit')
        return joint.fit(captured, vibe, fixed=joint.INITIAL, memory_bytes=memory_bytes)
    def publish(callback):
        events.append('guard')
        if accept:
            callback()
        return accept
    @contextmanager
    def snapshot(vibe, configuration):
        assert (vibe, configuration) == ('relax', 'config')
        events.append('capture')
        try:
            yield SimpleNamespace(predictions=predictions, priors=lambda batch, vibe: batch,
                                  previous={}, head=None, policy='policy', publish=publish)
        finally:
            events.append('released')
    command = JointPreparation(predictions, snapshot=snapshot, next_fit=lambda _waiting: 'relax',
        configuration=lambda: 'config', fit=fit, now=lambda: 1000, monotonic=lambda: 1, max_rows=32)
    work = command.steps(joint.MEMORY_BYTES)
    next(work)
    assert events == ['capture']
    predictions.stage_joint_model.assert_not_called()
    with pytest.raises(StopIteration) as done:
        next(work)
    assert done.value.value is accept and events == ['capture', 'fit', 'guard', 'released']
    predictions.joint_inputs.assert_called_once_with('relax', 32)
    assert predictions.publish_joint_model.call_count == int(accept)
    if accept:
        args = predictions.publish_joint_model.call_args.args
        assert args[:2] == ('relax', 'generation')
        assert args[2]['outcome'] == 'ready' and args[2]['revision'] == 'revision'
        assert args[3] == [1, 2, 3, 4] and args[4] is staged and args[5] is True
        assert predictions.publish_joint_model.call_args.kwargs == {'artists': [4, 3, 2, 1]}
    staged.close.assert_called_once()
    events.clear()
    cancelled = command.steps(joint.MEMORY_BYTES)
    next(cancelled)
    cancelled.close()
    assert events == ['capture', 'released']
    assert predictions.stage_joint_model.call_count == 1
