"""Real retained copying through the serving app and durable feedback inbox."""
from contextlib import ExitStack, closing
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
import asyncio
import sqlite3
import threading

import pytest

from fastapi.testclient import TestClient

from tests.conftest import FakeClient
from tests.compaction_fixture_support import unfiltered_copy
from tests.home_fixture_support import without_metadata_providers
from tests.genre_catalog_support import bound_catalog
from tests.recommendation_support import seed_publication_subjects
from tests.store_fixture_support import (
    PristineSchema, _file_consumer_store, bounded_consumer_schema,
    install_numbered_input, use_native_sql_calls, without_query_timing,
)
from tests.test_deferred_playback_feedback import playing
from yt_playlist.core import db_handles, playback_feedback
from yt_playlist.core.db_rebuild import build, job
from yt_playlist.core.store import Store
from yt_playlist.library import live_plays, player_events
from yt_playlist.web.app import create_app


pytestmark = pytest.mark.usefixtures('unfiltered_copy')


@pytest.fixture
def library(browsing_input):
    store, main = browsing_input
    other = store.accounts.upsert_account('Other', 'bridge', 'other', False)
    entry = store.tracks.upsert_track('known', 'Song', 'Artist', None, 180)
    playlist = store.playlists.upsert_playlist(main, 'LM', 'Liked Music', 0, '', 1)
    return store, main, other, entry, playlist


@pytest.fixture
def browsing_input(request, tmp_path, monkeypatch, without_query_timing):
    from yt_playlist.core import db_revisions
    from yt_playlist.core.db_rebuild import installed_format
    from yt_playlist.core.migrations import regular

    version = getattr(request, 'param', 107)
    bound_catalog(monkeypatch)
    with ExitStack() as resources:
        # HTTP readers consume completed admission. Construct the private file
        # and its real guards once, then retain those definitions for reopen.
        with pytest.MonkeyPatch.context() as patch:
            use_native_sql_calls(patch)
            store = Store(tmp_path / 'library.db')
            resources.callback(lambda: store.conn.close())
            store.conn.execute('PRAGMA synchronous=OFF')
            install_numbered_input(store.conn, version, regular_owners=regular.CHANGES)
            store._schema_compatibility = version < 103
            store._rebuild_source_version = None
            store._schema_recovery_pending = False
            store._bind_repositories()
            store.conn.execute('PRAGMA journal_mode=WAL')
            store.conn.execute('PRAGMA wal_autocheckpoint=0')
            store.conn.execute('BEGIN')
            store._snapshot_revisions = db_revisions.Revisions(store.conn)
            schema = resources.enter_context(closing(sqlite3.connect(':memory:', factory=PristineSchema)))
            store.conn.backup(schema)
            schema.capture_revisions(store._snapshot_revisions)
            schema.execute('PRAGMA query_only=ON')
            account = store.accounts.upsert_account('Main', 'main.json', None, True)
            seed_publication_subjects(store, [dict(
                key='title|artist', video_id='known', title='Title', artist='Artist', duration_s=180)])
        assert store.accounts.get_master_account().id == account
        assert store.tracks.metadata_for_video('known')['title'] == 'Title'
        assert store._snapshot_revisions.conn is store.conn
        # The unfiltered candidate has identical installed SQL. Reuse those
        # actual guard definitions on reopen; format admission has repo owners.
        monkeypatch.setattr(db_revisions, 'Revisions', schema.bind_revisions)
        monkeypatch.setattr(installed_format, 'validate', lambda conn: version)
        monkeypatch.setattr(build, '_format', lambda conn: version)
        yield store, account


@pytest.fixture(autouse=True)
def copy_routes(monkeypatch, without_metadata_providers):
    from fastapi.responses import HTMLResponse
    from yt_playlist.web import app as webapp
    from yt_playlist.web.radio_session import PlaylistRadio
    from yt_playlist.web.routes import (app_update, audio_preview, home, likes,
                                       playlists, radio_session, sync, track)

    # Retain the real serving, playback, maintenance and feedback routes used
    # across replacement, along with their normal middleware and lifecycle.
    monkeypatch.setattr(webapp, 'build_all', lambda ctx: [module.build(ctx) for module in
                        (app_update, audio_preview, home, likes, playlists, radio_session, sync, track)])
    monkeypatch.setattr(webapp, '_install_idle_shutdown', lambda *args: None)
    # Status still reads real checkpoints and schedules/drains actual Radio
    # work. Ranking a new seed menu is below the replacement/lifetime subject.
    monkeypatch.setattr(PlaylistRadio, 'seeds', lambda self: {'tracks': [], 'menu_id': None})
    templates = webapp.Jinja2Templates

    def route_templates(*args, **kwargs):
        result = templates(*args, **kwargs)
        # These assertions exercise route/database availability during real
        # replacement. Template rendering has separate page/browser owners.
        monkeypatch.setattr(result, 'TemplateResponse',
                            lambda request, name, context: HTMLResponse(name))
        return result

    monkeypatch.setattr(webapp, 'Jinja2Templates', route_templates)


@pytest.fixture
def browsing_routes(copy_routes, monkeypatch):
    from fastapi import APIRouter
    from yt_playlist.web import app as webapp
    from yt_playlist.web.routes import app_update, home, playlists, radio_session

    # Keep actual Home/page/Radio reads across replacement. Mutating Home
    # controls and extension HTTP routes are outside this browsing workflow.
    for name in ('home_board', 'home_tuning', 'home_discovery', 'home_onboarding',
                 'home_artist_start', 'home_growth', 'home_generation', 'home_tv'):
        monkeypatch.setattr(getattr(home, name), 'build', lambda *args: APIRouter())
    monkeypatch.setattr(webapp, 'build_bridge_route', lambda ctx: APIRouter())
    monkeypatch.setattr(webapp, 'build_all', lambda ctx: [module.build(ctx) for module in
                        (app_update, home, playlists, radio_session)])





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


def replace(store, rollback, progress=lambda phase, detail: None):
    def observed(phase, detail):
        progress(phase, detail)
        if rollback and phase == 'reopening':
            raise RuntimeError('controlled rollback')

    if rollback:
        with pytest.raises(RuntimeError, match='controlled rollback'):
            job.run(store, progress=observed)
    else:
        job.run(store, progress=observed)


@pytest.mark.parametrize('browsing_input', [79, 93, 107], indirect=True)
@pytest.mark.parametrize('rollback', [False, True])
def test_preconstructed_app_browses_legacy_readers_and_radio_after_reopen(browsing_input, browsing_routes, rollback):
    store, account = browsing_input
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

        replace(store, rollback, progress)
        assert browsed_during_copy
        # These are the actual repository objects retained by app services.
        settings.set_setting('retained consumer', 'resumed')
        assert settings.get_setting('retained consumer') == 'resumed'
        tracks.upsert_track('after', 'Resumed', 'Artist', None, 100)
        assert playlists.get_playlists() == []
        for path in ('/', '/playlists', '/radio/status'):
            response = client.get(path)
            assert response.status_code == 200, (path, response.text[:200])


def test_remote_confirmation_and_extension_frames_are_retained_with_main_handles_closed(feedback_library):
    store, main, other = feedback_library
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
def test_new_browse_waits_for_closed_handle_cutover_then_succeeds(cutover_library,monkeypatch,rollback):
    store, main, other = cutover_library
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


@pytest.fixture
def cutover_library(tmp_path, monkeypatch, copy_routes, without_query_timing):
    from fastapi import APIRouter
    from yt_playlist.core import db_revisions
    from yt_playlist.core.db_rebuild import installed_format
    from yt_playlist.core.migrations import current
    from yt_playlist.web import app as webapp
    from yt_playlist.web.routes import app_update, playlists

    # Closed-handle admission consumes an installed library and the real browse
    # router. Bootstrap, ingestion and playback routers have independent owners.
    # Keep actual replacement, rollback, reopen and reader admission. Installed
    # format admission has its independent owner in
    # test_db_rebuild_runtime_retention::test_rebuild_input_destination_sql_keeps_installed_contract.
    entries = (('rock', 'Rock'), ('jazz', 'Jazz'))
    bound_catalog(monkeypatch, entries=entries)
    monkeypatch.setattr(webapp, 'build_all', lambda ctx: [
        module.build(ctx) for module in (app_update, playlists)])
    monkeypatch.setattr(webapp, 'build_bridge_route', lambda ctx: APIRouter())
    admitted_format = lambda conn: current.VERSION
    monkeypatch.setattr(installed_format, 'validate', admitted_format)
    monkeypatch.setattr(build, '_format', admitted_format)
    with ExitStack() as resources:
        # Only dependency preparation uses native SQL. Restore instrumented
        # handle/thread admission before the actual cutover and HTTP request.
        with pytest.MonkeyPatch.context() as patch:
            use_native_sql_calls(patch)
            schema = resources.enter_context(bounded_consumer_schema(entries))
            store = resources.enter_context(_file_consumer_store(tmp_path, schema))
            main = store.accounts.upsert_account('Main', 'bridge', None, True)
            other = store.accounts.upsert_account('Other', 'bridge', 'other', False)
            seed_publication_subjects(store, [dict(
                key='song|artist', video_id='known', title='Song', artist='Artist', duration_s=180)])
            store.playlists.upsert_playlist(main, 'LM', 'Liked Music', 0, '', 1)
            store.settings.set_setting('onboard_setup_complete', '1')
        # Reopen the unchanged installed shape with the same genuine guard
        # definitions in a new private TEMP namespace.
        monkeypatch.setattr(db_revisions, 'Revisions', schema.bind_revisions)
        yield store, main, other


@pytest.fixture
def feedback_library(cutover_library, monkeypatch):
    from yt_playlist.web import app as webapp
    from yt_playlist.web.routes import app_update, bridge, likes

    # The inbox consumes the same completed installed library. Preserve the
    # real HTTP rating, bridge control and player-event entry points while the
    # replacement closes its main handles.
    monkeypatch.setattr(webapp, 'build_all', lambda ctx: [
        module.build(ctx) for module in (app_update, likes)])
    monkeypatch.setattr(webapp, 'build_bridge_route', bridge.build)
    return cutover_library
