"""Home's worker prepares related shelves while Mix Tapes assemble in the request."""
import logging
from types import SimpleNamespace

import pytest

from tests.rec.test_home_prepared import cached_menu, cached_menu_schema, store
from yt_playlist.rec import album_choices, album_tuning, home_cache, home_prepared
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
def test_worker_leaves_existing_mixes_alone(store, cached_menu, monkeypatch, albums_pending):
    def forbidden(*args):
        pytest.fail('A fresh complete Home board must stop automatic mix preparation')

    tasks = tasks_for(store, monkeypatch, forbidden, albums_pending=albums_pending)
    for _ in range(34):
        assert tasks.home() is albums_pending
    assert home_cache.read(store, home_prepared.SURFACE) == cached_menu


def test_forced_worker_refresh_schedules_shelves_without_a_finished_mix_board(store, monkeypatch):
    def forbidden(*args):
        pytest.fail('A forced Home refresh must not prepare a finished Mix Tape board')

    tasks = tasks_for(store, monkeypatch, forbidden)
    worker = RecWorker(tasks.ctx, enabled=False)
    worker.tasks = tasks
    scheduled = []
    monkeypatch.setattr(worker, '_schedule', lambda **kwargs: scheduled.append(kwargs))
    assert tasks.home() is False
    worker.trigger_home_refresh(force=True)
    assert tasks.home() is False
    assert home_cache.read(store, home_prepared.SURFACE) is None
    assert scheduled == [{'full': False, 'home': True}]


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
