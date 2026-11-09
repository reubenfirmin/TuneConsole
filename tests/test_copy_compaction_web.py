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
from tests.prepared_fixture_support import historical_template, schema_definitions
from tests.recommendation_support import seed_publication_subjects
from tests.store_fixture_support import (
    _file_consumer_store, bounded_consumer_schema, use_native_sql_calls,
    without_query_timing,
)
from tests.test_deferred_playback_feedback import playing
from yt_playlist.core import db_handles, playback_feedback
from yt_playlist.core.db_rebuild import build, job
from yt_playlist.library import live_plays, player_events
from yt_playlist.web.app import create_app


pytestmark = pytest.mark.usefixtures('unfiltered_copy')


@pytest.mark.parametrize('version', [79, 93, 107])
def test_compaction_schema_input_keeps_installed_history_and_read_only_sql(compaction_inputs, version):
    from yt_playlist.core.migrations import registry, regular

    schema = compaction_inputs.schema(version)
    assert [tuple(row) for row in schema.execute(
        'SELECT version,name FROM schema_migrations ORDER BY version')] == [
        (entry.version, entry.name) for entry in registry.MIGRATIONS[:version]]
    assert regular.validate(schema) == len(regular.CHANGES)
    assert schema.execute('PRAGMA query_only').fetchone()[0] == 1
    assert not schema.execute('PRAGMA foreign_key_check').fetchone()


@pytest.mark.parametrize('version', [79, 93, 107])
def test_compaction_admitted_input_keeps_private_snapshot_guards(compaction_inputs, version, tmp_path):
    template = compaction_inputs(version)
    assert template.source.conn.execute('PRAGMA query_only').fetchone()[0] == 1
    assert template.source._snapshot_revisions.conn is template.source.conn
    assert template.revision_guards
    before = template.source._snapshot_revisions.capture()
    with template.open(tmp_path / 'library.db') as private:
        assert schema_definitions(private.conn) == schema_definitions(template.source.conn)
        private.settings.set_setting('private guard check', 'written')
        assert private._snapshot_revisions.capture()
    assert template.source._snapshot_revisions.capture() == before


@pytest.fixture(scope='module')
def copy_library_input(compaction_inputs, tmp_path_factory):
    completed = compaction_inputs(107)

    def seed(source):
        main = source.accounts.upsert_account('Main', 'bridge', None, True)
        other = source.accounts.upsert_account('Other', 'bridge', 'other', False)
        entry = source.tracks.upsert_track('known', 'Song', 'Artist', None, 180)
        playlist = source.playlists.upsert_playlist(main, 'LM', 'Liked Music', 0, '', 1)
        return main, other, entry, playlist

    database = tmp_path_factory.mktemp('copy-playback-input') / 'library.db'
    with pytest.MonkeyPatch.context() as patch:
        bound_catalog(patch)
        with historical_template(completed.source.conn, seed, database=database, completed=completed) as template:
            yield template


def test_copy_playback_input_keeps_accounts_catalog_and_private_guards(copy_library_input):
    source = copy_library_input.source
    main, other, entry, playlist = copy_library_input.result
    assert main != other and entry and playlist
    assert source.conn.execute('PRAGMA query_only').fetchone()[0] == 1
    assert source._snapshot_revisions.conn is source.conn
    assert source.conn.execute('SELECT count(*) FROM main.catalog_entries').fetchone()[0] == 1


@pytest.fixture
def library(tmp_path, copy_library_input, monkeypatch):
    bound_catalog(monkeypatch)
    with copy_library_input.open(tmp_path / 'library.db') as store:
        yield store, *copy_library_input.result


@pytest.fixture(scope='module')
def browsing_inputs(compaction_inputs, tmp_path_factory):
    templates = {}
    with ExitStack() as resources:
        def get(version):
            if version not in templates:
                completed = compaction_inputs(version)
                database = tmp_path_factory.mktemp('copy-browsing-input') / 'library.db'

                def seed(source):
                    account = source.accounts.upsert_account('Main', 'main.json', None, True)
                    source.tracks.upsert_track('known', 'Title', 'Artist', None, 180)
                    return account

                with pytest.MonkeyPatch.context() as patch:
                    bound_catalog(patch)
                    templates[version] = resources.enter_context(historical_template(
                        completed.source.conn, seed, database=database, completed=completed))
            return templates[version]

        yield get


@pytest.mark.parametrize('version', [79, 93, 107])
def test_browsing_input_keeps_private_account_and_known_track(browsing_inputs, version):
    template = browsing_inputs(version)
    assert template.source.conn.execute('PRAGMA query_only').fetchone()[0] == 1
    assert template.source.accounts.get_master_account().id == template.result
    assert template.source.tracks.metadata_for_video('known')['title'] == 'Title'
    assert template.source._snapshot_revisions.conn is template.source.conn


@pytest.fixture
def browsing_input(request, browsing_inputs, tmp_path, monkeypatch):
    bound_catalog(monkeypatch)
    template = browsing_inputs(request.param)
    with template.open(tmp_path / 'library.db') as store:
        yield store, template.result


@pytest.fixture(autouse=True)
def copy_routes(monkeypatch, without_metadata_providers):
    from fastapi.responses import HTMLResponse
    from yt_playlist.web import app as webapp
    from yt_playlist.web.routes import (app_update, audio_preview, home, likes,
                                       playlists, radio_session, sync, track)

    # Retain the real serving, playback, maintenance and feedback routes used
    # across replacement, along with their normal middleware and lifecycle.
    monkeypatch.setattr(webapp, 'build_all', lambda ctx: [module.build(ctx) for module in
                        (app_update, audio_preview, home, likes, playlists, radio_session, sync, track)])
    monkeypatch.setattr(webapp, '_install_idle_shutdown', lambda *args: None)
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
    from yt_playlist.web import app as webapp
    from yt_playlist.web.routes import app_update, home, playlists, radio_session

    monkeypatch.setattr(webapp, 'build_all', lambda ctx: [module.build(ctx) for module in
                        (app_update, home, playlists, radio_session)])


@pytest.fixture
def radio_routes(copy_routes, monkeypatch):
    from yt_playlist.web import app as webapp
    from yt_playlist.web.routes import app_update, radio_session

    monkeypatch.setattr(webapp, 'build_all', lambda ctx: [app_update.build(ctx), radio_session.build(ctx)])


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
            playlist_url = 'https://music.youtube.com/playlist?list=LM'
            response = client.post('/play',json={'url':playlist_url})
            assert response.status_code==200 and response.json()=={'ok':True}
            assert {'type':'navigate','url':playlist_url} in commands
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


def test_copy_browsing_stays_available_while_writers_are_fenced(library, monkeypatch):
    store, main, other, _, _ = library
    app = create_app(store, lambda: {main: FakeClient(), other: FakeClient()}, background_work=False)
    held, release = threading.Event(), threading.Event()
    original = build.build

    def copy(*args, **kwargs):
        held.set()
        assert release.wait(30)
        return original(*args, **kwargs)

    monkeypatch.setattr(build, 'build', copy)
    with TestClient(app, base_url='http://127.0.0.1') as client:
        try:
            assert client.post('/storage-maintenance/compact').status_code == 202
            assert held.wait(20)
            status = client.get('/storage-maintenance/status').json()
            assert status['active'] and status['banner_message'] == (
                'Optimizing app storage; some recommendations are temporarily unavailable.')
            for path in ('/', '/playlists', '/track/known', '/radio/status', '/storage-maintenance/status'):
                assert client.get(path).status_code == 200, path
            assert client.post('/sync').status_code == 503
            with closing(sqlite3.connect(store._db_path, timeout=0)) as writer:
                with pytest.raises(sqlite3.OperationalError, match='locked'):
                    writer.execute('BEGIN IMMEDIATE')
        finally:
            release.set()
            app.state.ctx.storage_maintenance.join(30)
        assert app.state.ctx.storage_maintenance.phase == 'done'


@pytest.mark.parametrize('browsing_input', [107], indirect=True)
@pytest.mark.parametrize('failure', [False, True])
def test_compaction_acceptance_drains_slow_radio_before_returning(browsing_input, radio_routes, monkeypatch, failure):
    _compaction_acceptance(browsing_input, monkeypatch, failure)


def _compaction_acceptance(browsing_input, monkeypatch, failure, *, wait_for_radio=True):
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

    def copy_and_close():
        store, main = browsing_input
        app = observed_app(store, lambda: {main: FakeClient()},
                           now_fn=lambda: 1000, background_work=False)
        ctx = app.state.ctx
        with TestClient(app, base_url='http://127.0.0.1') as client:
            assert client.post('/storage-maintenance/compact').status_code == 202
            ctx.storage_maintenance.join(30)
            assert ctx.storage_maintenance.phase == 'done'
            assert client.get('/radio/status').status_code == 200
            if wait_for_radio:
                entered.result(timeout=5)
            # The failure happens after actual Radio entry. Context exit must
            # drain its managed thread before returning either result.
            ctx.storage_maintenance.join(30)

    with ThreadPoolExecutor(max_workers=1) as executor:
        acceptance = executor.submit(copy_and_close)
        try:
            # Radio is scheduled by the post-copy status request. Its dispatch
            # deadline starts after actual scheduling, not before the copy.
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
    browsing_input[0].settings.set_setting('after app shutdown', 'accepted')
    assert browsing_input[0].settings.get_setting('after app shutdown') == 'accepted'


def test_compaction_drain_probe_reports_original_failure_before_radio():
    acceptance, scheduled = Future(), Future()
    acceptance.set_exception(RuntimeError('Controlled failure before Radio scheduling'))
    with pytest.raises(RuntimeError, match='Controlled failure before Radio scheduling'):
        _acceptance_stage(acceptance, scheduled, 0, 'Radio scheduling after compaction')


@pytest.mark.parametrize('browsing_input', [107], indirect=True)
def test_compaction_drain_probe_rejects_return_without_radio(browsing_input, radio_routes, monkeypatch):
    original_app = create_app

    def app_without_radio(*args, **kwargs):
        app = original_app(*args, **kwargs)
        monkeypatch.setattr(app.state.ctx.radio, 'prepare', lambda: None)
        return app

    monkeypatch.setattr(f'{__name__}.create_app', app_without_radio)
    with pytest.raises(AssertionError, match='Acceptance returned before Radio scheduling'):
        # Run real compaction, status HTTP and shutdown. The omitted producer
        # cannot enter our controlled Radio task; let the probe catch completion
        # rather than waiting for that test-only event inside the workflow.
        _compaction_acceptance(browsing_input, monkeypatch, False, wait_for_radio=False)


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
def test_preconstructed_app_browses_legacy_readers_during_copy(browsing_input, browsing_routes, rollback):
    store, account = browsing_input
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


@pytest.mark.parametrize('browsing_input', [79, 93, 107], indirect=True)
@pytest.mark.parametrize('rollback', [False, True])
def test_preconstructed_app_browses_legacy_readers_and_radio_after_reopen(browsing_input, browsing_routes, rollback):
    store, account = browsing_input
    settings, tracks, playlists = store.settings, store.tracks, store.playlists
    app = create_app(store, lambda: {account: FakeClient()}, background_work=False)
    with TestClient(app, base_url='http://127.0.0.1') as client:
        replace(store, rollback)
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
        # definitions in a new private TEMP namespace. Guard construction is
        # independently exercised by
        # test_compaction_admitted_input_keeps_private_snapshot_guards.
        monkeypatch.setattr(db_revisions, 'Revisions', schema.bind_revisions)
        yield store, main, other


@pytest.fixture
def feedback_library(cutover_library, monkeypatch):
    from yt_playlist.web import app as webapp
    from yt_playlist.web.routes import app_update, audio_preview, bridge, likes

    # The inbox consumes the same completed installed library. Preserve the
    # real HTTP rating, bridge control and player-event entry points while the
    # replacement closes its main handles.
    monkeypatch.setattr(webapp, 'build_all', lambda ctx: [
        module.build(ctx) for module in (app_update, audio_preview, likes)])
    monkeypatch.setattr(webapp, 'build_bridge_route', bridge.build)
    return cutover_library
