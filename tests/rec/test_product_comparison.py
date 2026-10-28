"""Production comparison chronology, correlated evidence and atomic recovery."""
import base64
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import logging
import threading
from types import SimpleNamespace

import numpy as np
import pytest

from yt_playlist.rec import (choice_evidence as choices, comparison_preparation,
                            model_selection, model_setup, product_comparison as product)
from yt_playlist.core.activity import Activity
from yt_playlist.rec.rec_worker import RecWorker


@pytest.fixture
def trial(store):
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


def offer(store, current, day, *, format='mixes', episode='episode', outcome=1, suffix=''):
    now = day * product.DAY + 100
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


def test_comparison_preferences_serialize_with_concurrent_settings_updates(store):
    started, queried = threading.Event(), threading.Event()
    store.conn.set_trace_callback(lambda sql: queried.set() if sql.startswith('SELECT key,value FROM settings') else None)

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


def test_comparison_services_queued_capture_between_individual_menus(store, trial, monkeypatch):
    old = [('recording', np.array([1., 0.], dtype=np.float32).tobytes())]
    new = [('recording', np.array([0., 1., 0.], dtype=np.float32).tobytes())]
    store.rec_model.replace_rec_vectors(old)
    store.settings.set_setting('rec_dim', '2')
    trial['vectors'] = store.rec_ledger.evidence().artifact({arm: [
        (key, base64.b64encode(blob).decode()) for key, blob in rows]
        for arm, rows in (('incumbent', old), ('challenger', new))})
    worker = RecWorker(SimpleNamespace(store=store, work=Activity(),
        logger=logging.getLogger(__name__), now_fn=lambda: product.DAY), debounce_s=0)
    monkeypatch.setattr(worker, '_start_loop', lambda: None)
    entered, release = threading.Event(), threading.Event()
    checkpoints = []

    def checkpoint():
        checkpoints.append(threading.get_ident())
        # Context, ranked pools and the first menu precede this fourth boundary.
        if len(checkpoints) == 4:
            entered.set()
            assert release.wait(5)
        worker._flush_measurements()

    try:
        with ThreadPoolExecutor(1) as threads:
            future = threads.submit(worker._stage, 'comparison audit', comparison_preparation.prepare,
                                    store, trial, product.DAY, checkpoint)
            try:
                assert entered.wait(5), store.model_selection.selection_run(trial['run_id'])
                oid = choices.issue(store, 'albums', [{'id': 'a'}], product.DAY, context={})
                assert choices.respond(store, oid, 'visible', product.DAY + 1, visible=['a'])
                assert not store.rec_ledger.evidence().get('opportunity', oid)
            finally:
                release.set()
            assert future.result(15), store.model_selection.selection_run(trial['run_id'])
        assert checkpoints and all(ident != threading.get_ident() for ident in checkpoints)
        assert choices.opportunity(store, oid)
        assert store.rec_ledger.evidence().get('response', f'{oid}:visible:')
        assert not worker.jobs.measurements
        assert store.rec_model.get_rec_vectors() == old
        from yt_playlist.rec import home_prepared
        from yt_playlist.rec.comparison_storage import decode
        for arm in ('incumbent', 'challenger'):
            paired = store.rec_proposals.get_proposals(product.pair_surface(trial['run_id'], arm, 'mixes'))
            assert decode(paired)['tuning'] == home_prepared.tuning_key(store, product.DAY)
        saved_run = store.model_selection.selection_run(trial['run_id'])
        sizes, timings = saved_run['preparation_bytes'], saved_run['preparation_timings']
        assert len(sizes['projections']) == 6
        assert sizes['total'] == sum(len(row[0].encode()) for row in store.conn.execute(
            'SELECT payload FROM rec_proposals WHERE surface LIKE ?', (f"model_pair:{trial['run_id']}:%",)))
        assert sizes['total'] <= comparison_preparation.MAX_PREPARED_BYTES
        for arm in ('incumbent', 'challenger'):
            assert all(f'{arm}: mix menu {index + 1}' in timings for index in range(12))
            assert all(f'{arm}: {format} storage' in timings for format in product.FORMATS)
    finally:
        release.set()
        worker.shutdown()


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


def test_many_tracks_or_branches_cannot_manufacture_independent_days(store, trial):
    for index in range(40):
        offer(store, trial, 0, suffix=str(index))
    report = product.measure(store, trial, trial['decision_at'])
    assert sum(report['overall']['days'].values()) == 1
    assert sum(len(values) for values in report['overall']['samples'].values()) == 1
    assert product.decision(trial, report, trial['decision_at'])[0] == 'retained'


def favorable(store, trial):
    for day in range(28):
        selected = product.assignment(store, 'mixes', day, day * product.DAY + 100)
        for format in product.FORMATS:
            offer(store, trial, day, format=format, outcome=1 if selected['arm'] == 'challenger' else 'close')
    return product.measure(store, trial, trial['decision_at'])


@pytest.mark.parametrize('format', product.FORMATS)
def test_a_single_listening_format_can_supply_the_complete_comparison(store, trial, format):
    for day in range(28):
        selected = product.assignment(store, format, day, day * product.DAY + 100)
        for episode in range(2):
            offer(store, trial, day, format=format, episode=str(episode),
                  outcome=1 if selected['arm'] == 'challenger' else 'close')
    report = product.measure(store, trial, trial['decision_at'])
    assert product.decision(trial, report, trial['decision_at'])[0] == 'applied', report


def test_missing_promotion_artifact_keeps_the_current_model_and_reports_retention(store, trial):
    old = [('recording', np.array([1., 0.], dtype=np.float32).tobytes())]
    store.rec_model.replace_rec_vectors(old)
    store.settings.set_setting('rec_dim', '2')
    trial['vectors'] = 'missing'
    favorable(store, trial)
    store.rec_proposals.put_proposals(product.SURFACE, trial, 0)
    comparison_preparation.maintain(store, trial['decision_at'])
    assert store.rec_model.get_rec_vectors() == old
    run = store.model_selection.selection_run(trial['run_id'])
    assert run['status'] == 'retained' and 'snapshot is missing' in run['reason']


def test_failed_daily_preparation_keeps_the_trial_and_last_published_pair(store, trial):
    trial['vectors'] = 'missing'
    surface = product.pair_surface(trial['run_id'], 'incumbent', 'radio')
    saved = {'run_id': trial['run_id'], 'data': {'last_good': True}}
    store.rec_proposals.put_proposals(surface, saved, 0)
    assert comparison_preparation.prepare(store, trial, product.DAY) is False
    current = product.state(store)
    assert current['phase'] == 'observing' and current['last_prepare_attempt'] == product.DAY
    assert current['decision_at'] == trial['decision_at']
    assert store.rec_proposals.get_proposals(surface) == saved
    assert store.model_selection.selection_run(trial['run_id'])['status'] == 'running'


def test_pair_quota_failure_retains_complete_previous_pairs_and_records_sizes(store, trial, monkeypatch):
    store.settings.set_setting('rec_dim', '2')
    trial['vectors'] = store.rec_ledger.evidence().artifact({'incumbent': [], 'challenger': []})
    saved = {}
    for arm in ('incumbent', 'challenger'):
        for format in product.FORMATS:
            surface = product.pair_surface(trial['run_id'], arm, format)
            saved[surface] = {'run_id': trial['run_id'], 'data': {'last_good': [arm, format]}}
            store.rec_proposals.put_proposals(surface, saved[surface], 0)

    def complete_arm(*args):
        return {'_policies': [], '_policy_bases': [], **{
            format: {'complete': [f'recording:{i}' for i in range(100)]} for format in product.FORMATS}}

    monkeypatch.setattr(comparison_preparation, '_arm', complete_arm)
    monkeypatch.setattr(comparison_preparation, 'MAX_PREPARED_BYTES', 1)
    assert not comparison_preparation.prepare(store, trial, product.DAY)
    assert all(store.rec_proposals.get_proposals(surface) == value for surface, value in saved.items())
    run = store.model_selection.selection_run(trial['run_id'])
    assert run['status'] == 'running'
    assert 'storage budget' in run['preparation_error']
    assert run['preparation_bytes']['total'] > 1
    assert set(run['preparation_bytes']['projections']) == set(saved)
    assert product.state(store)['decision_at'] == trial['decision_at']


def test_interrupted_admission_without_published_trial_state_recovers_as_failed(store, trial):
    store.conn.execute('DELETE FROM rec_proposals WHERE surface=?', (product.SURFACE,))
    store.conn.commit()
    model_selection.recover_interrupted(store)
    assert store.model_selection.selection_run(trial['run_id'])['status'] == 'failed'


def test_fixed_horizon_can_promote_real_serving_evidence_and_retains_with_missing_coverage(store, trial):
    report = favorable(store, trial)
    assert product.decision(trial, report, trial['decision_at'] - 1)[0] == 'observing'
    assert product.decision(trial, report, trial['decision_at'])[0] == 'applied', report
    report['formats']['albums']['arms']['challenger']['unknown'] = 100
    report['formats']['albums']['arms']['challenger']['mature'] = 120
    assert product.decision(trial, report, trial['decision_at'])[0] == 'retained'


def test_promotion_restart_restore_and_resume_keep_preferences_and_vectors(store, trial):
    old = [('recording', np.array([1., 0.], dtype=np.float32).tobytes())]
    new = [('recording', np.array([0., 1., 0.], dtype=np.float32).tobytes())]
    store.rec_model.replace_rec_vectors(old)
    store.settings.set_setting('rec_dim', '2')
    store.rec_model.set_weight('genre:jazz', 1.4, now=1)
    trial['vectors'] = store.rec_ledger.evidence().artifact({'challenger': [
        (key, base64.b64encode(blob).decode()) for key, blob in new]})
    trial['report'] = favorable(store, trial)
    store.rec_proposals.put_proposals(product.SURFACE, trial, 0)
    model_selection.recover_interrupted(store)
    assert store.model_selection.selection_run(trial['run_id'])['status'] == 'running'
    comparison_preparation.maintain(store, trial['decision_at'])
    applied = store.model_selection.selection_run(trial['run_id'])
    assert applied['status'] == 'applied', applied
    assert store.rec_model.get_rec_vectors() == new
    assert model_selection.current_setup(store) == trial['challenger']
    assert store.model_selection.restore_model_selection(trial['run_id'], trial['decision_at'] + 100)
    assert store.rec_model.get_rec_vectors() == old
    assert store.rec_model.weight_records()['genre:jazz']['weight'] == 1.4
    assert store.settings.get_setting('rec_selection_paused') == '1'
    assert not product.state(store)
    assert product.assignment(store, 'mixes', 'new', trial['decision_at'] + 200) is None
    store.settings.set_setting('rec_selection_paused', '0')
    assert store.model_selection.selection_run(trial['run_id'])['status'] == 'restored'


def test_changed_standing_intent_ends_epoch_without_publishing_and_learning_weights_do_not(store, trial):
    store.rec_model.set_weight('genre:jazz', 1.4, now=1)
    assert product.constraints(store) == trial['constraints']
    store.settings.set_setting('rec_param:album_collab_weight', '.75')
    comparison_preparation.maintain(store, 100)
    assert product.state(store)['phase'] == 'retained'
    assert store.settings.get_setting('rec_selection_active_run') is None


def test_format_regression_is_not_hidden_by_other_formats(store, trial):
    for day in range(28):
        selected = product.assignment(store, 'mixes', day, day * product.DAY + 100)
        for format in product.FORMATS:
            success = (selected['arm'] == 'challenger') != (format == 'radio')
            offer(store, trial, day, format=format, outcome=1 if success else 'close')
    report = product.measure(store, trial, trial['decision_at'])
    assert report['formats']['radio']['difference'] == -1
    assert product.decision(trial, report, trial['decision_at'])[0] == 'retained'


def test_missing_endings_can_reverse_an_apparent_gain_without_becoming_negative_votes(store, trial):
    for day in range(28):
        selected = product.assignment(store, 'mixes', day, day * product.DAY + 100)
        for episode in range(10):
            unknown = episode >= 7
            offer(store, trial, day, episode=str(episode),
                outcome=None if unknown else 1 if selected['arm'] == 'challenger' else 'close')
    report = product.measure(store, trial, trial['decision_at'])
    assert report['overall']['difference'] == 1
    assert report['overall']['missing_outcome_bounds']['difference_bounds'] == pytest.approx([.4, 1])
    assert sum(row['arms'][arm].get('unknown', 0) for row in report['formats'].values()
               for arm in ('incumbent', 'challenger')) == 84
    assert product.decision(trial, report, trial['decision_at'])[0] == 'retained'


def test_active_trial_retains_visible_unanswered_offers_past_the_retention_setting(store, trial):
    assigned = product.assignment(store, 'mixes', 'retained', 100)
    oid = choices.issue(store, 'mixes', [{'id': 'option'}], 100,
        context={'comparison': assigned}, opportunity_id='unanswered-trial')
    choices.respond(store, oid, 'visible', 101, visible=['option'])
    store.settings.set_setting('rec_param:choice_retention_d', '1')
    choices.maintain_storage(store, 10 * product.DAY)
    assert store.rec_ledger.evidence().get('opportunity', oid)
    assert product.measure(store, trial, 10 * product.DAY)['formats']['mixes']['arms'][assigned['arm']]['unknown'] == 1


def test_restore_does_not_reset_the_repeated_comparison_error_budget(store, trial, monkeypatch):
    store.conn.execute('DELETE FROM rec_proposals WHERE surface=?', (product.SURFACE,))
    store.conn.commit()
    store.rec_model.replace_rec_vectors([('recording', np.array([1., 0.], dtype=np.float32).tobytes())])
    monkeypatch.setattr(model_setup, 'MIN_TRACKS', 1)
    monkeypatch.setattr(store.charts, 'history_bounds', lambda: (0, 20 * product.DAY))
    monkeypatch.setattr(comparison_preparation, 'prepare', lambda *a: None)
    comparison_preparation.maintain(store, 21 * product.DAY)
    current = product.state(store)
    assert current['attempt'] == 2
    assert current['alpha'] == pytest.approx(product.POLICY['family_alpha'] / 6)


def test_weekly_monitoring_requires_sustained_observed_decline_and_never_restores_automatically(store, trial):
    plan = {**trial, 'started_at': 40 * product.DAY, 'exposure_until': 68 * product.DAY,
            'next_at': 48 * product.DAY, 'baseline': [1.] * 14}
    store.rec_proposals.put_proposals(product.MONITOR, plan, 40 * product.DAY)
    for day in range(40, 54):
        for index in range(2):
            now = day * product.DAY + 100 + index * 10
            oid = choices.issue(store, 'mixes', [{'id': 'chosen'}], now, context={},
                opportunity_id=f'monitor:{day}:{index}')
            choices.respond(store, oid, 'visible', now + 1, visible=['chosen'])
            choices.respond(store, oid, 'close', now + 2)
        if day == 46:
            comparison_preparation.monitor(store, 48 * product.DAY)
    comparison_preparation.monitor(store, 55 * product.DAY)
    run = store.model_selection.selection_run(trial['run_id'])
    assert run['monitoring']['status'] == 'review'
    assert run['status'] == 'running'  # Monitoring never changes the selected model.
    assert store.settings.get_setting('rec_selection_restore') is None


def test_storage_keeps_an_old_active_restore_point_and_radio_assignment(store, trial):
    store.rec_proposals.put_proposals(product.pair_surface(trial['run_id'], 'incumbent', 'radio'),
        {'run_id': trial['run_id'], 'data': {}}, 0)
    store.settings.set_setting('rec_selection_active_run', str(trial['run_id']))
    for index in range(15):
        store.model_selection.start_selection(index + 1, {'status': 'retained', 'ran_at': index + 1})
    store.model_selection.maintain_selection(400 * product.DAY)
    assert store.rec_proposals.get_proposals(product.pair_surface(trial['run_id'], 'incumbent', 'radio'))
    store.settings.set_setting('rec_selection_active_run', '')
    store.settings.set_setting('radio_current_session', '{"comparison":{"run_id":1},"stopped":false}')
    store.model_selection.maintain_selection(400 * product.DAY)
    assert store.rec_proposals.get_proposals(product.pair_surface(trial['run_id'], 'incumbent', 'radio'))
    store.settings.set_setting('radio_current_session', '{"stopped":true}')
    store.model_selection.maintain_selection(400 * product.DAY)
    assert store.rec_proposals.get_proposals(product.pair_surface(trial['run_id'], 'incumbent', 'radio')) is None


def test_health_uses_daily_policy_and_preserves_legacy_history_without_fitting(store, trial, monkeypatch):
    from fastapi.testclient import TestClient
    from yt_playlist.web.app import create_app
    report = favorable(store, trial)
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
        store.tracks.set_track_enrichment(tid, 'Techno', '2020')
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


def test_paired_mix_and_album_menus_serve_the_assigned_model_and_keep_the_window(store, trial):
    from tests.rec.test_home_prepared import seed, service
    from yt_playlist.rec import album_choices, home_prepared
    seed(store)
    normal = home_prepared.prepare(store, 1000)
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
def test_rebuilt_home_menu_captures_new_opportunity_without_old_comparison(store, trial, monkeypatch, rebuild):
    from tests.rec.test_home_prepared import seed
    from yt_playlist.rec import home_prepared
    from yt_playlist.web.routes.home_views import HomeViews

    seed(store)
    normal = home_prepared.prepare(store, 1000)
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
    request = SimpleNamespace(url=SimpleNamespace(path='/home/cards'), query_params={})
    try:
        first = views.card_context(request, 1000)
        original = deepcopy(first['choice_context'])
        assert original['comparison']['controlled']
        previous_id = first['protos'][0]['recipe']['opportunity_id']
        lane = first['protos'][0]['lane']
        old_keys = [t['key'] for t in first['protos'][0]['tracks']]
        replacement = next(deepcopy(p) for menu in normal['menus'] for p in menu['protos']
            if p['lane'] == lane and [t['key'] for t in p['tracks']] != old_keys)
        if rebuild == 'pool':
            monkeypatch.setattr(views.recommendations, 'pool',
                lambda *args: SimpleNamespace(proto=replacement))
            fragment = views.pool_fragment(request, object())
            assert fragment['p'] == replacement
        else:
            explicit = home_prepared.cards(store, normal, 1001, views.recommendations, record=False)
            explicit.protos = [p for p in explicit.protos if p['lane'] != lane] + [replacement]
            explicit.slots = [{'proto': p, 'pool': None} for p in explicit.protos]
            monkeypatch.setattr(views.recommendations, 'cards', lambda *args: explicit)
            views.card_context(request, 1001, live=True)

        shown = views.card_context(request, 1002)
        assert shown['protos'] and not shown['choice_context'].get('comparison')
        assert shown['choice_context']['arm_snapshot'] == original['arm_snapshot']
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
        assert choices.resolve_context(store, {'artifact': choices.opportunity(store, current_id)['context']})[
            'arm_snapshot'] == original['arm_snapshot']
        measured = product.measure(store, trial, 1004)
        assert sum(row.get('offered', 0) for row in measured['formats']['mixes']['arms'].values()) == 1
    finally:
        worker.shutdown()


@pytest.mark.parametrize('steering', ['include', 'avoid', 'vibe', 'vibe_change', 'vibe_reset',
                                    'vibe_expire', 'breadth', 'lean'])
def test_new_home_tuning_preserves_the_tailored_menu_without_comparison_attribution(store, trial, steering):
    from tests.rec.test_home_prepared import seed, service
    from yt_playlist.rec import home_cards, home_prepared, home_vibe, mix_tuning, rec_params
    from yt_playlist.util import genre_map

    seed(store)
    now = 1000
    if steering in ('vibe_change', 'vibe_reset', 'vibe_expire'):
        with mix_tuning.adjusting(store, now):
            store.settings.set_setting(home_vibe.SETTING, 'energize')
    original = home_prepared.prepare(store, now)
    assert original is not None
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
