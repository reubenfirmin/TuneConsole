"""Rehearse #686 on private bytes, retaining only the JSON evidence report.

Run with the project's Python. No providers, native application handles or
normal workers run here. SQLite and every physical file are copied before
application admission; mutable packs are never hardlinked to the donor.
"""
from contextlib import closing
import argparse
import bz2
import faulthandler
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import socket
import tempfile
import time

from yt_playlist.core import db_handles, metadata_retention
from yt_playlist.core.db_rebuild import build, files, install, job
from yt_playlist.core.store import Store
from tests.live_server import LiveServer


def digest(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream,'sha256').hexdigest()


def clone(source, target, account, private, audit):
    """Relocate physical coordinates while preserving captured logical names."""
    target.parent.mkdir(parents=True,exist_ok=True)
    def relocated(path):
        path = Path(path)
        if not path.is_relative_to(account):
            raise ValueError('Unclassified actual-library physical root: '+str(path))
        return private/path.relative_to(account)
    def copy(path):
        path = Path(path)
        destination = relocated(path)
        if str(path) in audit['files']:
            return destination
        if not path.exists():
            audit['missing_files'].append(str(path))
            return destination
        before = digest(path)
        destination.parent.mkdir(parents=True,exist_ok=True)
        shutil.copy2(path,destination)
        if digest(destination)!=before or digest(path)!=before:
            raise ValueError('Donor file changed during capture: '+str(path))
        audit['files'][str(path)] = before
        return destination
    if source.with_name(source.name+'-wal').exists():
        raise ValueError('Qualification requires a checkpointed read-only donor')
    before = digest(source)
    with closing(sqlite3.connect(source.as_uri()+'?mode=ro&immutable=1',uri=True)) as donor:
        donor.row_factory = sqlite3.Row
        owners = [(name,owner.directory(donor),list(refs)) for name,owner,refs in files.owners(donor)]
        replacements = []
        for table in sorted(r[0] for r in donor.execute(
                "SELECT name FROM sqlite_schema WHERE type='table' AND name NOT GLOB 'sqlite_*'")):
            for column in [r[1] for r in donor.execute('PRAGMA table_info('+build.quote(table)+')')
                    if r[1]=='filename' or r[1].endswith(('_filename','_file_path','_manifest_path'))]:
                if (table,column)==('action_backups','filename'):
                    continue
                for value, in donor.execute('SELECT DISTINCT '+build.quote(column)+' FROM '+build.quote(table)
                                            +' WHERE '+build.quote(column)+' IS NOT NULL'):
                    if value and Path(value).is_absolute():
                        replacements.append((table,column,value,str(copy(value))))
        for name,root,references in owners:
            for path in references:
                copy(path.resolve())
            destination = files.roots(target)[name]
            mapped = relocated(root.resolve())
            mapped.mkdir(parents=True,exist_ok=True)
            if destination!=mapped and not destination.exists():
                destination.parent.mkdir(parents=True,exist_ok=True)
                destination.symlink_to(mapped,target_is_directory=True)
        # Evidence artifacts can include unindexed context/history files.
        evidence = files.roots(source)['choice_evidence'].resolve()
        if evidence.exists():
            for path in evidence.rglob('*'):
                if path.is_file():
                    copy(path)
        with closing(sqlite3.connect(target)) as copied:
            donor.backup(copied)
            triggers = list(copied.execute("SELECT name,sql FROM sqlite_schema WHERE type='trigger'"))
            for name,_ in triggers:
                copied.execute('DROP TRIGGER '+build.quote(name))
            grouped = {}
            for table,column,old,new in replacements:
                grouped.setdefault((table,column),[]).append((old,new))
            copied.execute('CREATE TEMP TABLE locators(old TEXT PRIMARY KEY,new TEXT NOT NULL) WITHOUT ROWID')
            for (table,column),values in grouped.items():
                copied.execute('DELETE FROM locators')
                copied.executemany('INSERT INTO locators VALUES (?,?)',values)
                copied.execute('UPDATE '+build.quote(table)+' SET '+build.quote(column)+
                    '=(SELECT new FROM locators WHERE old='+build.quote(column)+') WHERE '+
                    build.quote(column)+' IN (SELECT old FROM locators)')
            for _,sql in triggers:
                copied.execute(sql)
            copied.commit()
        assert donor.total_changes==0
    assert digest(source)==before
    audit['databases'][str(source)] = before
    os.utime(target,ns=(source.stat().st_atime_ns,source.stat().st_mtime_ns))


def storage(conn):
    page_size = conn.execute('PRAGMA page_size').fetchone()[0]
    return dict(sql_bytes=conn.execute('PRAGMA page_count').fetchone()[0]*page_size,
        free_bytes=conn.execute('PRAGMA freelist_count').fetchone()[0]*page_size,
        rows={table:conn.execute('SELECT count(*) FROM '+build.quote(table)).fetchone()[0]
            for table in ('recommendation_candidates','recommendation_generations','group_generations',
                          'group_members','group_graph','artist_credit_observations',*sorted(metadata_retention.RETIRED))})


def measure(args):
    source,report = args.source.resolve(strict=True),args.report.absolute()
    account = args.account.resolve(strict=True)
    result = dict(source=str(source),mode=args.mode,success=False,phases={},files={},missing_files=[],databases={})
    report.parent.mkdir(parents=True,exist_ok=True)
    def save():
        report.write_text(json.dumps(result,indent=2)+'\n')
    with tempfile.TemporaryDirectory(prefix='tuneconsole-686-') as directory:
        private = Path(directory)/'account'
        target = private/source.relative_to(account)
        clone(source,target,account,private,result)
        if args.mode=='installed':
            state = json.loads(bz2.decompress(source.with_name(source.name+'.compaction.json').read_bytes()))
            retained = Path(state['original'])
            clone(retained,private/retained.relative_to(account),account,private,result)
            for key in ('database','work','destination','original','sibling'):
                state[key] = str(private/Path(state[key]).relative_to(account))
            state['original_sha256'] = digest(Path(state['original']))
            install.save(target,state)
        save()
        opened = time.monotonic()
        store = Store(target)
        owner = client = None
        try:
            store.init_schema()
            result['schema_admission_seconds'] = time.monotonic()-opened
            import httpx
            import uvicorn
            from yt_playlist.web.app import create_app
            app = create_app(store,lambda:{},background_work=False)
            maintenance = app.state.ctx.storage_maintenance
            maintenance.ready.clear()
            maintenance.active = args.mode=='fresh'
            maintenance._rebuilding = True
            with socket.socket() as reserve:
                reserve.bind(('127.0.0.1',0))
                port = reserve.getsockname()[1]
            server = uvicorn.Server(uvicorn.Config(app,host='127.0.0.1',port=port,
                lifespan='off',log_level='warning'))
            owner = LiveServer(server)
            result['server_ready_seconds'] = time.monotonic()-opened
            client = httpx.Client(base_url=f'http://127.0.0.1:{port}',timeout=60)
            def http_checks(label):
                checks = {}
                for url in ('/storage-maintenance/status','/playlists','/charts/listening-data'):
                    print('Checking HTTP '+label+' '+url,flush=True)
                    began = time.monotonic()
                    faulthandler.dump_traceback_later(60)
                    try:
                        response = client.get(url)
                    finally:
                        faulthandler.cancel_dump_traceback_later()
                    assert response.status_code==200,(url,response.text[:1000])
                    checks[url] = dict(seconds=time.monotonic()-began,bytes=len(response.content))
                    if url=='/storage-maintenance/status':
                        checks[url]['value'] = response.json()
                    result.setdefault('http',{})[label] = checks
                    save()
                result.setdefault('http',{})[label] = checks
                return checks
            print('Checking initial HTTP usability',flush=True)
            http_checks('before_copy')
            result['first_http_seconds'] = time.monotonic()-opened
            result['before'] = storage(store.conn)
            playlist = store.playlists.get_playlists()[0].id
            members = store.playlists.get_playlist_track_ids(playlist)
            started,last = time.monotonic(),0
            def progress(phase,detail):
                nonlocal last
                name = detail.get('stage',phase)
                result['phases'][name] = round(time.monotonic()-started,3)
                if time.monotonic()-last>15:
                    print(json.dumps(dict(phase=phase,stage=name,seconds=result['phases'][name])),flush=True)
                    save()
                    last = time.monotonic()
                # Cutover callbacks run with all SQLite handles deliberately
                # closed. Serving requests wait on the admission lock there.
                if not getattr(store.conn,'_handle_closed',False):
                    assert store.playlists.get_playlist_track_ids(playlist)==members
                if phase=='analytics' and not name.startswith('finalize:'):
                    store.settings.set_setting('686-accepted-during-analytics',name)
            if args.mode=='fresh':
                job.run(store,rebuild=True,progress=progress)
            maintenance.active = False
            maintenance.report = db_handles.state(target)
            result['runtime'] = storage(store.conn)
            assert not store.activity_paused
            status = http_checks('runtime')['/storage-maintenance/status']['value']
            assert status['recording'] and 'some recommendations' in status['banner_message']
            store.settings.set_setting('686-accepted-after-runtime','preserved')
            result['job'] = job.finish_analytics(store,progress=progress)
            maintenance.report = result['job']
            maintenance.ready.set()
            result['complete'] = storage(store.conn)
            result['owned_files'] = job._owned_files(store.conn)
            assert not http_checks('complete')['/storage-maintenance/status']['value']['banner_message']
            assert store.settings.get_setting('686-accepted-after-runtime')=='preserved'
            assert store.settings.get_setting('686-accepted-during-analytics')
            assert not store.conn.execute('PRAGMA foreign_key_check').fetchall()
            assert not db_handles.state(target)['analytics_pending']
            result.update(success=True,seconds=time.monotonic()-started)
        finally:
            if client is not None:
                client.close()
            if owner is not None:
                owner.close()
            store.conn.close()
            result['changed_donor_files'] = [path for path,expected in result['files'].items()
                                            if digest(Path(path))!=expected]
            result['changed_donor_databases'] = [path for path,expected in result['databases'].items()
                                                if digest(Path(path))!=expected]
            save()
            assert not result['changed_donor_files'] and not result['changed_donor_databases']


if __name__=='__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source',type=Path,required=True)
    parser.add_argument('--account',type=Path,required=True)
    parser.add_argument('--report',type=Path,required=True)
    parser.add_argument('--mode',choices=('fresh','installed'),default='fresh')
    measure(parser.parse_args())
