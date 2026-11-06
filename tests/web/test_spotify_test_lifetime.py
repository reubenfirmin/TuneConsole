"""Spotify route checks must finish owned work before releasing their Store."""
import json
import threading

import pytest

from tests.conftest import FakeClient, _track
from tests.web.test_spotify_data_route import (
    _managed_clients, _prepare_library, _select_library, _wait_for_library_job,
    spotify_clients,
)
from yt_playlist.web.app import create_app
from yt_playlist.web.spotify_import import save_job


def _blocked_job(store, open_client, phase):
    entered, release = threading.Event(), threading.Event()

    class BlockingClient(FakeClient):
        worker = None

        def block(self):
            self.worker = threading.current_thread()
            entered.set()
            assert release.wait(10), "The test did not release the Spotify provider"

        def search(self, query, filter="songs"):
            if phase == 'preview':
                self.block()
            return super().search(query, filter)

        def create_playlist(self, title, description):
            if phase == 'confirm':
                self.block()
            return super().create_playlist(title, description)

    identity = store.accounts.upsert_account('Main', 'bridge', None, True)
    remote = BlockingClient(search_results=[_track('yt', 'Song', 'Artist')],
                            catalog={'yt': _track('yt', 'Song', 'Artist')})
    web = open_client(create_app(store, lambda: {identity: remote}, now_fn=lambda: 1000,
                                 background_work=False), base_url='http://127.0.0.1:8123')
    if phase == 'preview':
        token, _ = _select_library(web)
        response = web.post('/import/spotify-library/prepare', data={
            'selection_token': token, 'identity_id': identity, 'playlist_index': '0'})
        assert response.status_code == 200
    else:
        token = _prepare_library(web, store, identity, albums=False)
        response = web.post('/import/spotify-library/confirm', data={'preview_token': token},
                            follow_redirects=False)
        assert response.status_code == 303
    return web, remote, entered, release


@pytest.mark.parametrize('phase', ['preview', 'confirm'])
def test_job_wait_is_bounded_and_reads_only_after_slow_worker_finishes(store, spotify_clients, monkeypatch, phase):
    web, remote, entered, release = _blocked_job(store, spotify_clients, phase)
    timer = None
    reads = []
    original = store.settings.get_setting

    def observe_read(*args, **kwargs):
        if threading.current_thread() is threading.main_thread():
            reads.append(web.app.state.ctx.work.active)
        return original(*args, **kwargs)

    try:
        assert entered.wait(3)
        monkeypatch.setattr(store.settings, 'get_setting', observe_read)
        worker_name = 'spotify-library-preview' if phase == 'preview' else 'spotify-library-import'
        with pytest.raises(AssertionError, match=worker_name):
            _wait_for_library_job(web, timeout=.03)
        assert reads == []
        assert remote.worker.is_alive()
        # Actual provider work lasts beyond the old polling deadline.
        timer = threading.Timer(2.1, release.set)
        timer.start()
        job = _wait_for_library_job(web)
        assert job['status'] == ('preview' if phase == 'preview' else 'done')
        assert reads == [()]
        if phase == 'confirm':
            assert job['report']['playlists_imported'] == 1
            assert len(remote.created) == 1
    finally:
        release.set()
        if timer is not None:
            timer.cancel()
            timer.join(3)
        if remote.worker is not None:
            remote.worker.join(3)
    assert not remote.worker.is_alive()


@pytest.mark.parametrize('phase', ['preview', 'confirm'])
def test_failed_body_drains_spotify_work_and_closes_http_before_store(store, phase):
    stop_observer, shutdown_seen = threading.Event(), threading.Event()
    observer = None
    release = None
    try:
        with pytest.raises(AssertionError, match='Controlled route assertion'):
            with _managed_clients() as open_client:
                web, remote, entered, release = _blocked_job(store, open_client, phase)

                def release_during_shutdown():
                    while web.app.state.ctx.work.accepting:
                        if stop_observer.wait(.005):
                            return
                    shutdown_seen.set()
                    release.set()

                observer = threading.Thread(target=release_during_shutdown)
                observer.start()
                assert entered.wait(3)
                raise AssertionError('Controlled route assertion')
    finally:
        stop_observer.set()
        if release is not None:
            release.set()
        if observer is not None:
            observer.join(3)
    assert shutdown_seen.is_set()
    assert not observer.is_alive()
    remote.worker.join(3)
    assert not remote.worker.is_alive()
    assert web.is_closed
    assert not web.app.state.ctx.work.active
    assert not web.app.state.ctx.spotify_import_lock.locked()
    # The real preview/import finished its final SQLite write during unwinding.
    job = json.loads(store.settings.get_setting('spotify_library_import_job'))
    assert job['status'] == ('preview' if phase == 'preview' else 'done')
    if phase == 'confirm':
        assert job['report']['playlists_imported'] == 1
        assert len(store.playlists.get_playlists()) == 1


@pytest.mark.parametrize('status', [None, 'queued', 'running'])
def test_idle_work_tracker_does_not_hide_an_unfinished_or_missing_job(store, spotify_clients, status):
    identity = store.accounts.upsert_account('Main', 'bridge', None, True)
    web = spotify_clients(create_app(store, lambda: {}, background_work=False))
    if status is not None:
        save_job(store, {'status': status, 'account_id': identity, 'started_at': 1000})
    with pytest.raises(AssertionError, match='without a finished job'):
        _wait_for_library_job(web, timeout=.1)


def test_settled_job_read_obeys_remaining_lock_deadline(store, spotify_clients):
    web = spotify_clients(create_app(store, lambda: {}, background_work=False))
    entered, release = threading.Event(), threading.Event()

    def hold_store():
        with store._lock:
            entered.set()
            assert release.wait(3)

    holder = threading.Thread(target=hold_store)
    holder.start()
    try:
        assert entered.wait(1)
        with pytest.raises(AssertionError, match='before the deadline'):
            _wait_for_library_job(web, timeout=.03)
    finally:
        release.set()
        holder.join(3)
    assert not holder.is_alive()
