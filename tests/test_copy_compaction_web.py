"""Real retained copying through the serving app and durable feedback inbox."""
from contextlib import closing
from concurrent.futures import ThreadPoolExecutor
import asyncio
import sqlite3
import threading

import pytest

from fastapi.testclient import TestClient

from tests.conftest import FakeClient
from tests.core.test_predecessor_writes import installed
from tests.test_deferred_playback_feedback import library,playing
from yt_playlist.core import playback_feedback
from yt_playlist.core.db_rebuild import build as retained_copy, job as copy_compaction
from yt_playlist.library import live_plays, player_events
from yt_playlist.web.app import create_app


def test_browsing_playback_feedback_and_resumed_writes_across_replacement(library,monkeypatch):
    store,main,other,_,_ = library
    remote = FakeClient()
    app = create_app(store,lambda:{main:remote,other:FakeClient()},now_fn=lambda:1000,background_work=False)
    ctx = app.state.ctx
    ctx.bridge.now_playing = playing()
    commands = []
    monkeypatch.setattr(ctx.bridge,'send_control',lambda frame,**kwargs:commands.append(frame) or True)
    held,release = threading.Event(),threading.Event()
    original = retained_copy.build
    def copy(*args,**kwargs):
        held.set()
        assert release.wait(30)
        return original(*args,**kwargs)
    monkeypatch.setattr(retained_copy,'build',copy)
    client = TestClient(app,base_url='http://127.0.0.1')
    before = store._db_path.stat().st_ino
    try:
        assert client.post('/storage-maintenance/compact').status_code==202
        assert held.wait(20)
        status = client.get('/storage-maintenance/status').json()
        assert status['active'] and status['banner_message']=='Library updates are paused.'
        for path in ('/','/playlists','/track/known','/radio/status','/storage-maintenance/status'):
            assert client.get(path).status_code==200,path
        playlist_url = 'https://music.youtube.com/playlist?list=LM'
        response = client.post('/play',json={'url':playlist_url})
        assert response.status_code==200 and response.json()=={'ok':True}
        assert {'type':'navigate','url':playlist_url} in commands
        assert client.post('/sync').status_code==503
        with closing(sqlite3.connect(store._db_path,timeout=0)) as writer:
            import pytest
            with pytest.raises(sqlite3.OperationalError,match='locked'):
                writer.execute('BEGIN IMMEDIATE')
        response = client.post('/track/like',data={'video_id':'known','on':'1'})
        assert response.status_code==200
        assert remote.rated==[('known','LIKE')]
        assert playback_feedback.inbox(store).pending()==1
        assert not store.conn.execute('SELECT 1 FROM playback_feedback_receipts').fetchone()
    finally:
        release.set()
        ctx.storage_maintenance.join(30)
    assert ctx.storage_maintenance.phase=='done'
    assert store._db_path.stat().st_ino!=before
    assert not playback_feedback.inbox(store).pending()
    assert store.tracks.item_rating(main,'known')['status']=='LIKE'
    assert store.conn.execute('SELECT count(*) FROM playback_feedback_receipts').fetchone()[0]==1
    assert client.get('/storage-maintenance/status').json()['recording']
    store.settings.set_setting('personal edit after replacement','accepted')
    assert client.get('/track/known').status_code==200
    assert client.get('/radio/status').status_code==200
    assert copy_compaction.db_handles.state(store._db_path)['phase']=='complete'


@pytest.mark.parametrize('installed', [79, 93, 107], indirect=True)
@pytest.mark.parametrize('rollback', [False, True])
def test_preconstructed_app_browses_legacy_readers_and_radio_after_reopen(installed, rollback):
    store, _ = installed
    account = store.accounts.upsert_account('Main', 'main.json', None, True)
    store.tracks.upsert_track('known', 'Title', 'Artist', None, 180)
    settings, tracks, playlists = store.settings, store.tracks, store.playlists
    app = create_app(store, lambda: {account: FakeClient()}, background_work=False)
    with TestClient(app, base_url='http://127.0.0.1') as client:
        browsed_during_copy = False
        def progress(phase, detail):
            nonlocal browsed_during_copy
            if phase == 'copy' and not browsed_during_copy:
                browsed_during_copy = True
                for path in ('/', '/playlists', '/radio/status'):
                    response = client.get(path)
                    assert response.status_code == 200, (path, response.text[:200])
            if rollback and phase == 'reopening':
                raise RuntimeError('controlled rollback')
        if rollback:
            with pytest.raises(RuntimeError, match='controlled rollback'):
                copy_compaction.run(store, progress=progress)
        else:
            copy_compaction.run(store, progress=progress)
        # These are the actual repository objects retained by app services.
        settings.set_setting('retained consumer', 'resumed')
        assert settings.get_setting('retained consumer') == 'resumed'
        tracks.upsert_track('after', 'Resumed', 'Artist', None, 100)
        assert playlists.get_playlists() == []
        for path in ('/', '/playlists', '/radio/status'):
            response = client.get(path)
            assert response.status_code == 200, (path, response.text[:200])


def test_remote_confirmation_and_extension_frames_are_retained_with_main_handles_closed(library):
    store, main, other, _, _ = library
    sent, next_sent, confirmed = threading.Event(), threading.Event(), threading.Event()
    remote = FakeClient()
    original_rate = remote.rate_song
    def rate(video, status):
        sent.set()
        assert confirmed.wait(30)
        return original_rate(video, status)
    remote.rate_song = rate
    app = create_app(store, lambda: {main: remote, other: FakeClient()},
                     now_fn=lambda: 1000, background_work=False)
    app.state.ctx.bridge.now_playing = playing()
    app.state.ctx.bridge.account_bound_next = True
    async def control(frame, timeout=10):
        next_sent.set()
        assert await asyncio.to_thread(confirmed.wait, 30)
        return 200, '{"ok":true}'
    app.state.ctx.bridge.request_control = control
    client = TestClient(app, base_url='http://127.0.0.1')
    with ThreadPoolExecutor(max_workers=2) as executor:
        response = executor.submit(client.post, '/track/like', data={'video_id':'known','on':'1'})
        next_response = executor.submit(client.post, '/now-playing/next', json={'videoId':'known'})
        try:
            assert sent.wait(5)
            assert next_sent.wait(5)
            def progress(phase, detail):
                if phase == 'original_moved':
                    assert store.accounts.get_master_account().id == main
                    assert live_plays.handle_play_event(app.state.ctx, {
                        'videoId':'known','title':'Song','artist':'Artist','brandKnown':True,
                        'brandId':'','volume':100,'paused':False}, 1001)
                    assert player_events.handle_player_event(app.state.ctx, {
                        'kind':'next','videoId':'known','brandKnown':True,'brandId':'',
                        'position':20,'duration':180}, 1002)
                    confirmed.set()
                    result = response.result(timeout=5)
                    assert result.status_code == 200, result.text
                    assert next_response.result(timeout=5).json() == {'ok':True}
                    assert playback_feedback.inbox(store).pending() == 4
            copy_compaction.run(store, progress=progress)
        finally:
            confirmed.set()
        assert response.result(timeout=5).status_code == 200
    assert playback_feedback.drain(store) == 4
    assert not playback_feedback.inbox(store).pending()
    assert store.tracks.item_rating(main,'known')['status'] == 'LIKE'
    assert store.conn.execute('SELECT count(*) FROM playback_feedback_receipts').fetchone()[0] == 4


@pytest.mark.parametrize('rollback',[False,True])
def test_new_browse_waits_for_closed_handle_cutover_then_succeeds(library,monkeypatch,rollback):
    store, main, other, _, _ = library
    with store.read_view() as reader:
        assert reader.accounts.get_master_account().id==main
    app = create_app(store,lambda:{main:FakeClient(),other:FakeClient()},background_work=False)
    client = TestClient(app,base_url='http://127.0.0.1')
    attempted = threading.Event()
    owner = threading.get_ident()
    original_state = copy_compaction.db_handles.state
    def state(path):
        value = original_state(path)
        if threading.get_ident()!=owner and value and value['phase']=='original_moved':
            attempted.set()
        return value
    monkeypatch.setattr(copy_compaction.db_handles,'state',state)
    with ThreadPoolExecutor(max_workers=1) as executor:
        future = None
        def progress(phase,detail):
            nonlocal future
            if phase=='original_moved':
                future = executor.submit(client.get,'/playlists')
                assert attempted.wait(5),'request did not reach private-reader admission'
                if rollback:
                    raise RuntimeError('controlled rollback with waiting browse')
        if rollback:
            with pytest.raises(RuntimeError,match='controlled rollback with waiting browse'):
                copy_compaction.run(store,progress=progress)
        else:
            copy_compaction.run(store,progress=progress)
        response = future.result(timeout=15)
        assert response.status_code==200,response.text[:200]
