"""Explicit large-library startup check; never collected by pytest.

Supply a managed private library snapshot or a disposable database in /tmp and a JSON report path.
This changes that copy. Real storage, Radio, recommendation and enrichment workers
run; provider clients, enrichment network calls and generated-playlist GC do not.
Uses uvloop, matching the Linux backend and avoiding restricted-runner selector
wakeup limitations in the default asyncio loop.
"""
import asyncio
from collections import Counter
from contextlib import asynccontextmanager
from contextvars import ContextVar
from dataclasses import replace
import faulthandler
import hashlib
from functools import partial
import json
import logging
import os
from pathlib import Path
import re
import sqlite3
import sys
import threading
import time
import traceback
from types import SimpleNamespace
from unittest.mock import patch

import httpx
import uvloop


async def radio_launches(client, ctx, store, bridge, report, *, tuning_and_vibes=False):
    """Real launch/controller/provider-write path with controlled remote responses."""
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    from tests.conftest import FakeClient, _track
    from yt_playlist.library.sync import sync_identity
    from urllib.parse import parse_qs, urlparse

    class Provider(FakeClient):
        def __init__(self):
            super().__init__()
            self.operations = Counter()

        def add_playlist_items(self, playlistId, videoIds, **kwargs):
            self.operations['add_playlist_items'] += 1
            for track in ctx.radio.session.selected:
                video = track['video_id']
                self._catalog[video] = _track(video, track['title'], track['artist'], dur=track.get('duration') or 200)
            return super().add_playlist_items(playlistId, videoIds, **kwargs)

        def create_playlist(self, title, description, *, video_ids=None):
            self.operations['create_playlist'] += 1
            playlist = 'ISOLATED_RADIO_' + str(len(self.created))
            self.created.append((title, description, playlist))
            self._tracks[playlist] = [_track(t['video_id'], t['title'], t['artist'], dur=t.get('duration') or 200)
                                      for t in ctx.radio.session.selected if t['video_id'] in (video_ids or ())]
            return playlist

        def get_playlist(self, playlistId, limit=100):
            self.operations['get_playlist'] += 1
            return super().get_playlist(playlistId, limit=limit)

        def get_song(self, videoId):
            self.operations['get_song'] += 1
            return super().get_song(videoId)

    provider, frames, navigated = Provider(), [], []
    account = store.accounts.get_master_account().id
    ctx.client_provider = lambda: {account: provider}

    async def send(frame):
        if frame.get('type') == 'navigate':
            session = ctx.radio.session
            if session.seeded_launch or session.initial_queue_maximum is not None:
                assert not session.candidates.model_ready, 'The candidate model was loaded before playback'
                assert not any(session.directions.slots), 'Prepared streams were built before playback'
            elif session.candidates.prepared_streams:
                assert not any(session.directions.slots), 'Prepared streams were built before playback'
            frames.append(frame)
            navigated.append(time.monotonic())
            query = parse_qs(urlparse(frame['url']).query)
            playlist = query['list'][0]
            video = query.get('v', [provider._tracks[playlist][0]['videoId']])[0]
            if session.seeded_launch:
                assert [t['videoId'] for t in provider._tracks[playlist]] == [video]
                assert provider.operations == {'create_playlist': 1}
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
    sync_account = store.accounts.upsert_account('Isolated sync check', None, False)
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
    report['vibe_generations'] = []

    async def select_vibe(preset):
        row = {'preset': preset}
        report['vibe_generations'].append(row)
        started = time.monotonic()
        response = await client.post('/home/vibe', data={'vibe': preset})
        row['response_seconds'] = time.monotonic() - started
        row['status'] = response.status_code
        assert response.status_code == 200, response.text
        assert f'data-vibe="{preset}"' in response.text
        assert response.text.count('data-recipe=') == 4

    async def start_radio(source):
        response = await client.post('/radio/start', json=source)
        return response, time.monotonic()

    sources = [{'video_id': seed['video_id'], 'menu_id': offer['menu_id']},
               {'video_id': seed['video_id']}, {'playlist_id': playlist[0]}]
    for index, source in enumerate(sources):
        # The startup replay may have restored active playback. Move the simulated
        # player away so a song selection starts a new episode instead of joining it.
        bridge.now_playing = {'playlist_ytm_id': 'isolated-ordinary-playlist', 'video_id': 'ordinary', 'paused': False}
        bridge.now_playing_seen_at = time.monotonic()
        vibe = None
        if index == 1:
            offer = (await client.get('/radio/seeds')).json()
            source = {'video_id': offer['tracks'][0]['video_id'], 'menu_id': offer['menu_id']}
            vibe = asyncio.create_task(select_vibe('relax'))
            await asyncio.sleep(0)
        sync = asyncio.create_task(asyncio.to_thread(sync_identity, store, sync_account, sync_client, now=time.time()))
        vibe_phase = ('returned' if vibe.done() else 'assembling') if vibe else None
        source = {**source, 'launch_id': f'actual-library-{index}'}
        before = time.monotonic()
        provider.operations.clear()
        pending = asyncio.create_task(start_radio(source))
        probes = []
        while not pending.done():
            for route in ('/static/favicon.svg', '/bridge/status', '/radio/status'):
                at = time.monotonic()
                response = await asyncio.wait_for(client.get(route), 12)
                probes.append({'path': route, 'seconds': time.monotonic() - at, 'status': response.status_code})
                assert response.status_code == 200
            assert time.monotonic() - before < 60, 'Radio launch exceeded 60s'
            await asyncio.sleep(.1)
        response, responded = pending.result()
        if response.status_code!=200:
            report['radio_launch_failure'] = {'source':source,'status':response.status_code,'body':response.text[:2000],
                'seconds':time.monotonic()-before,'probes':probes}
        assert response.status_code==200, (response.status_code,response.text)
        result = response.json()
        assert result.get('ok'), result
        state = {}
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            state = (await client.get('/radio/status')).json()
            playlist_id = state.get('playlist_id')
            if (state.get('launch_id') == source['launch_id'] and state['active'] and state['position'] >= 0
                    and playlist_id in provider._tracks and bridge.now_playing['playlist_ytm_id'] == playlist_id):
                confirmed = time.monotonic()
                break
            await asyncio.sleep(.2)
        else:
            raise AssertionError((source, {key: state.get(key) for key in (
                'launch_id', 'active', 'position', 'error', 'playlist_id')}))
        remote = [row['videoId'] for row in provider._tracks[state['playlist_id']]]
        if 'video_id' in source:
            assert remote[0] == bridge.now_playing['video_id'] == source['video_id']
            assert state['tracks'][0]['video_id'] == remote[0]
        else:
            assert [track['video_id'] for track in state['tracks']] == remote
        launch = {'source': source, 'seconds': confirmed - before,
                  'response_seconds': responded - before,
                  'navigation_seconds': navigated[index] - before, 'playlist': state['playlist_id'],
                  'queue': remote, 'probes': probes, 'vibe_phase_at_start': vibe_phase,
                  'provider_operations': dict(provider.operations)}
        report['radio_launches'].append(launch)
        session = ctx.radio.session
        if not tuning_and_vibes:
            deadline = time.monotonic() + 15
            while time.monotonic() < deadline:
                state = (await client.get('/radio/status')).json()
                if len(state['directions']) == 4:
                    break
                await asyncio.sleep(.1)
            directions = state['directions']
            remote = [row['videoId'] for row in provider._tracks[state['playlist_id']]]
            launch['queue'] = remote
            assert [track['video_id'] for track in state['tracks']] == remote
            launch['menu_seconds'] = time.monotonic() - before
            launch['groups'] = [branch['cluster_id'] for branch in directions]
            launch['stream_lengths'] = [len(branch['tracks']) for branch in directions]
            assert len(set(launch['groups'])) == 4, launch
            active = next(branch for branch in directions if branch['active'])
            assert [track['video_id'] for track in active['tracks'][:len(remote) - 1]] == remote[1:]
            assert len(active['tracks']) == 6
        replay = await client.post('/radio/start', json=source)
        assert replay.status_code == 200 and replay.json().get('playlist_id') == state['playlist_id'], replay.text
        assert provider.operations['create_playlist'] == 1
        if tuning_and_vibes and index < 2:
            launch['vibe_phase_at_tune'] = ('returned' if vibe.done() else 'assembling') if vibe else None
            before_tune = time.monotonic()
            tune = await client.post('/radio/tune', json={**state['tuning'], 'energy': .23})
            tuned = tune.json()
            launch['chill_seconds'] = time.monotonic() - before_tune
            assert tune.status_code == 200 and tuned['ok'], tuned
            assert tuned['tuning']['energy'] == .23 and tuned['directions'], tuned
            assert tuned['active'] and tuned['playlist_id'] == state['playlist_id']
            assert bridge.now_playing['video_id'] == source['video_id']
        await asyncio.wait_for(sync, 15)
        if vibe is not None:
            await vibe
            if tuning_and_vibes:
                await select_vibe('energize')
        await client.post('/radio/stop')
    assert len(frames) == len(sources), frames
    if not tuning_and_vibes:
        assert all(launch['seconds'] <= 2.0 for launch in report['radio_launches']), [
            (launch['source'], launch['seconds']) for launch in report['radio_launches']]
    report['radio_navigation_frames'] = frames
    report['controls'] += ' Radio launches use controlled provider create/add/readback and simulated native playback; '
    report['controls'] += 'a real sync runs on an additional isolated account. Automatic sync is suppressed.'


async def check(path, report_path, *, launch_radio=False, launch_mix=None, check_ownership=False,
                tuning_and_vibes=False, trace_schema=False, unrelated_radio=False):
    from tests.library_image.fixtures import managed_database
    cache = Path(__file__).resolve().parents[2] / '.pytest_cache'
    assert (managed_database(path) == path or
            (path.is_relative_to(Path('/tmp')) or path.is_relative_to(cache)) and path.name.startswith('tuneconsole-'))
    os.environ['YT_PLAYLIST_HOME'] = str(path.with_suffix('.home'))
    os.environ.pop('DEEPSEEK_API_KEY', None)
    from yt_playlist.core.bridge import Bridge
    from yt_playlist.core.store import Store
    from yt_playlist.providers import enrichment
    from yt_playlist.rec import transient
    from yt_playlist.web.app import create_app
    from yt_playlist.web.radio_launch import RadioLaunch

    counts = Counter()
    report = {'snapshot': str(path), 'bytes': path.stat().st_size,
              'python': sys.version, 'sqlite': sqlite3.sqlite_version, 'uvloop': uvloop.__version__,
              'status': 'failed', 'requests': [],
              'controls': 'Real storage, Radio, recommendation and enrichment workers; no live provider clients; '
                          'enrichment waterfall and artwork network calls suppressed; '
                          'provider availability controlled false; generated-playlist GC deferred.'}
    resume, adopt, configure = RadioLaunch.resume, RadioLaunch.adopt, enrichment.provider_configuration

    def observed_resume(*args, **kwargs):
        session = resume(*args, **kwargs)
        if session is not None:
            counts['radio_checkpoint_restored'] += 1
        return session

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
        'paid_requests':'SELECT * FROM album_description_requests ORDER BY id',
        'saved_recipes':'''SELECT id,recipe_id,recipe_revision,recipe_created_at,recipe_media,recipe_model,recipe_journey,
            recipe_opportunity_id,recipe_option_id,recipe_recovery_filename,recipe_recovery_digest,recipe_recovery_bytes,
            recipe_facets FROM playlist_scopes WHERE recipe_revision IS NOT NULL ORDER BY recipe_id''',
        'road_trip_recipes':'SELECT * FROM road_trip_recipes ORDER BY id',
        'user_choices':('SELECT s.id,h.key,h.atom FROM track_sources s,json_each(s.choice_observations) h '
                        'ORDER BY s.id,h.key'),
    }
    def fingerprints(*, added_recipe_id=None):
        result = {}
        for name,query in protected_queries.items():
            digest,count = hashlib.sha256(),0
            excluded_column = 'recipe_id' if name == 'saved_recipes' else None
            for row in store.conn.execute(query):
                if added_recipe_id is not None and excluded_column and row[excluded_column] == added_recipe_id:
                    continue
                digest.update(json.dumps(tuple(row),ensure_ascii=False,allow_nan=False).encode())
                digest.update(b'\n')
                count += 1
            result[name] = {'rows':count,'sha256':digest.hexdigest()}
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
    schema_queries = {}
    preparation_schema_queries = {}
    preparation_trace = threading.local()
    launch_trace = ContextVar('radio_launch_trace', default=None)
    report['radio_launch_sql'] = []
    catalog_sql = re.compile(r'\bsqlite_(?:temp_)?(?:schema|master)\b|'
                             r'\b(?:pragma[._ ]+)(?:\w+\.)?(?:table_[x]?info|table_list|index_[x]?info|'
                             r'index_list|foreign_key_list|schema_version|user_version)\b', re.IGNORECASE)
    from yt_playlist.core.db_diagnostics import DiagnosticConnection, DiagnosticCursor
    from yt_playlist.web.radio_session import PlaylistRadio
    call = DiagnosticCursor._call
    finish = DiagnosticConnection._finish
    start = PlaylistRadio._start

    def trace_preparation(operation):
        def traced(*args, **kwargs):
            active = getattr(preparation_trace, 'active', False)
            preparation_trace.active = True
            try:
                return operation(*args, **kwargs)
            finally:
                preparation_trace.active = active
        return traced

    def record_sql(stats, stage, sql, started):
        elapsed = time.perf_counter() - started
        row = stats.setdefault((stage, sql), {'count': 0, 'seconds': 0.0})
        row['count'] += 1
        row['seconds'] += elapsed

    def traced_start(manager, *args, **kwargs):
        stats = {}
        token = launch_trace.set(stats)
        started = time.perf_counter()
        try:
            return start(manager, *args, **kwargs)
        finally:
            launch_trace.reset(token)
            report['radio_launch_sql'].append({
                'elapsed_seconds': time.perf_counter() - started,
                'driver_calls': sum(row['count'] for row in stats.values()),
                'sql_calls': sum(row['count'] for (stage, _), row in stats.items()
                                 if stage in ('execute', 'executemany', 'executescript')),
                'driver_seconds': sum(row['seconds'] for row in stats.values()),
                'commits': stats.get(('transaction', 'COMMIT'), {}).get('count', 0),
                'launch_id': kwargs['launch'].launch_id,
                'playlist_source': kwargs.get('playlist_id'),
                'queries': [dict(stage=stage, sql=' '.join((sql or '').split()), **row)
                            for (stage, sql), row in stats.items()],
            })

    def traced_finish(conn, sql, target, *args):
        stats = launch_trace.get()
        if stats is None or not conn.in_transaction:
            return finish(conn, sql, target, *args)
        started = time.perf_counter()
        try:
            return finish(conn, sql, target, *args)
        finally:
            record_sql(stats, 'transaction', sql, started)

    def owned_call(cursor, stage, target, *args):
        if check_ownership and cursor.connection is store.conn and not store._lock.held_by_current_thread:
            error = {'thread': threading.current_thread().name, 'stage': stage,
                     'stack': traceback.format_stack(limit=15)}
            if len(ownership_errors) < 20:
                ownership_errors.append(error)
            # Check before entering SQLite; reproducing a connection-mutex/GIL
            # deadlock would prevent even asyncio's timeout from firing.
            raise AssertionError('Serving connection used without its lock')
        if trace_schema and stage in ('execute', 'executemany', 'executescript') and catalog_sql.search(args[0]):
            sql = args[0]
            queries = preparation_schema_queries if getattr(preparation_trace, 'active', False) else schema_queries
            if sql not in queries:
                queries[sql] = dict(count=0, stack=traceback.format_stack(limit=12))
            queries[sql]['count'] += 1
        stats = launch_trace.get()
        if stats is None:
            return call(cursor, stage, target, *args)
        started = time.perf_counter()
        try:
            return call(cursor, stage, target, *args)
        finally:
            record_sql(stats, stage, cursor._sql, started)
    ownership = patch.object(DiagnosticCursor, '_call', owned_call)
    launched_playlist = None
    try:
        before = time.monotonic()
        if trace_schema:
            trace_preparation(store.init_schema)()
        else:
            store.init_schema()
        report['admission_seconds'] = time.monotonic() - before
        protected_before = fingerprints()
        report['protected_before'] = protected_before
        history = list(map(tuple, store.conn.execute('SELECT * FROM schema_migrations ORDER BY version')))
        saved = store.radio_checkpoints.load()
        checkpoint = saved[0] or {}
        queue = [track['video_id'] for track in checkpoint.get('tracks', [])]
        tracks = checkpoint.get('tracks') or checkpoint.get('initial_tracks') or []
        matching = bool(not unrelated_radio and not checkpoint.get('stopped') and tracks
                        and checkpoint.get('playlist') and checkpoint.get('identity') is not None)
        report['saved_radio_queue_size'] = len(queue)
        report['radio_startup_presence'] = 'matching checkpoint' if matching else 'unrelated playlist'
        if matching:
            report['controls'] += ' The restored episode monitor is held while its saved queue is checked.'
        bridge = Bridge()
        registry = {provider['name']: SimpleNamespace(available=lambda store: False) for provider in enrichment.PROVIDERS}
        start_monitor = PlaylistRadio._start_monitor

        def start_live_monitor(manager, session):
            # The saved queue is compared before the simulated player can advance it.
            if not matching or session.episode_id != checkpoint['episode_id']:
                return start_monitor(manager, session)

        @asynccontextmanager
        async def player_presence():
            async def heartbeat():
                while True:
                    if bridge.now_playing is not None:
                        bridge.now_playing_seen_at = time.monotonic()
                    await asyncio.sleep(.25)

            task = asyncio.create_task(heartbeat())
            try:
                yield
            finally:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)

        with patch.object(RadioLaunch, 'resume', observed_resume), \
             patch.object(RadioLaunch, 'adopt', observed_adopt), \
             patch.object(PlaylistRadio, '_start_monitor', start_live_monitor), \
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
            if matching:
                position = checkpoint.get('position', -1)
                track = tracks[position] if 0 <= position < len(tracks) else tracks[0]
                bridge.now_playing.update(video_id=track['video_id'], playlist_ytm_id=checkpoint['playlist'])
                ctx.client_provider = lambda: {checkpoint['identity']: SimpleNamespace()}
            bridge.now_playing_seen_at = time.monotonic()
            ownership.start()
            async with player_presence(), app.router.lifespan_context(app):
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
                            if route == '/radio/status':
                                state = response.json()
                                assert [track['video_id'] for track in state['tracks']] == queue
                                assert state['active'] == matching
                        await asyncio.to_thread(transient.radio_list_ids, store)
                        await asyncio.sleep(.1)
                    if matching:
                        assert counts['radio_checkpoint_restored'], counts
                        assert ctx.radio.session.episode_id == checkpoint['episode_id']
                    else:
                        assert counts['radio_adoption'] and not counts['radio_checkpoint_restored'], counts
                        assert ctx.radio.session is None
                    if launch_mix is not None:
                        launched_playlist = await launch_mix(client, ctx, store, bridge, report)
                        assert launched_playlist
                        save_report()
                    if launch_radio:
                        with store._lock:
                            assert fingerprints()==protected_before
                            assert store.radio_checkpoints.load() == saved
                        report['protected_before_controlled_radio_exact'] = True
                        with patch.object(PlaylistRadio, '_start', traced_start), \
                             patch.object(DiagnosticConnection, '_finish', traced_finish):
                            await radio_launches(client, ctx, store, bridge, report,
                                                 tuning_and_vibes=tuning_and_vibes)
            assert not ownership_errors, ownership_errors
            assert not schema_queries, {sql: detail['count'] for sql, detail in schema_queries.items()}
            with store._lock:
                assert list(map(tuple, store.conn.execute('SELECT * FROM schema_migrations ORDER BY version'))) == history
                saved_radio_exact = store.radio_checkpoints.load() == saved
                if not launch_radio or store.activity_paused:
                    assert saved_radio_exact
                added_recipe_id = None
                if launched_playlist is not None:
                    recipes = store.conn.execute(
                        'SELECT recipe_id AS id FROM playlist_scopes '
                        'WHERE provider_playlist_id=? AND recipe_revision IS NOT NULL', (launched_playlist,)).fetchall()
                    assert len(recipes) == 1, (launched_playlist, recipes)
                    added_recipe_id = recipes[0]['id']
                    report['expected_added_mix_recipe'] = {'playlist': launched_playlist, 'id': added_recipe_id}
                assert fingerprints(added_recipe_id=added_recipe_id) == protected_before
                report['protected_sources_and_paid_files_exact'] = True
                report['regular_history'] = list(map(tuple,store.conn.execute('SELECT version,name FROM schema_changes ORDER BY version')))
            assert counts['enrichment_configuration'], counts
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
        if trace_schema:
            report['startup_preparation_schema_queries'] = preparation_schema_queries
            report['runtime_schema_queries'] = schema_queries
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
                        check_ownership='--check-ownership' in sys.argv[3:],
                        unrelated_radio='--unrelated-radio' in sys.argv[3:]))
    finally:
        faulthandler.cancel_dump_traceback_later()
