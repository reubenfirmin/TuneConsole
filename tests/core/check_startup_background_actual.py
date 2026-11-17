"""Explicit large-library startup check; never collected by pytest.

Supply a managed private library snapshot or a disposable database in /tmp and a JSON report path.
This changes that copy. Real storage, Radio, recommendation and enrichment workers
run; provider clients, enrichment network calls and generated-playlist GC do not.
Uses uvloop, matching the Linux backend and avoiding restricted-runner selector
wakeup limitations in the default asyncio loop.
"""
import asyncio
from collections import Counter
from dataclasses import replace
import faulthandler
import hashlib
from functools import partial
import json
import logging
import os
from pathlib import Path
import sqlite3
import sys
import threading
import time
import traceback
from types import SimpleNamespace
from unittest.mock import patch

import httpx
import uvloop


async def radio_launches(client, ctx, store, bridge, report):
    """Real launch/controller/provider-write path with controlled remote responses."""
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    from tests.conftest import FakeClient, _track
    from yt_playlist.library.sync import sync_identity
    from urllib.parse import parse_qs, urlparse

    class Provider(FakeClient):
        def add_playlist_items(self, playlistId, videoIds, **kwargs):
            for track in store.charts.video_tracks_detail(videoIds):
                self._catalog[track['video_id']] = _track(track['video_id'], track['title'], track['artist'],
                                                        dur=track.get('duration') or 200)
            return super().add_playlist_items(playlistId, videoIds, **kwargs)

        def create_playlist(self, title, description):
            playlist = 'ISOLATED_RADIO_' + str(len(self.created))
            self.created.append((title, description, playlist))
            self._tracks[playlist] = []
            return playlist

    provider, frames = Provider(), []
    account = store.accounts.get_master_account().id
    ctx.client_provider = lambda: {account: provider}

    async def send(frame):
        if frame.get('type') == 'navigate':
            frames.append(frame)
            query = parse_qs(urlparse(frame['url']).query)
            playlist = query['list'][0]
            video = query.get('v', [provider._tracks[playlist][0]['videoId']])[0]
            bridge.now_playing = {'playlist_ytm_id': playlist, 'video_id': video, 'paused': False,
                                  'position': 1, 'duration': 200}
            bridge.now_playing_seen_at = time.monotonic()
        if 'id' in frame:
            if frame.get('type') == 'radio-session':
                ids = [row['videoId'] for row in provider._tracks[frame['playlist']]]
                body = {'ok': True, 'player': bridge.now_playing, 'playback_synced': True,
                        'native': {'available': True, 'videoIds': ids}}
                bridge.resolve(frame['id'], 200, json.dumps(body))
            else:
                bridge.resolve(frame['id'], 503, '{}')

    bridge.connect(send, asyncio.get_running_loop())
    # A genuine sync runs on its own controlled account so no fixture can prune
    # the copied user's library by pretending its remote account is empty.
    sync_account = store.accounts.upsert_account('Isolated sync check', 'isolated.json', None, False)
    sync_client = FakeClient(playlists=[{'playlistId': 'SYNC_CHECK', 'title': 'Sync check', 'count': 2}],
                            tracks={'SYNC_CHECK': [_track('sync-a', 'Sync A', 'Fixture'),
                                                   _track('sync-b', 'Sync B', 'Fixture')]})
    offer = (await client.get('/radio/seeds')).json()
    assert offer['tracks'], offer
    seed = offer['tracks'][0]
    with store.read_view() as reader:
        playlist = reader.conn.execute('SELECT id FROM playlists WHERE account_id=? AND track_count '
                                      'BETWEEN 2 AND 6 ORDER BY id LIMIT 1', (account,)).fetchone()
    assert playlist, 'actual copy needs a short saved playlist'
    report['radio_launches'] = []
    for source in ({'video_id': seed['video_id'], 'menu_id': offer['menu_id']}, {'playlist_id': playlist[0]}):
        sync = asyncio.create_task(asyncio.to_thread(sync_identity, store, sync_account, sync_client, now=time.time()))
        before = time.monotonic()
        pending = asyncio.create_task(client.post('/radio/start', json=source))
        probes = []
        while not pending.done():
            for route in ('/static/favicon.svg', '/bridge/status', '/radio/status'):
                at = time.monotonic()
                response = await asyncio.wait_for(client.get(route), 12)
                probes.append({'path': route, 'seconds': time.monotonic() - at, 'status': response.status_code})
                assert response.status_code == 200
            assert time.monotonic() - before < 60, 'Radio launch exceeded 60s'
            await asyncio.sleep(.1)
        response = pending.result()
        if response.status_code!=200:
            report['radio_launch_failure'] = {'source':source,'status':response.status_code,'body':response.text[:2000],
                'seconds':time.monotonic()-before,'probes':probes}
        assert response.status_code==200, (response.status_code,response.text)
        result = response.json()
        assert result.get('ok'), result
        await asyncio.wait_for(sync, 15)
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            state = (await client.get('/radio/status')).json()
            if state['active'] and state['position'] >= 0:
                break
            await asyncio.sleep(.2)
        assert state['active'] and state['position'] >= 0, {
            key: state.get(key) for key in ('active', 'position', 'error', 'playlist_id')}
        remote = [row['videoId'] for row in provider._tracks[state['playlist_id']]]
        assert [track['video_id'] for track in state['tracks']] == remote
        report['radio_launches'].append({'source': source, 'seconds': time.monotonic() - before,
                                        'playlist': state['playlist_id'], 'queue': remote, 'probes': probes})
        await client.post('/radio/stop')
        bridge.now_playing = {'playlist_ytm_id': 'isolated-ordinary-playlist', 'video_id': 'ordinary', 'paused': False}
    assert len(frames) == 2, frames
    report['radio_navigation_frames'] = frames
    report['controls'] += ' Radio launches use controlled provider create/add/readback and simulated native playback; '
    report['controls'] += 'a real sync runs on an additional isolated account. Automatic sync is suppressed.'


async def check(path, report_path, *, launch_radio=False, launch_mix=None, check_ownership=False):
    from tests.library_image.fixtures import managed_database
    cache = Path(__file__).resolve().parents[2] / '.pytest_cache'
    assert (managed_database(path) == path or
            (path.is_relative_to(Path('/tmp')) or path.is_relative_to(cache)) and path.name.startswith('tuneconsole-'))
    os.environ['YT_PLAYLIST_HOME'] = str(path.with_suffix('.home'))
    os.environ.pop('DEEPSEEK_API_KEY', None)
    from yt_playlist.core.bridge import Bridge
    from yt_playlist.core import fao
    from yt_playlist.core.store import Store
    from yt_playlist.providers import enrichment
    from yt_playlist.rec import transient
    from yt_playlist.web.app import create_app
    from yt_playlist.web.radio_launch import RadioLaunch

    counts = Counter()
    report = {'snapshot': str(path), 'bytes': path.stat().st_size,
              'python': sys.version, 'sqlite': sqlite3.sqlite_version, 'uvloop': uvloop.__version__,
              'status': 'failed', 'requests': [],
              'controls': 'Real storage, Radio, recommendation and enrichment workers; no provider clients; '
                          'enrichment waterfall and artwork network calls suppressed; '
                          'provider availability controlled false; generated-playlist GC deferred.'}
    adopt, configure = RadioLaunch.adopt, enrichment.provider_configuration

    def observed_adopt(*args, **kwargs):
        counts['radio_adoption'] += 1
        return adopt(*args, **kwargs)

    def observed_configuration(*args, **kwargs):
        counts['enrichment_configuration'] += 1
        return configure(*args, **kwargs)

    def save_report():
        report['workers'] = dict(counts)
        report_path.write_text(json.dumps(report, indent=2))

    store = Store(path)
    protected_queries = {
        'paid_descriptions':'SELECT * FROM album_descriptions ORDER BY id',
        'paid_requests':'SELECT * FROM album_description_requests ORDER BY id',
        'saved_recipes':'SELECT * FROM rec_recipes ORDER BY id',
        'road_trip_recipes':'SELECT * FROM road_trip_recipes ORDER BY id',
        'recipe_artists':'SELECT * FROM recipe_artists ORDER BY recipe_id,position',
        'recipe_genres':'SELECT * FROM recipe_genres ORDER BY recipe_id,position',
        'recipe_unresolved_artists':'SELECT * FROM recipe_unresolved_artists ORDER BY recipe_id,position',
        'recipe_unresolved_genres':'SELECT * FROM recipe_unresolved_genres ORDER BY recipe_id,position',
        'user_choices':'SELECT * FROM metadata_choice_heads ORDER BY entry_id,field',
    }
    def fingerprints():
        result = {}
        for name,query in protected_queries.items():
            digest,count = hashlib.sha256(),0
            for row in store.conn.execute(query):
                digest.update(json.dumps(tuple(row),ensure_ascii=False,allow_nan=False).encode())
                digest.update(b'\n')
                count += 1
            result[name] = {'rows':count,'sha256':digest.hexdigest()}
        files = hashlib.sha256()
        for row in store.conn.execute('SELECT filename,content_digest FROM album_descriptions ORDER BY id'):
            raw = fao.read(row['filename']).bytes
            assert hashlib.sha256(raw).hexdigest()==row['content_digest']
            files.update(raw)
        result['paid_file_bytes'] = files.hexdigest()
        return result
    from yt_playlist.core.priority import ServingLock
    acquire,release = ServingLock.acquire,ServingLock.release
    acquired_at,lock_stats = {},{}
    def observed_acquire(lock,*args,**kwargs):
        acquired = acquire(lock,*args,**kwargs)
        if lock is store._lock and acquired and len(lock._local.releases)==1:
            acquired_at[threading.get_ident()] = time.monotonic()
        return acquired
    def observed_release(lock):
        started = acquired_at.pop(threading.get_ident(),None) if lock is store._lock and len(lock._local.releases)==1 else None
        release(lock)
        if started is not None:
            stats = lock_stats.setdefault(threading.current_thread().name,{'count':0,'max_hold_seconds':0})
            stats['count'] += 1
            stats['max_hold_seconds'] = max(stats['max_hold_seconds'],time.monotonic()-started)
    wait_record = store._lock._diagnostics.record
    def observed_wait(wait,yielded,outcome,*,background):
        stats = lock_stats.setdefault('background_wait' if background else 'foreground_wait',{'count':0,'max_wait_seconds':0,'max_cooperative_yield_seconds':0})
        stats['count'] += 1
        stats['max_wait_seconds'] = max(stats['max_wait_seconds'],wait)
        stats['max_cooperative_yield_seconds'] = max(stats['max_cooperative_yield_seconds'],yielded)
        return wait_record(wait,yielded,outcome,background=background)
    ownership_errors = []
    from yt_playlist.core.db_diagnostics import DiagnosticCursor
    call = DiagnosticCursor._call

    def owned_call(cursor, stage, target, *args):
        if cursor.connection is store.conn and not store._lock.held_by_current_thread:
            error = {'thread': threading.current_thread().name, 'stage': stage,
                     'stack': traceback.format_stack(limit=15)}
            if len(ownership_errors) < 20:
                ownership_errors.append(error)
            # Check before entering SQLite; reproducing a connection-mutex/GIL
            # deadlock would prevent even asyncio's timeout from firing.
            raise AssertionError('Serving connection used without its lock')
        return call(cursor, stage, target, *args)
    ownership = patch.object(DiagnosticCursor, '_call', owned_call if check_ownership else call)
    try:
        before = time.monotonic()
        store.init_schema(defer_metadata=True)
        report['admission_seconds'] = time.monotonic() - before
        protected_before = fingerprints()
        report['protected_before'] = protected_before
        history = list(map(tuple, store.conn.execute('SELECT * FROM schema_migrations ORDER BY version')))
        saved = store.conn.execute("SELECT value FROM settings WHERE key='radio_current_session'").fetchone()
        saved = saved[0] if saved else None
        queue = [track['video_id'] for track in json.loads(saved or '{}').get('tracks', [])]
        report['saved_radio_queue_size'] = len(queue)
        bridge = Bridge()
        registry = {name: SimpleNamespace(available=lambda store: False) for name in enrichment._BY_NAME}
        with patch.object(RadioLaunch, 'adopt', observed_adopt), \
             patch.object(enrichment, 'provider_configuration', observed_configuration), \
             patch.object(ServingLock,'acquire',observed_acquire), \
             patch.object(ServingLock,'release',observed_release), \
             patch.object(store._lock._diagnostics,'record',observed_wait), \
             patch('yt_playlist.web.app._background_sync_loop'), \
             patch('yt_playlist.core.updatecheck._raw_version', return_value='0.0.0.dev0'), \
             patch('yt_playlist.enrich.artwork.retry_due', return_value=0):
            app = create_app(store, lambda: {}, bridge=bridge, background_work=True)
            ctx = app.state.ctx
            ctx.rec_worker.gc_initial_s = 3600
            ctx.enrich_worker.waterfall_fn = partial(lambda *args, **kwargs: [], registry=registry)
            ctx.enrich_worker.idle_sleep_s = .2

            async def send(frame):
                if 'id' in frame:
                    bridge.resolve(frame['id'], 503, '{"error":"isolated startup check"}')

            bridge.connect(send, asyncio.get_running_loop())
            bridge.now_playing = {'video_id': 'isolated-current-video', 'title': 'Controlled presence',
                                  'playlist_ytm_id': 'isolated-ordinary-playlist', 'paused': False}
            bridge.now_playing_seen_at = time.monotonic()
            ownership.start()
            async with app.router.lifespan_context(app):
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://localhost') as client:
                    for _ in range(3):
                        for route in ('/static/favicon.svg', '/bridge/status', '/radio/status', '/home/onboard/status', '/'):
                            before = time.monotonic()
                            worker_reads = counts['enrichment_configuration']
                            response = await asyncio.wait_for(client.get(route), 12)
                            report['requests'].append({'path': route, 'status': response.status_code,
                                'seconds': time.monotonic() - before,
                                'enrichment_reads_before': worker_reads,
                                'enrichment_reads_after': counts['enrichment_configuration']})
                            save_report()
                            assert response.status_code == 200, (route, response.status_code)
                            if route == '/radio/status' and queue:
                                assert [track['video_id'] for track in response.json()['tracks']] == queue
                        await asyncio.to_thread(transient.radio_list_ids, store)
                        await asyncio.sleep(.1)
                    if launch_mix is not None:
                        await launch_mix(client, ctx, store, bridge, report)
                        save_report()
                    if launch_radio:
                        with store._lock:
                            assert fingerprints()==protected_before
                            current = store.conn.execute("SELECT value FROM settings WHERE key='radio_current_session'").fetchone()
                            assert (current[0] if current else None)==saved
                        report['protected_before_controlled_radio_exact'] = True
                        async def heartbeat():
                            # The controlled player must keep reporting presence,
                            # as the real extension does while music is playing.
                            while True:
                                if bridge.now_playing is not None:
                                    bridge.now_playing_seen_at = time.monotonic()
                                await asyncio.sleep(.25)

                        presence = asyncio.create_task(heartbeat())
                        try:
                            await radio_launches(client, ctx, store, bridge, report)
                        finally:
                            presence.cancel()
                            await asyncio.gather(presence, return_exceptions=True)
            assert not ownership_errors, ownership_errors
            with store._lock:
                assert list(map(tuple, store.conn.execute('SELECT * FROM schema_migrations ORDER BY version'))) == history
                current = store.conn.execute("SELECT value FROM settings WHERE key='radio_current_session'").fetchone()
                saved_radio_exact = (current[0] if current else None) == saved
                if not launch_radio or store.activity_paused:
                    assert (current[0] if current else None) == saved
                assert fingerprints()==protected_before
                report['protected_sources_and_paid_files_exact'] = True
                report['regular_history'] = list(map(tuple,store.conn.execute('SELECT version,name FROM schema_changes ORDER BY version')))
            assert counts['radio_adoption'] and counts['enrichment_configuration'], counts
            report['historical_version'] = history[-1][0]
            report['history_exact'] = True
            report['saved_radio_exact'] = saved_radio_exact
            report['status'] = 'passed'
    except BaseException as exc:
        report['error'] = repr(exc)
        raise
    finally:
        ownership.stop()
        report['connection_ownership_errors'] = ownership_errors
        report['serving_lock'] = lock_stats
        save_report()
        if 'ctx' in locals() and ctx.radio.session is not None:
            ctx.radio.session.candidates.close()
        store.conn.close()


if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO)
    # Radio's synchronous observer creates another event loop on its worker.
    # Use the same runner there: the restricted harness cannot wake selector
    # loops across threads, even for an already resolved controlled bridge reply.
    asyncio.set_event_loop_policy(uvloop.EventLoopPolicy())
    faulthandler.dump_traceback_later(90, exit=True)
    try:
        uvloop.run(check(Path(sys.argv[1]).resolve(), Path(sys.argv[2]).resolve(),
                        launch_radio='--radio' in sys.argv[3:],
                        check_ownership='--check-ownership' in sys.argv[3:]))
    finally:
        faulthandler.cancel_dump_traceback_later()
