"""Real retained copying through the serving app and durable feedback inbox."""
from contextlib import closing
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
import asyncio
import sqlite3
import threading

import pytest

from fastapi.testclient import TestClient

from tests.conftest import FakeClient
from tests.core.test_predecessor_writes import installed
from tests.test_deferred_playback_feedback import library,playing
from yt_playlist.core import db_handles, playback_feedback
from yt_playlist.core.db_rebuild import build, job
from yt_playlist.library import live_plays, player_events
from yt_playlist.web.app import create_app


def _acceptance_stage(acceptance, stage, timeout, description):
    completed, _ = wait((acceptance, stage), timeout=timeout, return_when=FIRST_COMPLETED)
    if acceptance in completed:
        acceptance.result()  # Preserve an earlier assertion/exception from the real workflow.
        raise AssertionError(f'Acceptance returned before {description}')
    assert stage in completed, f'Acceptance is still running before {description}'
    return stage.result()


def test_browsing_playback_feedback_and_resumed_writes_across_replacement(library,monkeypatch):
    store,main,other,_,_ = library
    remote = FakeClient()
    app = create_app(store,lambda:{main:remote,other:FakeClient()},now_fn=lambda:1000,background_work=False)
    ctx = app.state.ctx
    ctx.bridge.now_playing = playing()
    commands = []
    monkeypatch.setattr(ctx.bridge,'send_control',lambda frame,**kwargs:commands.append(frame) or True)
    held,release = threading.Event(),threading.Event()
    original = build.build
    def copy(*args,**kwargs):
        held.set()
        assert release.wait(30)
        return original(*args,**kwargs)
    monkeypatch.setattr(build,'build',copy)
    with TestClient(app,base_url='http://127.0.0.1') as client:
        before = store._db_path.stat().st_ino
        try:
            assert client.post('/storage-maintenance/compact').status_code==202
            assert held.wait(20)
            status = client.get('/storage-maintenance/status').json()
            assert status['active'] and status['banner_message']==(
                'Optimizing app storage; some recommendations are temporarily unavailable.')
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
        assert db_handles.state(store._db_path)['phase']=='complete'


@pytest.mark.parametrize('failure', [False, True])
def test_compaction_acceptance_drains_slow_radio_before_returning(library, monkeypatch, failure):
    scheduled, entered, draining = Future(), Future(), Future()
    release = threading.Event()
    contexts = []
    original_app = create_app

    def observed_app(*args, **kwargs):
        app = original_app(*args, **kwargs)
        ctx = app.state.ctx
        contexts.append(ctx)
        join_radio, wait_work, start_thread = ctx.radio.join, ctx.work.wait, ctx.work.start_thread

        def observe_schedule(*args, **kwargs):
            thread = start_thread(*args, **kwargs)
            if kwargs.get('name') == 'radio-prepare' and not scheduled.done():
                scheduled.set_result(thread)
            return thread

        def slow_radio():
            if not entered.done():
                entered.set_result(threading.current_thread())
            assert release.wait(30), 'Controlled Radio task was not released'
            return join_radio()

        def wait_for_work(timeout):
            if not draining.done():
                draining.set_result(True)
            return wait_work(timeout)

        monkeypatch.setattr(ctx.radio, 'join', slow_radio)
        monkeypatch.setattr(ctx.work, 'wait', wait_for_work)
        monkeypatch.setattr(ctx.work, 'start_thread', observe_schedule)
        if failure:
            join_maintenance = ctx.storage_maintenance.join
            interrupted = False

            def fail_after_compaction(timeout):
                nonlocal interrupted
                result = join_maintenance(timeout)
                if not interrupted and entered.done():
                    interrupted = True
                    raise RuntimeError('Controlled acceptance failure')
                return result

            monkeypatch.setattr(ctx.storage_maintenance, 'join', fail_after_compaction)
        return app

    monkeypatch.setattr(f'{__name__}.create_app', observed_app)
    with ThreadPoolExecutor(max_workers=1) as executor:
        acceptance = executor.submit(
            test_browsing_playback_feedback_and_resumed_writes_across_replacement, library, monkeypatch)
        try:
            # Radio is scheduled by the post-copy status request. Its dispatch
            # deadline starts after actual scheduling, not before the copy,
            # browsing, feedback replay and resumed-write assertions.
            radio_thread = _acceptance_stage(acceptance, scheduled, 30, 'Radio scheduling after compaction')
            assert _acceptance_stage(acceptance, entered, 5, 'scheduled Radio entry') is radio_thread
            _acceptance_stage(acceptance, draining, 30, 'application shutdown drain')
            assert not acceptance.done(), 'Acceptance returned while Radio still owned the Store'
            assert contexts[0].work.active == ('radio-prepare',)
        finally:
            release.set()
        if failure:
            with pytest.raises(RuntimeError, match='Controlled acceptance failure'):
                acceptance.result(timeout=30)
        else:
            acceptance.result(timeout=30)
    assert not contexts[0].work.active
    # The caller still owns an open, usable database after the app has drained.
    library[0].settings.set_setting('after app shutdown', 'accepted')
    assert library[0].settings.get_setting('after app shutdown') == 'accepted'


def test_compaction_drain_probe_reports_original_failure_before_radio(library, monkeypatch):
    post = TestClient.post

    def fail_before_compaction(client, path, *args, **kwargs):
        if path == '/storage-maintenance/compact':
            raise RuntimeError('Controlled failure before Radio scheduling')
        return post(client, path, *args, **kwargs)

    monkeypatch.setattr(TestClient, 'post', fail_before_compaction)
    with pytest.raises(RuntimeError, match='Controlled failure before Radio scheduling'):
        test_compaction_acceptance_drains_slow_radio_before_returning(library, monkeypatch, False)


def test_compaction_drain_probe_rejects_return_without_radio(library, monkeypatch):
    original_app = create_app

    def app_without_radio(*args, **kwargs):
        app = original_app(*args, **kwargs)
        # Controlled omission must fail the probe even if real compaction and
        # every ordinary acceptance assertion finish successfully.
        monkeypatch.setattr(app.state.ctx.radio, 'prepare', lambda: None)
        return app

    monkeypatch.setattr(f'{__name__}.create_app', app_without_radio)
    with pytest.raises(AssertionError, match='Acceptance returned before Radio scheduling'):
        test_compaction_acceptance_drains_slow_radio_before_returning(library, monkeypatch, False)


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
                job.run(store, progress=progress)
        else:
            job.run(store, progress=progress)
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
    with TestClient(app, base_url='http://127.0.0.1') as client, ThreadPoolExecutor(max_workers=2) as executor:
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
            job.run(store, progress=progress)
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
    attempted = threading.Event()
    owner = threading.get_ident()
    original_state = db_handles.state
    def state(path):
        value = original_state(path)
        if threading.get_ident()!=owner and value and value['phase']=='original_moved':
            attempted.set()
        return value
    monkeypatch.setattr(db_handles,'state',state)
    with TestClient(app,base_url='http://127.0.0.1') as client, ThreadPoolExecutor(max_workers=1) as executor:
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
                job.run(store,progress=progress)
        else:
            job.run(store,progress=progress)
        response = future.result(timeout=15)
        assert response.status_code==200,response.text[:200]
