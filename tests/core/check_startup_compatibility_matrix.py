"""Controlled fresh/partial/current startup beside real background workers.

This complements the populated actual-library experiment. It does not replace
its slow-phase, file-preservation or controlled Radio/provider checks.
"""
import asyncio
from tests.enrichment_support import choose_field
from functools import partial
import json
import os
from pathlib import Path
import sqlite3
import sys
import time
from unittest.mock import patch

import httpx
import uvloop

from yt_playlist.core.bridge import Bridge
from yt_playlist.core.migrations import registry, regular, runner
from yt_playlist.core.preparation import metadata_destinations as destination, metadata_evidence as preparation
from yt_playlist.core.store import Store
from yt_playlist.providers import enrichment
from yt_playlist.web.app import create_app


async def case(root, version):
    name = 'fresh' if version is None else str(version)
    path = root/('tuneconsole-'+name+'.db')
    assert not path.exists()
    os.environ['YT_PLAYLIST_HOME'] = str((root/('home-'+name)).resolve())
    os.environ.pop('DEEPSEEK_API_KEY', None)
    store = Store(path)
    report = {'case':name,'fixture':str(path),'requests':[],'writes':[],'status':'failed'}
    try:
        if version is not None:
            runner.migrate(store.conn,registry=registry.MIGRATIONS[:version])
        started = time.monotonic()
        store.init_schema(defer_metadata=True)
        report['admission_seconds'] = time.monotonic()-started
        account = None
        if version is not None:
            account = store.accounts.upsert_account('Controlled matrix','isolated.json',None,True)
            for number in range(4):
                store.tracks.upsert_track('matrix-'+str(number),'Matrix '+str(number),'Fixture',None,200)
            if version==79:
                from tests.core.test_metadata_owner_cutover import seed
                seed(store)
        history = list(map(tuple,store.conn.execute('SELECT * FROM schema_migrations ORDER BY version')))
        bridge = Bridge()
        with patch('yt_playlist.web.app._background_sync_loop'), \
                patch('yt_playlist.enrich.artwork.retry_due',return_value=0):
            app = create_app(store,lambda:{},bridge=bridge,background_work=True)
            ctx = app.state.ctx
            ctx.rec_worker.gc_initial_s = 3600
            ctx.enrich_worker.waterfall_fn = partial(lambda *args,**kwargs:[],registry={})
            ctx.enrich_worker.idle_sleep_s = .2
            async with app.router.lifespan_context(app):
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://localhost') as client:
                    async def probe(phase):
                        for route in ('/','/home/onboard/status','/radio/status','/bridge/status'):
                            began = time.monotonic()
                            response = await asyncio.wait_for(client.get(route),10)
                            report['requests'].append({'phase':phase,'path':route,'status':response.status_code,
                                'seconds':time.monotonic()-began})
                            assert response.status_code==200,(name,phase,route,response.text[:500])

                    async def write(phase):
                        def commit():
                            began = time.monotonic()
                            store.settings.set_setting('matrix-continuing-write',phase)
                            if account is not None:
                                store.history.record_play_event(account,'matrix 0|fixture','matrix-0',time.time())
                            report['writes'].append({'phase':phase,'seconds':time.monotonic()-began})
                        await asyncio.to_thread(commit)

                    await probe('startup')
                    assert await asyncio.to_thread(ctx.storage_maintenance.ready.wait,15)
                    await write('regular-ready')
                    await probe('regular-ready')
                    if version==79:
                        def prepare_partial():
                            with sqlite3.connect(path) as conn:
                                conn.row_factory = sqlite3.Row
                                conn.execute('PRAGMA foreign_keys=ON')
                                preparation.JOB.install(conn)
                                return preparation.JOB.step(conn,batch_size=1)
                        progress = await asyncio.to_thread(prepare_partial)
                        assert progress.status!='prepared'
                        report['partial_preparation_status'] = progress.status
                        await write('partial')
                        await probe('partial')
                        def select():
                            from tests.core.test_metadata_destinations import stage
                            with sqlite3.connect(path) as conn:
                                conn.row_factory = sqlite3.Row
                                conn.execute('PRAGMA foreign_keys=ON')
                                identity = stage(conn)
                                destination.publish(conn,identity=identity)
                                return identity
                        identity = await asyncio.to_thread(select)
                        with store._lock:
                            assert destination.selected(store.conn)==identity
                        report['metadata_selected'] = identity
                        await probe('selected')
                        # A real compatible writer withdraws the selection in
                        # its transaction and continues serving installed owners.
                        await asyncio.to_thread(choose_field, store,
                            store.tracks.track_ids_for_videos(['matrix-0'])['matrix-0'],'genre','Jazz')
                        with store._lock:
                            assert destination.selected(store.conn) is None
                        await write('withdrawn')
                        await probe('withdrawn')
                    await write('final')
                    await probe('final')
                    with store._lock:
                        report['regular_history'] = list(map(tuple,store.conn.execute(
                            'SELECT version,name FROM schema_changes ORDER BY version')))
                    assert report['regular_history']==[(change.version,change.name) for change in regular.CHANGES]
                    assert not store.activity_paused
            with store._lock:
                assert list(map(tuple,store.conn.execute('SELECT * FROM schema_migrations ORDER BY version')))==history
            report['history_exact'] = True
    except BaseException as exc:
        report['error'] = repr(exc)
        raise
    finally:
        store.conn.close()
        (root/(name+'.json')).write_text(json.dumps(report,indent=2)+'\n')
    reopened = Store(path)
    try:
        reopened.init_schema(defer_metadata=True)
        assert reopened.settings.get_setting('matrix-continuing-write')=='final'
        assert list(map(tuple,reopened.conn.execute('SELECT * FROM schema_migrations ORDER BY version')))==history
        regular.validate(reopened.conn)
        report.update(status='passed',restart_exact=True)
        (root/(name+'.json')).write_text(json.dumps(report,indent=2)+'\n')
    finally:
        reopened.conn.close()
    return report


async def check(root):
    assert root.resolve().is_relative_to(Path('.pytest_cache').resolve())
    root.mkdir(parents=True,exist_ok=True)
    report = {'python':sys.version,'sqlite':sqlite3.sqlite_version,'uvloop':uvloop.__version__,
        'scope':'Controlled small fresh/79/93/107 layouts, real background workers, no provider clients; '
        'artwork network and automatic sync suppressed, generated-playlist GC deferred. '
        'Not actual-library latency, slow-phase or rendered UI acceptance.','cases':[]}
    for version in (None,79,93,107):
        result = await case(root,version)
        report['cases'].append(result)
        print(json.dumps({'case':result['case'],'status':result['status']}),flush=True)
        (root/'matrix.json').write_text(json.dumps(report,indent=2)+'\n')


if __name__=='__main__':
    asyncio.set_event_loop_policy(uvloop.EventLoopPolicy())
    uvloop.run(check(Path(sys.argv[1])))
