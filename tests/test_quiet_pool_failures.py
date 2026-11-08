"""Unexpected Fresh worker errors stay internal across actual cards and retries."""
import re

import pytest

from tests.quiet_pool_support import DIAGNOSTIC, quiet_home_template, quiet_pool
from tests.home_fixture_support import home_routes_only, without_metadata_providers
from tests.test_home_other_genres import genre_routes, template_bytecode
from yt_playlist.core.action_errors import ActionInputError
from yt_playlist.core.activity import WorkClosing
from yt_playlist.rec import candidate_refresh, fresh_pool
from yt_playlist.web.jobs import start_fresh_pool


def test_quiet_fresh_input_keeps_playable_library_cards_and_no_discoveries(quiet_home_template):
    store = quiet_home_template.source
    cards = quiet_home_template.result
    assert {card['lane'] for card in cards} == {'comfort', 'wheelhouse', 'explore'}
    tracks = [track for card in cards for track in card['tracks']]
    assert len(tracks) == 3 and all(track['genre'] == 'Jazz' for track in tracks)
    assert len(store.rec_query.tracks_by_keys([track['key'] for track in tracks])) == 3
    assert store.discovery.get_discovered_tracks() == []
    assert store.recommendations.head('fresh') is None


@pytest.mark.parametrize('kind', ['json', 'value'])
@pytest.mark.parametrize('boundary', ['populate', 'prepare'])
def test_fresh_failure_preserves_menu_genres_and_exposes_only_explicit_retry(quiet_pool, kind, boundary, caplog):
    case = quiet_pool
    http, ctx = case.http, case.ctx
    chosen = http.post('/home/mix-genres', data={'genres': ['jazz']})
    assert chosen.status_code == 200
    titles = re.findall(r'data-title="([^"]*)"', chosen.text)
    assert titles and 'data-requested-genres=\'["jazz"]\'' in chosen.text
    case.fault, case.boundary = kind, boundary
    started = http.post('/home/fresh-pool', data={'include_genres': ['jazz']})
    assert started.status_code == 200 and DIAGNOSTIC not in started.text
    assert ctx.work.wait(5)
    parameters = {'include_genres': ['jazz'], 'avoid_genres': []}
    job = ctx.jobs.latest('fresh_pool', parameters)
    assert job.done and job.outcome == 'failed' and DIAGNOSTIC in job.error
    assert job.validation_code is None and DIAGNOSTIC in caplog.text
    refreshed = http.get('/home/cards')
    for response in (http.get(f'/home/fresh-pool/{job.id}'), refreshed):
        assert response.status_code == 200 and DIAGNOSTIC not in response.text
        assert 'Fresh mix not completed. You can try again.' in response.text
        assert 'Not enough matching songs yet' not in response.text
        assert 'hx-trigger="every 2s"' not in response.text
        assert 'aria-label="Find fresh songs"' in response.text
    assert re.findall(r'data-title="([^"]*)"', refreshed.text) == titles
    assert 'data-requested-genres=\'["jazz"]\'' in refreshed.text
    assert case.calls == [parameters] and not case.refreshes
    assert len(case.calculations) == int(boundary == 'prepare')


@pytest.mark.parametrize('kind', ['json', 'value'])
@pytest.mark.parametrize('boundary', ['populate', 'prepare'])
def test_fresh_explicit_retry_rejoins_scheduled_publication_and_recovers(quiet_pool, kind, boundary):
    case = quiet_pool
    http, ctx = case.http, case.ctx
    assert http.post('/home/mix-genres', data={'genres': ['jazz']}).status_code == 200
    parameters = {'include_genres': ['jazz'], 'avoid_genres': []}
    case.fault, case.boundary = kind, boundary
    # The failure-rendering owner above exercises the original HTTP submission.
    # Retry consumes a failed real worker job, without repeating its board/poll
    # rendering merely to establish the same committed precondition.
    job = start_fresh_pool(ctx, parameters)
    assert ctx.work.wait(5)
    assert job.done and job.outcome == 'failed' and DIAGNOSTIC in job.error
    case.fault = None
    assert http.post('/home/fresh-pool', data={'include_genres': ['jazz']}).status_code == 200
    assert ctx.work.wait(5)
    recovered = ctx.jobs.latest('fresh_pool', parameters)
    assert recovered.id != job.id and not recovered.error
    if boundary == 'prepare':
        # The failed calculation consumed this hourly admission. Retry starts
        # discovery once and joins the next real preparation publication.
        assert recovered.outcome == 'waiting' and not recovered.done
        waiting = http.get(f'/home/fresh-pool/{recovered.id}')
        assert 'hx-trigger="every 2s"' in waiting.text and DIAGNOSTIC not in waiting.text
        assert 'Not enough matching songs yet' not in waiting.text
        assert http.post('/home/fresh-pool', data={'include_genres': ['jazz']}).status_code == 200
        assert ctx.work.wait(5)
        assert ctx.jobs.latest('fresh_pool', parameters) is recovered
        assert case.calls == [parameters, parameters]
        assert len(case.calculations) == 1
        assert fresh_pool.prepare(case.store, ctx.now_fn() + candidate_refresh.CHECK_INTERVAL_S) is not None
    result = http.get(f'/home/fresh-pool/{recovered.id}')
    assert recovered.done and not recovered.error
    assert 'Not enough matching songs yet' in result.text and DIAGNOSTIC not in result.text
    assert 'hx-trigger="every 2s"' not in result.text
    assert case.calls == [parameters, parameters] and len(case.refreshes) == 1
    assert len(case.calculations) == int(boundary == 'prepare') + 1


@pytest.mark.parametrize('validation', ['account', 'genres'])
def test_actual_fresh_input_validation_uses_fixed_codes(quiet_pool, monkeypatch, validation):
    case = quiet_pool
    if validation == 'account':
        monkeypatch.setattr(case.ctx, 'client_provider', lambda: {})
    else:
        monkeypatch.setattr(fresh_pool.home_themes, 'genre_options', lambda owner: [])
    with pytest.raises(ActionInputError) as raised:
        fresh_pool.populate(case.ctx)
    assert raised.value.code == ('fresh_account_required' if validation == 'account' else 'fresh_genres_required')
    assert not case.calculations


def test_closed_real_work_admission_finishes_pool_reservation(quiet_pool):
    ctx = quiet_pool.ctx
    parameters = {'include_genres': ['jazz'], 'avoid_genres': []}
    ctx.work.close_admission()
    try:
        with pytest.raises(WorkClosing):
            start_fresh_pool(ctx, parameters)
        failed = ctx.jobs.latest('fresh_pool', parameters)
        assert failed.done and failed.outcome == 'failed' and failed.error
        assert not quiet_pool.calls
    finally:
        ctx.work.open()
    retry = start_fresh_pool(ctx, parameters)
    assert ctx.work.wait(5)
    assert retry.id != failed.id and retry.done and not retry.error
    assert quiet_pool.calls == [parameters]
