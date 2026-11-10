"""A usable Home board ends automatic mix preparation, even during catch-up."""
from copy import deepcopy
import logging
from types import SimpleNamespace

import pytest

from tests.rec.test_home_prepared import cached_menu, cached_menu_schema, store
from yt_playlist.rec import album_choices, album_tuning, home_cache, home_prepared, mix_tuning
from yt_playlist.rec.worker_tasks import ServingTasks
from yt_playlist.rec.rec_worker import RecWorker


def tasks_for(store, monkeypatch, producer, *, now=1001, albums_pending=False):
    # Album selection, reporting and graduation have independent owners. Keep
    # actual Home cache files, validation, controls and admission in these tests.
    monkeypatch.setattr(album_tuning, 'state', lambda *args: {'values': {}, 'genres': []})
    monkeypatch.setattr(album_choices, 'selection', lambda *args, **kwargs: (None, [], None))

    def stage(name, target, *args, **kwargs):
        if name == 'Home collections':
            return producer(*args, **kwargs)
        if name == 'Album choices':
            return None if albums_pending else {'items': []}
        if name == 'Home Discovery':
            return {}
        if name == 'Home revisit':
            return True
        return False

    return ServingTasks(SimpleNamespace(store=store, now_fn=lambda: now,
        client_provider=lambda: {}, logger=logging.getLogger(__name__)), stage)


@pytest.mark.parametrize('albums_pending', [False, True])
def test_existing_four_collections_do_not_repeat_mix_preparation(
        store, cached_menu, monkeypatch, albums_pending):
    def forbidden(*args):
        pytest.fail('A fresh complete Home board must stop automatic mix preparation')

    tasks = tasks_for(store, monkeypatch, forbidden, albums_pending=albums_pending)
    for _ in range(34):
        assert tasks.home() is albums_pending
    assert home_cache.read(store, home_prepared.SURFACE) == cached_menu


def test_first_complete_publication_ends_refill_before_twelve_menus(store, cached_menu, monkeypatch):
    home_cache.put(store, home_prepared.SURFACE, None, 1000)
    prepared = {**cached_menu, 'play_exposure': {}}
    calls = []

    def publish(store, now):
        calls.append(now)
        home_cache.put(store, home_prepared.SURFACE, prepared, now)
        return prepared

    tasks = tasks_for(store, monkeypatch, publish)
    assert tasks.home() is False
    assert tasks.home() is False
    assert calls == [1001]
    assert len(prepared['menus']) == 1


@pytest.mark.parametrize('damage', ['missing', 'corrupt', 'partial', 'empty-tracks', 'duplicate-lanes', 'expired', 'controls'])
def test_insufficient_or_outdated_board_still_requests_preparation(store, cached_menu, monkeypatch, damage):
    changed = deepcopy(cached_menu)
    now = 1001
    if damage == 'missing':
        changed = None
    elif damage == 'corrupt':
        changed['schema'] = home_prepared.SCHEMA + 1
    elif damage == 'partial':
        changed['menus'][0]['protos'].pop()
    elif damage == 'empty-tracks':
        changed['menus'][0]['protos'][0]['tracks'] = []
    elif damage == 'duplicate-lanes':
        changed['menus'][0]['protos'][0]['lane'] = changed['menus'][0]['protos'][1]['lane']
    elif damage == 'expired':
        now = changed['built_at'] + home_prepared.MAX_AGE_S
    else:
        with mix_tuning.adjusting(store, now):
            store.rec_proposals.put_proposals('home_genre_request', {
                'epoch': mix_tuning.card_epoch(store), 'include_genres': ['techno']}, now)
    home_cache.put(store, home_prepared.SURFACE, changed, 1000)
    calls = []
    tasks = tasks_for(store, monkeypatch, lambda *args: calls.append(True), now=now)
    assert tasks.home() is True
    assert calls == [True]


def test_explicit_refresh_survives_deferral_and_a_request_during_publication(store, cached_menu, monkeypatch):
    prepared = {**cached_menu, 'play_exposure': {}}
    calls = []

    def publish(*args):
        calls.append(True)
        if len(calls) == 1:
            return None
        if len(calls) == 2:
            tasks.request_home_refresh()
        return prepared

    tasks = tasks_for(store, monkeypatch, publish)
    tasks.request_home_refresh()
    assert tasks.home() is True  # A deferred explicit request retains its intent.
    tasks.home()                # A new request arrives during this publication.
    tasks.home()                # That newer request still gets its own attempt.
    assert tasks.home() is False
    assert len(calls) == 3


def test_forced_worker_request_reaches_home_admission(store, cached_menu, monkeypatch):
    calls = []
    tasks = tasks_for(store, monkeypatch,
        lambda *args: calls.append(True) or {**cached_menu, 'play_exposure': {}})
    worker = RecWorker(tasks.ctx, enabled=False)
    worker.tasks = tasks
    scheduled = []
    monkeypatch.setattr(worker, '_schedule', lambda **kwargs: scheduled.append(kwargs))
    assert tasks.home() is False
    assert not calls
    worker.trigger_home_refresh(force=True)
    assert tasks.home() is False
    assert calls == [True]
    assert scheduled == [{'full': False, 'home': True}]


def test_reporting_failure_does_not_repeat_a_completed_explicit_build(store, cached_menu, monkeypatch):
    calls = []
    tasks = tasks_for(store, monkeypatch,
        lambda *args: calls.append(True) or {**cached_menu, 'play_exposure': {}})
    stage = tasks.stage

    def reporting_failed(name, *args, **kwargs):
        if name == 'listening choices':
            raise OSError('Report publication failed')
        return stage(name, *args, **kwargs)

    tasks.request_home_refresh()
    tasks.stage = reporting_failed
    with pytest.raises(OSError, match='Report publication failed'):
        tasks.home()
    tasks.stage = stage
    assert tasks.home() is False
    assert calls == [True]


@pytest.mark.parametrize('summary', ['discovery', 'revisit'])
def test_summary_budget_retry_and_new_discovery_leave_ready_mixes_alone(
        store, cached_menu, monkeypatch, summary):
    from yt_playlist.core.activity import WorkBudgetExceeded
    from yt_playlist.rec import discover, surfaces

    store.discovery.upsert_discovered_album('first', 'Artist', 'First album', '2026', None, 1)
    for name in ('rediscover_playlists', 'rediscover_albums', 'rediscover_tracks'):
        monkeypatch.setattr(surfaces, name, lambda *args, **kwargs: [])
    owner, name = ((discover, 'pick_discovered_albums') if summary == 'discovery'
                   else (surfaces, 'rediscover_playlists'))
    original = getattr(owner, name)
    attempts = []

    def exhausted_once(*args, **kwargs):
        attempts.append(True)
        if len(attempts) == 1:
            raise WorkBudgetExceeded('Independent summary exhausted its slice')
        return original(*args, **kwargs)

    monkeypatch.setattr(owner, name, exhausted_once)

    def forbidden(*args):
        pytest.fail('Summary recovery must not rebuild a ready mix batch')

    tasks = tasks_for(store, monkeypatch, forbidden)
    other_stage = tasks.stage

    def stage(name, target, *args, **kwargs):
        if target in (home_prepared.prepare_discovery, home_prepared.prepare_revisit):
            return target(*args, **kwargs)
        return other_stage(name, target, *args, **kwargs)

    tasks.stage = stage
    assert tasks.home() is True
    assert tasks.home() is False
    discovery = store.rec_proposals.get_proposals(home_prepared.DISCOVERY_SURFACE)
    assert [album['browse_id'] for album in discovery['discovery_albums']] == ['first']
    revisit = store.rec_proposals.get_proposals(home_prepared.REVISIT_SURFACE)
    assert all(field in revisit for field in ('rediscover', 'rediscover_albums', 'rediscover_tracks'))
    store.discovery.upsert_discovered_album('second', 'Artist', 'Second album', '2026', None, 1002)
    assert tasks.home() is False
    discovery = store.rec_proposals.get_proposals(home_prepared.DISCOVERY_SURFACE)
    assert {album['browse_id'] for album in discovery['discovery_albums']} == {'first', 'second'}
    assert home_cache.read(store, home_prepared.SURFACE) == cached_menu
