"""Real compaction leaves the application and extension usable without recording."""
from contextlib import closing
import errno
import json
import sqlite3
import shutil
import subprocess
import threading
import time

import pytest
from fastapi.testclient import TestClient

from tests.conftest import FakeClient
from yt_playlist.core import account_auth_state, db_handles, db_space, playback_feedback
from yt_playlist.core.db_rebuild import build
from yt_playlist.core.db_rebuild.sqlite_checks import tables
from yt_playlist.core.store import Store
from yt_playlist.core.storage_maintenance import (
    COPY_DEFERRED_MESSAGE, COPY_LOW_SPACE_MESSAGE, MESSAGE, StorageCompacting, StorageMaintenance,
)
from yt_playlist.library import live_plays, player_events
from yt_playlist.rec import transient
from yt_playlist.web import app as web_app
from yt_playlist.web.routes.bridge import EXTENSION_ORIGIN


@pytest.fixture
def current_copy_store(file_backed_store):
    """Current schema and real retained-copy admission, build and installation."""
    return file_backed_store


@pytest.mark.parametrize('synced', [False, True])
def test_ui_and_extension_playback_during_actual_copy(current_copy_store, monkeypatch, synced):
    store = current_copy_store
    path = store._db_path
    iid = store.accounts.upsert_account('Main', 'bridge', None, True)
    pool = [{'videoId': f'radio-{i}', 'title': f'Radio song {i}',
             'artists': [{'name': f'Artist {i}'}], 'duration_seconds': 180,
             'setVideoId': f'set-radio-{i}'} for i in range(20)]
    tids = [store.tracks.upsert_track(t['videoId'], t['title'], t['artists'][0]['name'], '', 180)
            for t in pool]
    source_pid = store.playlists.upsert_playlist(iid, 'PLsource', 'Library', len(tids), '', 1)
    store.playlists.set_playlist_tracks(source_pid, tids)
    store.collection.add_saved_album({'browse': 'MPREalbum', 'title': 'Test album', 'artist': 'Artist'})
    if synced:
        store.settings.set_setting('last_sync_at', '1000')
    store.settings.set_setting('onboard_listening_format', 'radio')
    with store.conn:
        account_auth_state.flag(store.conn, iid, 'Main')
    album = {'title': 'Test album', 'audioPlaylistId': 'PLalbum', 'artists': [{'name': 'Artist'}],
             'tracks': [{'videoId': 'album-song', 'title': 'Album song', 'artists': [{'name': 'Artist'}]}]}
    remote = FakeClient(albums={'MPREalbum': album}, catalog={t['videoId']: t for t in pool})
    app = web_app.create_app(store, lambda: {iid: remote}, background_work=False)
    ctx = app.state.ctx
    monkeypatch.setattr(ctx.radio, 'prepare', lambda: None)
    monkeypatch.setattr(ctx.radio, '_start_monitor', lambda s: None)
    monkeypatch.setattr(ctx.rec_worker, 'start_ticker', lambda: None)
    monkeypatch.setattr(ctx.enrich_worker, 'start_ticker', lambda: None)
    monkeypatch.setattr(ctx.rec_worker, 'trigger_mode_refresh', lambda: None)
    monkeypatch.setattr(web_app, '_background_sync_loop', lambda *a, **kw: None)
    async def confirm_handoff(frame, **kwargs):
        if frame == {'type': 'pause-for-tv'}:
            return 200, '{"ok":true,"paused":true}'
        assert frame['type'] == 'radio-session'
        return 200, json.dumps({'ok': True, 'player': ctx.bridge.now_playing,
                               'playback_synced': True, 'native': {'available': True}})
    monkeypatch.setattr(ctx.bridge, 'request_control', confirm_handoff)
    entered, release = threading.Event(), threading.Event()
    original = build.build

    def hold_copy(source, target, **kwargs):
        # The actual job already owns its writer reservation and file fences.
        entered.set()
        while not release.wait(.01) and not ctx.storage_maintenance._stop.is_set():
            pass
        return original(source, target, **kwargs)
    monkeypatch.setattr(build, 'build', hold_copy)
    try:
        # Match the Linux application's loop, including cross-thread worker
        # wakeups; restricted runners cannot wake the selector socket pair.
        with TestClient(app, base_url='http://127.0.0.1', backend_options={'use_uvloop':True}) as client:
            # Prepare through the live route before explicitly selecting this
            # current copy operation. Ordinary startup performs no such copy.
            assert client.get('/radio/seeds').json()['tracks']
            seed_impressions = store.conn.execute('SELECT count(*) FROM rec_impressions').fetchone()[0]
            # This starter fixture has no prepared album recommendations. Its
            # existing progression guard must remain identical during repair.
            assert client.get('/home/album-choices').status_code == 409
            ctx.storage_maintenance.start_compaction()
            assert entered.wait(10)
            with closing(sqlite3.connect(path, timeout=0)) as other:
                with pytest.raises(sqlite3.OperationalError, match='locked'):
                    other.execute('BEGIN IMMEDIATE')
            start = time.monotonic()
            status = client.get('/storage-maintenance/status').json()
            assert status['active'] and status['message'] == MESSAGE and not status['recording']
            home = client.get('/')
            assert home.status_code == 200
            assert 'Optimizing app storage; some recommendations are temporarily unavailable.' in home.text
            assert '/static/storage-maintenance.css' in home.text
            assert "authBanner([])" in home.text
            for url in ('/playlists', '/cleanup', '/album?browse=MPREalbum',
                        '/album/inline?browse=MPREalbum', '/radio/status', '/radio/seeds',
                        '/album/MPREalbum/share.txt'):
                assert client.get(url).status_code == 200, url
            assert client.get('/home/album-choices').status_code == 409
            assert client.get('/static/storage-maintenance.js').status_code == 200
            assert client.post('/sync').status_code == 503
            assert client.post('/home/tv/takeover').json()['ok']
            assert client.post('/home/tv/playlist/1/event', data={'kind': 'tick'}).json()['ok']
            event = {'kind': 'state', 'state': 'paused', 'videoId': 'playing'}
            assert player_events.handle_player_event(ctx, event, 1000)
            assert playback_feedback.inbox(store).pending() == 1
            # A missed implicit write fails immediately, never holding Store's
                # lock while waiting for the copy's writer reservation.
            with pytest.raises(StorageCompacting):
                store.settings.set_setting('personal-edit', 'must retry')
            assert store.settings.get_setting('personal-edit') is None
            with client.websocket_connect('/bridge/ws', headers={'origin': EXTENSION_ORIGIN}) as ws:
                assert ctx.bridge.connected
                assert client.post('/play', json={'url': 'https://music.youtube.com/watch?v=playing'}).json()['ok']
                assert ws.receive_json()['type'] == 'navigate'
                ws.send_json({'type': 'play', 'videoId': 'playing', 'title': 'Playing', 'artist': 'Artist'})
                deadline = time.monotonic() + 3
                while not ctx.bridge.now_playing and time.monotonic() < deadline:
                    time.sleep(.01)
                assert client.get('/bridge/status').json()['now_playing']['title'] == 'Playing'
                assert client.post('/now-playing/toggle').json()['ok']
                assert ws.receive_json()['type'] == 'playpause'
                assert client.post('/album/play', data={'browse_id': 'MPREalbum'}).status_code == 204
                assert ws.receive_json()['type'] == 'navigate'
                # Exercise real seed ranking, remote playlist creation/readback,
                # session journaling and UI rendering while the job fences writes.
                offered = client.get('/radio/seeds').json()
                assert offered['tracks']
                before = counts(store.conn)
                response = client.post('/radio/start', json={
                    'video_id': offered['tracks'][0]['video_id'], 'menu_id': offered['menu_id']})
                assert response.status_code == 200, response.text
                launched = response.json()
                assert launched['ok'], launched
                assert launched['playlist_id'] == 'PL_NEW_0' and launched['local_id'] is None
                assert ws.receive_json()['type'] == 'navigate'
                assert len(remote._tracks['PL_NEW_0']) == 3
                s = ctx.radio.session
                last = s.tracks[-1]['video_id']
                ctx.bridge.now_playing = {'video_id': last, 'playlist_ytm_id': s.playlist,
                                         'paused': False, 'position': 30, 'duration': 180}
                ctx.bridge.now_playing_seen_at = time.monotonic()
                ctx.radio.listening.observe(s)
                ctx.radio.advance(s)
                assert len(remote._tracks['PL_NEW_0']) == 4
                assert len(s.tracks) == 4 and s.error is None
                table = client.get('/radio/playlist')
                assert table.status_code == 200 and 'radio-history' in table.text
                assert ctx.radio.persistence.load()['playlist'] == 'PL_NEW_0'
                assert store.settings.get_setting('radio_current_session') is None
                assert client.post('/radio/tune', json={'comfort': .3, 'theme': .5, 'energy': .5}).json()['ok']
                ctx.radio.advance(s)
                assert s.error is None
                assert client.post('/radio/seen', json={'menu_id': ctx.radio.session.directions.menu_id}).json()['ok']
                assert client.post('/radio/stop').json()['ok']
                assert counts(store.conn) == before
                assert ctx.auth_expired == {}
                # The healthy bridge clears the live warning without saving
                # anything during repair, and repair stays active throughout.
                assert account_auth_state.read(store.conn) == {iid: 'Main'}
                assert client.get('/storage-maintenance/status').json()['auth_labels'] == []
                assert ctx.storage_maintenance.active and not ctx.storage_maintenance._stop.is_set()
                assert store.conn.execute('SELECT count(*) FROM play_events').fetchone()[0] == 0
                assert store.conn.execute('SELECT count(*) FROM player_events').fetchone()[0] == 0
                assert store.conn.execute('SELECT count(*) FROM rec_impressions').fetchone()[0] == seed_impressions
            assert time.monotonic() - start < 8
            release.set()
            assert ctx.storage_maintenance.ready.wait(10)
            assert ctx.storage_maintenance.phase == 'done'
            assert store.settings.get_setting('radio_current_session') is None
            # Current receipt owners can replay both retained observations as
            # soon as the real compaction worker releases the writer.
            assert playback_feedback.drain(store) == 0
            assert not playback_feedback.inbox(store).pending()
            assert store.conn.execute('SELECT count(*) FROM playback_feedback_receipts').fetchone()[0] == 2
            assert client.get('/storage-maintenance/status').json()['recording']
            assert store.conn.execute('SELECT count(*) FROM play_events').fetchone()[0] == 1
            assert store.conn.execute("SELECT count(*) FROM player_events WHERE kind='state'").fetchone()[0] == 1
            # A new post-repair play saves immediately. Its generated provenance
            # survives even though this radio has no persisted playlist/session.
            track = s.tracks[0]
            assert live_plays.handle_play_event(ctx, {'videoId': track['video_id'],
                'title': track['title'], 'artist': track['artist'], 'playlist': s.playlist}, 1002)
            saved = store.conn.execute('SELECT * FROM play_events WHERE video_id=?',
                                       (track['video_id'],)).fetchall()
            assert len(saved) == 1 and saved[0]['is_generated'] == 1
            assert not any(row['video_id'] == track['video_id']
                           for row in store.history.play_events_since(0, taste_only=True))
            with store.read_snapshot() as snapshot:
                assert s.playlist in transient.radio_list_ids(snapshot.store)
            reopened = Store(path)
            try:
                reopened.init_schema()
                assert not reopened.transient_radio_playlists
                assert s.playlist in transient.radio_list_ids(reopened)
                assert reopened.settings.get_setting('radio_current_session') is None
                later = s.tracks[1]
                assert reopened.history.record_play_event(iid, later['key'], later['video_id'],
                    1004, playlist_ytm_id='RDAMPL' + s.playlist)
                assert reopened.conn.execute('SELECT is_generated FROM play_events ORDER BY id DESC LIMIT 1').fetchone()[0] == 1
            finally:
                reopened.conn.close()
            assert player_events.handle_player_event(ctx, event, 1001)
            assert store.conn.execute("SELECT count(*) FROM player_events WHERE kind='state'").fetchone()[0] == 2
            store.settings.set_setting('personal-edit', 'works again')
            assert store.settings.get_setting('personal-edit') == 'works again'
    finally:
        release.set()


def counts(conn):
    return {table: conn.execute('SELECT count(*) FROM "' + table.replace('"', '""') + '"').fetchone()[0]
            for table in tables(conn) if not table.startswith('sqlite_')}


@pytest.mark.parametrize('failure', ['space_guard', 'sqlite_full', 'os_full', 'interrupted'])
def test_deferred_compaction_warns_without_pausing_recording(current_copy_store, monkeypatch, failure):
    store = current_copy_store
    error = db_space.LowDiskSpace('injected insufficient compaction space')
    if failure == 'os_full':
        error = OSError(errno.ENOSPC, 'injected temporary filesystem full')
    elif failure != 'space_guard':
        error = sqlite3.OperationalError('injected compaction failure')
        error.sqlite_errorcode = sqlite3.SQLITE_FULL if failure == 'sqlite_full' else sqlite3.SQLITE_INTERRUPT

    def failed_compaction(source, target, **kwargs):
        raise error

    monkeypatch.setattr(build, 'build', failed_compaction)
    app = web_app.create_app(store, lambda: {}, background_work=False)
    maintenance = app.state.ctx.storage_maintenance
    expected = COPY_DEFERRED_MESSAGE if failure == 'interrupted' else COPY_LOW_SPACE_MESSAGE
    try:
        with TestClient(app, base_url='http://127.0.0.1', backend_options={'use_uvloop':True}) as client:
            maintenance.start_compaction()
            maintenance.join(10)
            status = client.get('/storage-maintenance/status').json()
            assert not status['active'] and status['recording'] and status['warning']
            assert status['phase'] == 'deferred' and status['message'] == expected
            assert not store.activity_paused
            store.settings.set_setting('after-deferral', 'recorded')
            assert store.settings.get_setting('after-deferral') == 'recorded'
            for _ in range(2):
                response = client.get('/playlists')
                assert response.status_code == 200
                assert status['banner_message'] in response.text
                assert expected not in response.text
    finally:
        maintenance.stop()
        maintenance.join(5)


def test_polling_preserves_short_warning_and_recording_state_across_disconnects():
    node = shutil.which('node')
    if not node:
        pytest.skip('Node.js is required to exercise maintenance polling')
    result = subprocess.run([node, '-e', r"""
const assert = require('node:assert/strict'), fs = require('node:fs'), vm = require('node:vm');
let banner = null, textWrites = 0;
const document = {readyState:'complete', documentElement:{dataset:{}}, getElementById: () => banner};
const events=[];
let refresh, fail=false, state={active:true,auth_labels:[],banner_message:'Optimizing app storage; some recommendations are temporarily unavailable.'};
vm.runInNewContext(fs.readFileSync('src/yt_playlist/web/static/storage-maintenance.js','utf8'), {
  document, window:{setInterval: callback => {refresh=callback;},dispatchEvent:event=>events.push(event)},
  CustomEvent:class {constructor(type,options){this.type=type;this.detail=options.detail;}},
  fetch: async () => {if(fail) throw Error('offline'); return {ok:true,json:async()=>state};}
});
const flush = () => new Promise(resolve => setImmediate(resolve));
(async () => {
  await flush(); assert.equal(document.documentElement.dataset.storageCompacting,'true');
  assert.equal(events.at(-1).type,'auth-checked'); assert.equal(events.at(-1).detail.labels.length,0);
  // Polling also works on standalone pages without the notice element.
  banner = {dataset:{},hidden:true,attributes:{role:'status'},text:'',
    getAttribute(name){return this.attributes[name];},setAttribute(name,value){this.attributes[name]=value;},
    get textContent(){return this.text;},set textContent(value){this.text=value;textWrites++;}};
  await refresh(); assert.equal(banner.textContent,state.banner_message); assert.equal(banner.hidden,false);
  const writes = textWrites; await refresh(); assert.equal(textWrites,writes,'unchanged polls must not reannounce');
  fail=true; await refresh();
  assert.equal(document.documentElement.dataset.storageCompacting,'true','a disconnect must preserve the paused state');
  fail=false; state={active:false,phase:'deferred',recording:true,warning:true,
    auth_labels:['Main'],message:'Detailed diagnostic text.',banner_message:'Free disk space to finish.'}; await refresh();
  assert.equal(document.documentElement.dataset.storageCompacting,'false');
  assert.equal(banner.hidden,true); assert.equal(banner.textContent,'');
  assert.equal(banner.title,undefined,'API diagnostics must not become a warning tooltip');
  assert.equal(banner.attributes.role,'status'); assert.equal(banner.attributes['aria-live'],'polite');
  assert.equal(events.at(-1).detail.labels[0],'Main');
  fail=true; await refresh(); assert.equal(document.documentElement.dataset.storageCompacting,'false');
  assert.equal(banner.textContent,''); assert.equal(banner.hidden,true);
  fail=false; state={active:true,auth_labels:[],banner_message:'Optimizing app storage; some recommendations are temporarily unavailable.'}; await refresh();
  assert.equal(document.documentElement.dataset.storageCompacting,'true');
  assert.equal(banner.attributes.role,'status'); assert.equal(banner.attributes['aria-live'],'polite');
  state={active:false,phase:'done',recording:true,warning:false,auth_labels:[]}; await refresh();
  assert.equal(document.documentElement.dataset.storageCompacting,'false');
  assert.equal(banner.hidden,true); assert.equal(banner.textContent,'');
})();
"""], capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr


def test_shutdown_interrupts_current_copy_and_reenables_recording(current_copy_store, monkeypatch):
    store = current_copy_store
    store.settings.set_setting('before-copy', 'exact original value')
    entered = threading.Event()
    configure = build._configure
    def expensive(conn, cancelled=lambda: False):
        configure(conn, cancelled)
        if not entered.is_set():
            entered.set()
            conn.execute('WITH RECURSIVE work(n) AS (VALUES(0) UNION ALL SELECT n+1 FROM work '
                         'WHERE n<100000000) SELECT sum(n) FROM work').fetchone()
    monkeypatch.setattr(build, '_configure', expensive)
    maintenance = StorageMaintenance(store)
    maintenance.start_compaction()
    try:
        assert entered.wait(10)
        maintenance.stop()
        maintenance.join(5)
        assert maintenance.ready.is_set() and not maintenance.active and not store.activity_paused
        assert store.conn.execute('PRAGMA query_only').fetchone()[0] == 0
        assert store.settings.get_setting('before-copy') == 'exact original value'
        assert db_handles.state(store._db_path)['phase'] == 'failed'
        store.settings.set_setting('after-interruption', 'keep')
        monkeypatch.setattr(build, '_configure', configure)
        resumed = StorageMaintenance(store)
        resumed.start_compaction()
        resumed.join(10)
        assert resumed.phase == 'done'
        assert db_handles.state(store._db_path)['phase'] == 'complete'
        assert store.settings.get_setting('after-interruption') == 'keep'
    finally:
        maintenance.stop()
        maintenance.join(5)
