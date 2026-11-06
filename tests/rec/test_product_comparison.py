"""Retained comparison assignments, correlated evidence and serving compatibility."""
from concurrent.futures import ThreadPoolExecutor
from tests.enrichment_support import seed_enrichment
from contextlib import ExitStack, contextmanager
from copy import deepcopy
import logging
import threading
from types import SimpleNamespace

import numpy as np
import pytest

from yt_playlist.rec import (choice_evidence as choices, model_selection,
                            model_setup, product_comparison as product)
from yt_playlist.core.activity import Activity
from yt_playlist.rec.rec_worker import RecWorker


def _remove_trial(store):
    if store._model_comparison_state:
        store.conn.execute('DELETE FROM model_comparison_plan')
    else:
        store.conn.execute('DELETE FROM rec_proposals WHERE surface=?', (product.SURFACE,))
    store.conn.commit()


def _trial(store):
    policy = dict(product.POLICY)
    current = {'phase': 'observing', 'attempt': 1, 'seed': 'balanced', 'policy': policy,
        'alpha': .025, 'previous': {'method': 'svd', 'dim': 2},
        'challenger': {'method': 'svd', 'dim': 3}, 'generation': 'frozen-serving',
        'started_at': 0, 'exposure_until': 28 * product.DAY, 'decision_at': 29 * product.DAY,
        'constraints': product.constraints(store)}
    run = {'kind': 'product', 'phase': 'observing', 'ran_at': 0, 'status': 'running',
        'previous': current['previous'], 'selected': current['previous'], 'challenger': current['challenger'], 'grid': []}
    current['run_id'] = store.model_selection.start_selection(0, run)
    store.rec_proposals.put_proposals(product.SURFACE, current, 0)
    return current


@pytest.fixture
def trial(store):
    return _trial(store)


@pytest.fixture(scope='module')
def paired_album_template(_pristine_current_schema):
    from tests.prepared_fixture_support import prepared_template
    from tests.test_growth_journey import early_journey

    # These cases consume readiness after real First sounds and Your music.
    with prepared_template(_pristine_current_schema, early_journey) as template:
        yield template


@pytest.fixture
def paired_album_store(file_backed_store, paired_album_template):
    paired_album_template.restore(file_backed_store)
    return file_backed_store


def offer(store, current, day, *, format='mixes', episode='episode', outcome=1, suffix='', assignment=None):
    now = day * product.DAY + 100
    if assignment is None:
        assignment = product.assignment(store, format, f'{day}:{suffix}', now)
    oid = choices.issue(store, format, [{'id': 'chosen', 'score': .7}], now,
        context={'comparison': assignment}, episode_id=f'{day}:{episode}',
        opportunity_id=f'{format}:{day}:{episode}:{suffix}')
    choices.respond(store, oid, 'visible', now + 1, visible=['chosen'])
    if outcome == 'close':
        choices.respond(store, oid, 'close', now + 2)
    else:
        choices.respond(store, oid, 'choice', now + 2, option_id='chosen')
        choices.respond(store, oid, 'start', now + 3, option_id='chosen')
        if outcome is not None:
            choices.respond(store, oid, 'experience', now + 4, option_id='chosen', details={
                'kind': 'track_exit', 'position': 200 if outcome else 20, 'duration': 200})
    return oid, assignment


def test_assignment_is_shared_by_day_and_held_windows_survive_midnight_and_later_runs(store, trial):
    old = product.assignment(store, 'mixes', 'window', 100)
    assert old['probability'] == .5 and old['controlled']
    assert all(product.assignment(store, format, format, 100)['arm'] == old['arm'] for format in product.FORMATS)
    assert product.assignment(store, 'mixes', 'window', product.DAY + 10, existing=old) == old
    changed = {**trial, 'run_id': 999, 'seed': 'different'}
    store.rec_proposals.put_proposals(product.SURFACE, changed, product.DAY)
    assert product.assignment(store, 'radio', 'continuing', 2 * product.DAY, existing=old) == old
    assert product.assignment(store, 'mixes', 'new', trial['exposure_until']) is None


def test_comparison_preferences_serialize_with_concurrent_settings_updates(store, file_backed_store, monkeypatch):
    from yt_playlist.repos.model_selection import ModelSelectionRepo
    from yt_playlist.repos.base import snapshot_read

    started, queried = threading.Event(), threading.Event()
    constraints = ModelSelectionRepo.comparison_constraints

    @snapshot_read
    def observe(repo):
        # The read now runs on a detached snapshot. Observe that connection,
        # while retaining the held-serving-lock admission check below.
        repo.conn.set_trace_callback(lambda sql: queried.set()
            if 'FROM settings' in sql else None)
        try:
            return constraints.__wrapped__(repo)
        finally:
            repo.conn.set_trace_callback(None)

    monkeypatch.setattr(ModelSelectionRepo, 'comparison_constraints', observe)

    def read():
        started.set()
        return product.constraints(store)

    try:
        with ThreadPoolExecutor(1) as worker:
            with store._lock:
                pending = worker.submit(read)
                assert started.wait(1)
                # A settings owner can update intent before the comparison's
                # read begins; SQLite must never be entered behind its lock.
                blocked = not queried.wait(.1)
                if blocked:
                    store.settings.set_setting('rec_param:album_collab_weight', '.75')
            result = pending.result(5)
        assert blocked, 'Comparison entered the shared connection while another thread owned it'
        assert queried.is_set()
        assert result == product.constraints(store)
    finally:
        store.conn.set_trace_callback(None)

    # Production readers must stay coherent without waiting on an unrelated
    # uncommitted writer; the writer itself must compose its own pending inputs.
    import hashlib
    from yt_playlist.core import album_feedback_storage
    from yt_playlist.core.evidence_ledger import canonical

    disk = file_backed_store
    before = product.constraints(disk)
    with disk.read_view() as pinned, disk._lock:
        disk.conn.execute('BEGIN IMMEDIATE')
        try:
            disk.settings.publish_values({'rec_param:album_collab_weight': '.25'})
            album_feedback_storage.import_legacy(disk.conn, {'pending-release': {'kind': 'more', 'at': 1}})
            values, standing = disk.model_selection.comparison_constraints()
            expected = hashlib.sha256(canonical([
                values, standing, album_feedback_storage.original(disk.conn)])).hexdigest()
            assert expected != before
            assert product.constraints(disk) == expected
            with ThreadPoolExecutor(1) as worker:
                assert worker.submit(product.constraints, disk).result(.5) == before
            disk.conn.commit()
            assert product.constraints(pinned) == before
        finally:
            disk.conn.rollback()
    assert product.constraints(disk) == expected


def test_unknown_endings_invisible_options_late_outcomes_and_withdrawals_stay_distinct(store, trial):
    known, assigned = offer(store, trial, 0)
    offer(store, trial, 0, episode='unknown', outcome=None)
    offer(store, trial, 0, episode='closed', outcome='close')
    late, _ = offer(store, trial, 0, episode='late', outcome=None)
    choices.respond(store, late, 'experience', 2 * product.DAY, option_id='chosen',
        details={'kind': 'ended', 'position': 200, 'duration': 200})
    hidden = choices.issue(store, 'mixes', [{'id': 'a'}], 120,
        context={'comparison': assigned}, opportunity_id='hidden')
    choices.respond(store, hidden, 'choice', 121, option_id='a')
    report = product.measure(store, trial, 3 * product.DAY)
    row = report['formats']['mixes']['arms'][assigned['arm']]
    assert row['offered'] == 5 and row['visible'] == 4
    assert row['observed'] == 2 and row['unknown'] == 2 and row['qualified'] == 1
    choices.respond(store, known, 'withdraw', 3 * product.DAY + 1,
        details={'action_id': f'{known}:experience:chosen'})
    revised = product.measure(store, trial, 4 * product.DAY)
    assert revised['formats']['mixes']['arms'][assigned['arm']]['observed'] == 1
    assert revised['formats']['mixes']['arms'][assigned['arm']]['unknown'] == 3
    # A later correction does not rewrite an earlier observation cutoff.
    assert product.measure(store, trial, 3 * product.DAY) == report


@pytest.mark.parametrize('format', ['albums', 'mixes'])
def test_interleaved_views_only_attribute_a_close_to_the_requesting_offer(store, trial, format):
    assigned = product.assignment(store, format, 'view', 100)
    held = [choices.present(store, format, [{'id': 'option'}], 100 + index,
        context={'comparison': assigned}, window=index, episode_id=f'view-{index}') for index in range(2)]
    for oid in held:
        choices.respond(store, oid, 'visible', 102, visible=['option'])
    assert store.rec_proposals.get_proposals('choice_window:' + format)['opportunity_id'] == held[1]
    choices.close_window(store, format, 103, opportunity_id=held[0])
    choices.close_window(store, format, 104, opportunity_id=held[0])
    closes = [row for row in store.rec_ledger.evidence().records('response') if row['kind'] == 'close']
    assert [row['opportunity_id'] for row in closes] == [held[0]]
    row = product.measure(store, trial, 3 * product.DAY)['formats'][format]['arms'][assigned['arm']]
    assert (row['visible'], row['observed'], row['unknown'], row['qualified']) == (2, 1, 1, 0)
    assert len(store.rec_ledger.evidence().records('opportunity')) == 2


def favorable(store, trial):
    for day in range(28):
        selected = product.assignment(store, 'mixes', day, day * product.DAY + 100)
        for format in product.FORMATS:
            offer(store, trial, day, format=format, outcome=1 if selected['arm'] == 'challenger' else 'close')
    return product.measure(store, trial, trial['decision_at'])


@pytest.fixture(scope='module')
def favorable_template(_pristine_current_schema):
    from tests.prepared_fixture_support import prepared_template

    def build(source):
        current = _trial(source)
        return current, favorable(source, current)

    with prepared_template(_pristine_current_schema, build) as template:
        yield template


@pytest.fixture
def favorable_evidence(store, favorable_template):
    return favorable_template.restore(store)


@pytest.fixture
def prepared_trial(store, home_prepared_template):
    normal = home_prepared_template.restore(store)
    return _trial(store), normal


@pytest.fixture(scope='module')
def comparison_template(_pristine_current_schema, tmp_path_factory):
    from tests.prepared_fixture_support import prepared_template

    home = tmp_path_factory.mktemp('comparison-template-home')

    @contextmanager
    def template(build):
        with ExitStack() as sources:
            # A module builder can run before per-test isolation, or be
            # requested lazily within it. Limit environment changes to the
            # actual build so neither fixture lifetime can undo the other's.
            with pytest.MonkeyPatch.context() as patch:
                patch.setenv('YT_PLAYLIST_HOME', str(home))
                patch.setenv('YT_PLAYLIST_AUDIO_VIBES', '0')
                patch.setenv('YT_PLAYLIST_AUDIO_VIBE_MODEL', '')
                patch.delenv('DEEPSEEK_API_KEY', raising=False)
                prepared = sources.enter_context(prepared_template(_pristine_current_schema, build))
            yield prepared

    return template


def test_comparison_template_isolates_build_settings_and_restores_the_callers_environment(
        comparison_template, tmp_path, monkeypatch):
    import os

    caller = {'YT_PLAYLIST_HOME': str(tmp_path / 'caller-home'),
              'YT_PLAYLIST_AUDIO_VIBES': '1',
              'YT_PLAYLIST_AUDIO_VIBE_MODEL': 'caller-model',
              'DEEPSEEK_API_KEY': 'fixture-test-key'}
    for key, value in caller.items():
        monkeypatch.setenv(key, value)

    def build(source):
        return {key: os.environ.get(key) for key in caller}

    with comparison_template(build) as template:
        assert template.result['YT_PLAYLIST_HOME'] != caller['YT_PLAYLIST_HOME']
        assert template.result['YT_PLAYLIST_AUDIO_VIBES'] == '0'
        assert template.result['YT_PLAYLIST_AUDIO_VIBE_MODEL'] == ''
        assert template.result['DEEPSEEK_API_KEY'] is None
        assert {key: os.environ.get(key) for key in caller} == caller
    assert {key: os.environ.get(key) for key in caller} == caller


def test_interrupted_admission_without_published_trial_state_recovers_as_failed(store, trial):
    _remove_trial(store)
    model_selection.recover_interrupted(store)
    assert store.model_selection.selection_run(trial['run_id'])['status'] == 'failed'


def test_active_trial_retains_visible_unanswered_offers_past_the_retention_setting(store, trial):
    assigned = product.assignment(store, 'mixes', 'retained', 100)
    oid = choices.issue(store, 'mixes', [{'id': 'option'}], 100,
        context={'comparison': assigned}, opportunity_id='unanswered-trial')
    choices.respond(store, oid, 'visible', 101, visible=['option'])
    store.settings.set_setting('rec_param:choice_retention_d', '1')
    choices.maintain_storage(store, 10 * product.DAY)
    assert store.rec_ledger.evidence().get('opportunity', oid)
    assert product.measure(store, trial, 10 * product.DAY)['formats']['mixes']['arms'][assigned['arm']]['unknown'] == 1


def test_storage_keeps_an_old_active_restore_point_and_radio_assignment(store, trial):
    store.rec_proposals.put_proposals(product.pair_surface(trial['run_id'], 'incumbent', 'radio'),
        {'run_id': trial['run_id'], 'data': {}}, 0)
    store.settings.set_setting('rec_selection_active_run', str(trial['run_id']))
    for index in range(15):
        store.model_selection.start_selection(index + 1, {'status': 'retained', 'ran_at': index + 1})
    store.model_selection.maintain_selection(400 * product.DAY)
    assert store.rec_proposals.get_proposals(product.pair_surface(trial['run_id'], 'incumbent', 'radio'))
    store.settings.set_setting('rec_selection_active_run', '')
    identity = store.accounts.upsert_account('Main', 'main.json', None, True)
    radio = {'identity': identity, 'episode_id': 'held-radio', 'phase': 'playing', 'tracks': [],
             'comparison': {'run_id': trial['run_id']}, 'stopped': False}
    import json
    from yt_playlist.core import radio_storage
    if radio_storage.available(store.conn):
        with store.conn.atomic():
            radio_storage.save(store.conn, radio, expected=None, now=1)
    else:
        store.settings.set_setting('radio_current_session', json.dumps(radio))
    store.model_selection.maintain_selection(400 * product.DAY)
    assert store.rec_proposals.get_proposals(product.pair_surface(trial['run_id'], 'incumbent', 'radio'))
    stopped = {**radio, 'stopped': True, 'phase': 'stopped'}
    if radio_storage.available(store.conn):
        with store.conn.atomic():
            radio_storage.save(store.conn, stopped, expected=radio_storage.load(store.conn)[1], now=2)
    else:
        store.settings.set_setting('radio_current_session', json.dumps(stopped))
    store.model_selection.maintain_selection(400 * product.DAY)
    assert store.rec_proposals.get_proposals(product.pair_surface(trial['run_id'], 'incumbent', 'radio')) is None


def test_health_uses_daily_policy_and_preserves_legacy_history_without_fitting(store, favorable_evidence, monkeypatch):
    from fastapi.testclient import TestClient
    from yt_playlist.web.app import create_app
    trial, report = favorable_evidence
    store.rec_proposals.put_proposals('model_live_report', report, trial['decision_at'])
    historical = store.model_selection.selection_run(trial['run_id'])
    historical.update(status='retained', product_report=report, ended_at=trial['decision_at'],
                      reason='Historical menu comparison retained')
    store.model_selection.save_selection(trial['run_id'], historical)
    monkeypatch.setattr(model_setup, 'build', lambda *a: pytest.fail('health request fitted a model'))
    app = create_app(store, lambda: {}, now_fn=lambda: 1000, background_work=False)
    with TestClient(app, base_url='http://127.0.0.1') as client:
        response = client.get('/taste/autotune-status')
        assert response.status_code == 200
        assert 'Checks run daily when enough fresh listening is available.' in response.text
        assert 'Choices and listening' not in response.text and 'Observed days:' not in response.text
        history = client.get('/taste/model-selection/history')
        assert history.status_code == 200
        assert f'id="selection-run-{trial["run_id"]}"' in history.text
        assert 'Historical menu comparison retained' in history.text
        assert 'Controlled menus and Radio episodes:' in history.text
        assert f'{report["overall"]["days"]["incumbent"]} observed days with the current setup' in history.text
        assert 'Listening outcomes by format' in history.text
        assert 'the latest 30 days' in history.text and 'withheld as one comparison period' in history.text

    # The same genuinely captured history also proves the fixed horizon and
    # missing-coverage guard; it needs no second restored database/file owner.
    assert product.decision(trial, report, trial['decision_at'] - 1)[0] == 'observing'
    assert product.decision(trial, report, trial['decision_at'])[0] == 'applied', report
    report['formats']['albums']['arms']['challenger']['unknown'] = 100
    report['formats']['albums']['arms']['challenger']['mature'] = 120
    assert product.decision(trial, report, trial['decision_at'])[0] == 'retained'


def test_health_pause_resume_controls_daily_selection_without_fitting(store, monkeypatch):
    from fastapi.testclient import TestClient
    from yt_playlist.rec import daily_model_selection as daily
    from yt_playlist.web.app import create_app

    def forbidden(*args, **kwargs):
        pytest.fail('health control request fitted a model')

    monkeypatch.setattr(model_setup, 'build', forbidden)
    monkeypatch.setattr(daily.training, 'build_vectors_from_baskets', forbidden)
    app = create_app(store, lambda: {}, now_fn=lambda: 1000, background_work=False)
    with TestClient(app, base_url='http://127.0.0.1') as client:
        assert daily.due(store, 1000)
        paused = client.post('/taste/model-selection/pause')
        assert paused.status_code == 200 and 'Resume automatic selection' in paused.text
        assert store.settings.get_setting('rec_selection_paused') == '1'
        assert daily.readiness(store, 1000)['state'] == 'paused'
        assert not daily.due(store, 1000)
        resumed = client.post('/taste/model-selection/resume')
        assert resumed.status_code == 200 and 'Pause automatic selection' in resumed.text
        assert store.settings.get_setting('rec_selection_paused') == ''
        assert daily.readiness(store, 1000)['state'] != 'paused'
        assert daily.due(store, 1000)


def test_collaborative_change_affects_actual_album_scores_with_content_available(store):
    from tests import model_support
    from tests.rec.test_album_interests import album
    from yt_playlist.rec import album_choices
    a = album(store, 'a', 'One', 'Techno', 2)
    model_support.build_content_model(store)
    discoveries = []
    for name in ('Third', 'Fourth'):
        tid = store.tracks.upsert_track(name, 'Song', name, name, 200)
        seed_enrichment(store, tid, 'Techno', '2020')
        discoveries.append({'browse_id': name, 'title': name, 'artist': name,
                            'genre': 'Techno', 'year': '2020', 'keys': ['song|' + name.lower()]})
    def rows(swapped):
        return [(key, np.array(value, dtype=np.float32).tobytes()) for key, value in
            [(key, [1., 0.]) for key in a]
            + [('song|third', [0., 1.] if swapped else [1., 0.]),
               ('song|fourth', [1., 0.] if swapped else [0., 1.])]]
    store.rec_model.replace_rec_vectors(rows(False))
    first = album_choices.prepare(store, 100, discoveries)
    store.rec_model.replace_rec_vectors(rows(True))
    second = album_choices.prepare(store, 100, discoveries)
    def scored(result):
        return {row['browse_id']: row['score'] for row in result}
    assert scored(first)['Third'] > scored(first)['Fourth']
    assert scored(second)['Third'] < scored(second)['Fourth']
    assert next(row for row in first if row['browse_id'] == 'Third')['basis'] != 'collaborative'


@pytest.mark.parametrize('held', ['missing', 'stale', 'current'])
def test_album_readiness_does_not_publish_paired_assignments(paired_album_store, held):
    from yt_playlist.rec import album_choices, onboarding
    from yt_playlist.rec.home_cards import _epoch

    store = paired_album_store
    trial = _trial(store)
    for arm in ('incumbent', 'challenger'):
        store.rec_proposals.put_proposals(product.pair_surface(trial['run_id'], arm, 'albums'),
            {'run_id': trial['run_id'], 'data': {'items': [{
                'browse_id': f'{arm}-{index}', 'title': f'Ready record {index}',
                'artist': f'Artist {index}', 'classification': 'discovery', 'score': .9}
                for index in range(2)],
                'context': {}, 'built_at': 1000}}, 1000)
    if held == 'stale':
        store.rec_proposals.put_proposals('model_album_assignment', {'epoch': 'old', 'comparison': {}}, 999)
    elif held == 'current':
        album_choices.load(store, 1000)
        assert store.rec_proposals.get_proposals('model_album_assignment')['epoch'] == _epoch(store, 'album_choices')
    before = store.rec_proposals.get_proposals('model_album_assignment')
    with store.read_view() as reader:
        assert reader.conn.execute('PRAGMA query_only').fetchone()[0] == 1
        assert onboarding.prepared_albums_ready(reader, 1000)
        assert onboarding.arrival_status(reader, 1000)['setup']['albums_ready']
        assert reader.rec_proposals.get_proposals('model_album_assignment') == before
    assert store.rec_proposals.get_proposals('model_album_assignment') == before


def test_paired_mix_and_album_menus_serve_the_assigned_model_and_keep_the_window(store, prepared_trial):
    from tests.rec.test_home_prepared import service
    from yt_playlist.rec import album_choices, home_prepared
    trial, normal = prepared_trial
    selected = product.assignment(store, 'mixes', 'window', 1000)
    for arm, index in (('incumbent', 0), ('challenger', -1)):
        data = {'menus': [deepcopy(normal['menus'][index])], 'choice_context': {'arm_snapshot': arm},
                'tuning': normal['tuning']}
        store.rec_proposals.put_proposals(product.pair_surface(trial['run_id'], arm, 'mixes'),
            {'run_id': trial['run_id'], 'data': data}, 1000)
        store.rec_proposals.put_proposals(product.pair_surface(trial['run_id'], arm, 'albums'),
            {'run_id': trial['run_id'], 'data': {'items': [{'browse_id': arm}],
                'context': {'arm_snapshot': arm}, 'built_at': 1000}}, 1000)
    first = home_prepared.cards(store, normal, 1000, service(store))
    held = store.rec_proposals.get_proposals('home_choice')
    assert held['choice_context']['comparison']['arm'] == selected['arm']
    expected = normal['menus'][0 if selected['arm'] == 'incumbent' else -1]['protos']
    assert {p['experiment_id'] for p in first.protos} == {p['experiment_id'] for p in expected}
    albums = album_choices.load(store, 1000)
    assert albums['items'][0]['browse_id'] == selected['arm']
    # A background refill or midnight does not redraw an existing choice window.
    again = home_prepared.cards(store, normal, product.DAY + 1000, service(store))
    assert [p['experiment_id'] for p in again.protos] == [p['experiment_id'] for p in first.protos]
    assert album_choices.load(store, product.DAY + 1000)['items'] == albums['items']


@pytest.mark.parametrize('rebuild', ['pool', 'live'])
def test_rebuilt_home_menu_captures_new_opportunity_without_old_comparison(store, prepared_trial, monkeypatch, rebuild):
    from starlette.requests import Request
    from yt_playlist.rec import home_prepared
    from yt_playlist.web.routes.home_views import HomeViews

    trial, normal = prepared_trial
    for arm in ('incumbent', 'challenger'):
        store.rec_proposals.put_proposals(product.pair_surface(trial['run_id'], arm, 'mixes'),
            {'run_id': trial['run_id'], 'data': {'menus': deepcopy(normal['menus']),
                'tuning': normal['tuning'], 'choice_context': {'arm_snapshot': arm}}}, 1000)
    worker = RecWorker(SimpleNamespace(store=store, work=Activity(),
        logger=logging.getLogger(__name__), now_fn=lambda: 1001), debounce_s=0)
    monkeypatch.setattr(worker, '_start_loop', lambda: None)
    ctx = SimpleNamespace(store=store, rec_worker=worker, now_fn=lambda: 1001,
        logger=logging.getLogger(__name__), jobs=SimpleNamespace(latest=lambda *args: None),
        templates=SimpleNamespace(TemplateResponse=lambda request, template, context: context))
    views = HomeViews(ctx)
    monkeypatch.setattr(views, 'prepared', lambda *args: normal)
    request = Request({'type': 'http', 'method': 'GET', 'path': '/home/cards',
                       'query_string': b'', 'headers': []})
    try:
        first = views.card_context(request, 1000)
        original = deepcopy(first['choice_context'])
        assert original['comparison']['controlled']
        previous_id = first['protos'][0]['recipe']['opportunity_id']
        replacement = deepcopy(next(p for p in first['protos'] if len(p['tracks']) > 1))
        lane = replacement['lane']
        old_keys = [t['key'] for t in replacement['tracks']]
        # The replacement is controlled; randomized menu selection need not
        # produce another menu with different contents for this exact lane.
        replacement['tracks'] = replacement['tracks'][1:] + replacement['tracks'][:1]
        assert [t['key'] for t in replacement['tracks']] != old_keys
        if rebuild == 'pool':
            monkeypatch.setattr(views.recommendations, 'pool',
                lambda *args: SimpleNamespace(proto=replacement))
            fragment = views.pool_fragment(request, object())
            assert fragment['p'] == replacement
        else:
            explicit = home_prepared.cards(store, normal, 1001, views.recommendations, record=False)
            explicit.protos = [p for p in explicit.protos if p['lane'] != lane] + [replacement]
            explicit.slots = [{'proto': p, 'pool': None} for p in explicit.protos]
            # Live recalculation owns a private-reader HomeCards instance.
            # Control that calculation rather than the route's writer instance.
            monkeypatch.setattr(type(views.recommendations), 'cards', lambda *args: explicit)
            views.card_context(request, 1001, live=True)

        shown = views.card_context(request, 1002)
        assert shown['protos'] and not shown['choice_context'].get('comparison')
        changed = next(p for p in shown['protos'] if p['lane'] == lane)
        assert [t['key'] for t in changed['tracks']] == [t['key'] for t in replacement['tracks']]
        assert all(p['assignment'] is None for p in shown['protos'])
        current_id = changed['recipe']['opportunity_id']
        assert current_id != previous_id
        again = views.card_context(request, 1003)
        assert [p['recipe']['opportunity_id'] for p in again['protos']] == [current_id] * len(shown['protos'])
        assert choices.opportunity(store, previous_id)['comparison'] == original['comparison']
        assert not choices.opportunity(store, current_id).get('comparison')
        # Serving froze metadata only; the existing worker persists both offers.
        assert not store.rec_ledger.evidence().records('opportunity')
        with ThreadPoolExecutor(1) as thread:
            while worker.jobs.measurements:
                thread.submit(worker._flush_measurements).result(5)
        assert not choices.opportunity(store, current_id).get('comparison')
        captured = choices.resolve_context(store, {'artifact': choices.opportunity(store, current_id)['context']})
        assert captured['precision'] == 'theme-raw-genres-v1'
        assert 'arm_snapshot' not in captured and not captured.get('comparison')
        measured = product.measure(store, trial, 1004)
        assert sum(row.get('offered', 0) for row in measured['formats']['mixes']['arms'].values()) == 1
    finally:
        worker.shutdown()


@pytest.fixture(scope='module')
def home_tuning_seed_template(comparison_template):
    from tests.home_prepared_support import seed

    def build(source):
        current = _trial(source)
        seed(source)  # Preserve all eight tracks per lane used by these cases.
        version = source.settings.get_setting('rec_vibe_version')
        assert version and version.endswith('/heuristic')
        return current

    with comparison_template(build) as template:
        yield template


@pytest.fixture(scope='module')
def home_tuning_templates(comparison_template, home_tuning_seed_template):
    from tests.home_fixture_support import returning_listener
    from yt_playlist.rec import home_prepared, home_vibe, mix_tuning

    with ExitStack() as stack:
        templates = {}

        def baseline(state):
            if state not in templates:
                def build(source):
                    current = home_tuning_seed_template.restore(source)
                    if state != 'plain':
                        # Missing today's menus must not erase earned access.
                        returning_listener(source, 'vibes', now=1000)
                    if state == 'energize':
                        with mix_tuning.adjusting(source, 1000):
                            source.settings.set_setting(home_vibe.SETTING, 'energize')
                    original = home_prepared.prepare(source, 1000)
                    assert original is not None
                    return current, original

                templates[state] = stack.enter_context(
                    comparison_template(build))
            return templates[state]

        yield baseline


@pytest.fixture
def home_tuning_trial(store, home_tuning_templates, steering):
    state = ('energize' if steering in ('vibe_change', 'vibe_reset', 'vibe_expire')
             else 'vibes' if steering == 'vibe' else 'plain')
    return home_tuning_templates(state).restore(store)


@pytest.mark.parametrize('steering', ['include', 'avoid', 'vibe', 'vibe_change', 'vibe_reset',
                                    'vibe_expire', 'breadth', 'lean'])
def test_new_home_tuning_preserves_the_tailored_menu_without_comparison_attribution(store, home_tuning_trial, steering):
    from tests.rec.test_home_prepared import service
    from yt_playlist.rec import home_cards, home_prepared, home_vibe, mix_tuning, rec_params
    from yt_playlist.util import genre_map

    trial, original = home_tuning_trial
    now = 1000
    for arm in ('incumbent', 'challenger'):
        store.rec_proposals.put_proposals(product.pair_surface(trial['run_id'], arm, 'mixes'),
            {'run_id': trial['run_id'], 'data': {'menus': deepcopy(original['menus']),
                'tuning': original['tuning'], 'choice_context': {'arm_snapshot': arm}}}, now)
    # Freeze an actual displayed comparison before the user's adjustment.
    first = home_prepared.cards(store, original, now, service(store))
    assert first.choice_context['comparison']['controlled']

    now += mix_tuning.TTL_S if steering == 'vibe_expire' else 1
    family = genre_map.family('Jazz')
    if steering == 'vibe_expire':
        assert mix_tuning.expire(store, now)
    else:
        with mix_tuning.adjusting(store, now):
            if steering in ('include', 'avoid'):
                store.rec_proposals.put_proposals('home_genre_request', {
                    'epoch': home_cards._epoch(store, 'cards'), f'{steering}_genres': [family]}, now)
            elif steering in ('vibe', 'vibe_change'):
                store.settings.set_setting(home_vibe.SETTING, 'relax')
            elif steering == 'vibe_reset':
                mix_tuning.reset(store)
            elif steering == 'breadth':
                rec_params.set_param(store, 'breadth_bias', '.75')
            elif steering == 'lean':
                store.rec_model.set_lean('genre:jazz', 2, now)
    tailored = home_prepared.prepare(store, now)
    assert tailored is not None and tailored['tuning'] != original['tuning']
    shown = home_prepared.cards(store, tailored, now, service(store))
    assert shown.protos and not shown.choice_context.get('comparison')
    assert not store.rec_proposals.get_proposals('home_choice')['choice_context'].get('comparison')
    def contents(protos):
        return [(p['experiment_id'], [t['key'] for t in p['tracks']]) for p in protos]
    assert any(contents(shown.protos) == contents(menu['protos']) for menu in tailored['menus'])
    if steering in ('include', 'avoid'):
        assert all((genre_map.family(t['genre']) == family) == (steering == 'include')
                   for p in shown.protos for t in p['tracks'])
    # A same-tuning refill must also leave the replacement window stable.
    again = home_prepared.cards(store, tailored, now + 1, service(store))
    assert contents(again.protos) == contents(shown.protos)
    assert not again.choice_context.get('comparison')


@pytest.mark.parametrize('stamp', [None, 'obsolete', 'current'])
def test_paired_mix_projection_requires_the_requested_tuning_stamp(store, trial, stamp):
    selected = product.assignment(store, 'mixes', 'window', 1000)
    data = {'menus': [], 'choice_context': {}}
    if stamp is not None:
        data['tuning'] = stamp
    store.rec_proposals.put_proposals(product.pair_surface(trial['run_id'], selected['arm'], 'mixes'),
        {'run_id': trial['run_id'], 'data': data}, 1000)
    paired, assigned = product.prepared(store, 'mixes', 'window', 1000, tuning='current')
    if stamp == 'current':
        assert paired == data and assigned == selected
    else:
        assert paired is None and assigned is None
