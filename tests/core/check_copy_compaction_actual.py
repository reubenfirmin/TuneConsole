"""Repair borrowed fixture coordinates, then rehearse real app cutover in isolation.

The input SQL must already be this experiment's disposable retained-copy output.
Only that SQL and its new file directories are written. The producer snapshot
and the native library are never modified. Fixture coordinate repair is measured
separately from maintenance; it is not production compaction behavior.
"""
from contextlib import closing
import json
from pathlib import Path
import shutil
import sqlite3
import sys
import time

from fastapi.testclient import TestClient

from tests.conftest import FakeClient
from yt_playlist.core import (album_description_files, collection_recovery, copy_compaction,
    group_documents, legacy_evidence, playback_feedback, policy_recovery, preparation_files,
    recommendation_outputs, retained_copy, saved_preparation_files, sample_files)
from yt_playlist.core.store import Store
from yt_playlist.web.app import create_app


def rebuild_exported_fixture(source, root):
    """Recover this disposable experiment from its surviving complete exports.

    This is an explicit fixture repair after shared pytest cache removal. It
    searches only the ten known export domains, then lets each production
    restore codec verify SQL identities, content digests and nested file graphs.
    The source SQL and export trees remain read-only throughout.
    """
    source, root = source.resolve(strict=True), root.resolve()
    assert source.parent.name == '.retained.db.compaction-c5893cada2f24f53a4cc539e48df853e'
    assert root == Path('backups/658-actual-rehearsal').resolve()
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    fixture = root/'retained.db'
    marker = root/'fixture-preparation.json'
    if marker.exists():
        return fixture
    started = time.monotonic()
    journal = root/'fixture-reconstruction.json'
    report = json.loads(journal.read_text()) if journal.exists() else dict(
        source=str(source), scope='Previously filtered actual-library SQL and its surviving full file exports',
        domains={}, started_at=time.time())
    if not fixture.exists():
        shutil.copyfile(source, fixture)
    if 'sql_sha256' not in report:
        assert copy_compaction._digest(source) == copy_compaction._digest(fixture)
        report['sql_sha256'] = copy_compaction._digest(fixture)
        journal.write_text(json.dumps(report,indent=2)+'\n')
    kinds = dict(samples='sample-backup', annotations='backup-manifest',
        policies='backup-manifest', descriptions='backup-manifest', groups='manifest',
        recommendations='manifest', metadata='backup-manifest', preparation='preparation-manifest',
        archives='backup-manifest')
    with closing(sqlite3.connect(fixture)) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute('PRAGMA foreign_keys=ON')
        for domain, kind in kinds.items():
            if domain in report['domains']:
                continue
            began, matches = time.monotonic(), []
            for path in (source.parent/'original-files'/domain).glob('*/*.json'):
                with path.open('rb') as stream:
                    encoded = stream.read(8*1024*1024+1)
                if len(encoded)>8*1024*1024:
                    continue
                try:
                    value = json.loads(encoded)
                    if domain=='policies':
                        value = value.get('configuration')
                    elif domain=='descriptions':
                        value = json.loads(value.get('record_json', 'null'))
                    if isinstance(value,dict) and value.get('type',value.get('kind'))==kind:
                        matches.append(path)
                except (ValueError, AttributeError):
                    continue  # Packed content records are not root manifests.
            assert len(matches)==1,(domain,matches)
            print(json.dumps(dict(event='fixture_restore_start',domain=domain)),flush=True)
            policy_recovery._restore_files(conn,{domain:str(matches[0])})
            report['domains'][domain] = dict(manifest=str(matches[0]),seconds=time.monotonic()-began)
            journal.write_text(json.dumps(report,indent=2)+'\n')
            print(json.dumps(dict(event='fixture_restore_complete',domain=domain,
                                 seconds=report['domains'][domain]['seconds'])),flush=True)
        if 'playback_feedback' not in report['domains']:
            snapshot = policy_recovery.playback_feedback_backup._queue(
                source.parent/'original-files/playback-feedback/snapshot.db').path
            manifest = dict(format=1,database=str(snapshot),sha256=copy_compaction._digest(snapshot))
            policy_recovery._restore_files(conn,{'playback_feedback':manifest})
            report['domains']['playback_feedback'] = dict(manifest=manifest)
            journal.write_text(json.dumps(report,indent=2)+'\n')
        policy_recovery._verify(conn)
        report.update(seconds=time.monotonic()-started,files=copy_compaction._owned_files(conn),
                      installed_version=retained_copy._format(conn))
    marker.write_text(json.dumps(report,indent=2)+'\n')
    return fixture


def coordinates(conn):
    """Explicit owners only, matching the recovery codecs used for the fixture."""
    columns = [('sample_attributes','filename'), ('audio_annotations','model_output_filename'),
               ('recommendation_policies','configuration_filename'),
               ('recommendation_policy_bodies','configuration_filename'),
               ('metadata_deliveries','raw_filename'),('action_backups','retained_filename'),
               ('action_steps','backup_filename'),('published_metadata_destination_deliveries','filename')]
    columns.extend((table,owner[1]) for table,owner in album_description_files.OWNERS.items())
    columns.extend((table,'filename') for table in legacy_evidence.DOMAINS.values())
    columns.extend((table,prefix+'_filename') for table,_,prefix,_ in group_documents.OWNERS)
    columns.extend((table,'recovery_filename') for table in collection_recovery.OWNERS)
    columns.extend((table,'filename') for table in recommendation_outputs.OUTPUT_OWNERS)
    columns.extend((table,'filename') for table in saved_preparation_files.OWNERS)
    for _,_,table,_,slots in preparation_files._owners(conn):
        columns.extend((table,slot[0]) for slot in slots)
    tables = policy_recovery._tables(conn)
    result = {}
    for table,column in columns:
        if table in tables and column in {row[1] for row in conn.execute('PRAGMA table_info('+table+')')}:
            result.setdefault(table,set()).add(column)
    return result


def prepare(producer,fixture,root):
    started = time.monotonic()
    borrowed = '/home/rfirmin/.local/share/yt-playlist/state.db'
    with closing(sqlite3.connect(producer.as_uri()+'?mode=ro',uri=True)) as original, \
            closing(sqlite3.connect(fixture.as_uri()+'?mode=ro',uri=True)) as reader:
        original.row_factory = reader.row_factory = sqlite3.Row
        def local(filename):
            if filename is None:
                return None
            if filename.startswith(borrowed+'.'):
                filename = str(producer)+filename[len(borrowed):]
            return str(Path(filename).resolve(strict=True))
        reader.create_function('fixture_filename',1,local)
        for table,fields in coordinates(reader).items():
            columns = [row[1] for row in reader.execute('PRAGMA main.table_info('+table+')')]
            select = ','.join(('fixture_filename('+name+') AS '+name) if name in fields else name for name in columns)
            reader.execute('CREATE TEMP VIEW '+table+' AS SELECT '+select+' FROM main.'+table)
        reader.execute('BEGIN')
        with copy_compaction._files(original):
            manifests = policy_recovery._export_files(reader,fixture,backup_root=root/'fixture-files')
        reader.rollback()
    with closing(sqlite3.connect(fixture)) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute('PRAGMA foreign_keys=ON')
        policy_recovery._restore_files(conn,manifests)
        policy_recovery._verify(conn)
        files = copy_compaction._owned_files(conn)
    return dict(seconds=time.monotonic()-started,files=files,manifests=manifests)


def check(producer,fixture,output):
    producer,fixture,output = producer.resolve(strict=True),fixture.resolve(strict=True),output.absolute()
    assert producer.is_relative_to(Path('.pytest_cache').resolve()) or producer.parent.name == \
        '.retained.db.compaction-c5893cada2f24f53a4cc539e48df853e'
    assert fixture.parent==output.parent and (fixture.parent.name in {
        '658-actual-direct-copy', 'tuneconsole-658-actual-cutover'} or
        fixture.parent==Path('backups/658-actual-rehearsal').resolve())
    report = dict(producer=str(producer),fixture=str(fixture),python=sys.version,sqlite=sqlite3.sqlite_version,
        scope='Actual isolated library already filtered by the separately measured direct-copy experiment. '
              'Borrowed native coordinates are mapped to the existing producer snapshot only during '
              'fixture export; production compaction still refuses unavailable files. No native writes.')
    store,client,ctx = None,None,None
    try:
        if not (fixture.parent/'fixture-preparation.json').exists():
            prepared = prepare(producer,fixture,fixture.parent)
            (fixture.parent/'fixture-preparation.json').write_text(json.dumps(prepared,indent=2)+'\n')
        report['fixture_preparation'] = json.loads((fixture.parent/'fixture-preparation.json').read_text())
        sample_report = fixture.parent/'sample-preparation.json'
        if not sample_report.exists():
            started = time.monotonic()
            with closing(sqlite3.connect(fixture)) as conn:
                conn.row_factory = sqlite3.Row
                before = {row[0]: Path(row[1]).read_bytes() for row in conn.execute(
                    'SELECT id,filename FROM sample_attributes')}
                conn.execute('BEGIN')
                manifest = sample_files.export(conn, fixture.parent/'fixture-samples')
                conn.rollback()
                sample_files.restore(conn, manifest)
                after = {row[0]: Path(row[1]).read_bytes() for row in conn.execute(
                    'SELECT id,filename FROM sample_attributes')}
                assert before == after
            sample_report.write_text(json.dumps(dict(samples=len(before),exact_bytes=True,
                seconds=time.monotonic()-started,manifest=manifest),indent=2)+'\n')
        report['actual_samples'] = json.loads(sample_report.read_text())
        store = Store(fixture)
        store.init_schema(defer_metadata=True)
        actual_samples = {row['id']:Path(row['filename']).read_bytes()
                          for row in store.conn.execute('SELECT id,filename FROM sample_attributes')}
        sample_runs = [row[0] for row in store.conn.execute('SELECT DISTINCT run_id FROM sample_attributes')]
        expected_samples = {run:store.audio_vibes.samples(run) for run in sample_runs}
        assert sum(map(len,expected_samples.values())) == len(actual_samples)
        report['installed_version_before'] = retained_copy._format(store.conn)
        accounts = store.accounts.get_accounts()
        assert accounts
        account = store.accounts.get_master_account().id
        video, = store.conn.execute('SELECT video_id FROM youtube_items WHERE video_id IS NOT NULL LIMIT 1').fetchone()
        remote = FakeClient()
        app = create_app(store,lambda:{row.id:remote for row in accounts},background_work=False)
        client = TestClient(app,base_url='http://127.0.0.1')
        ctx = app.state.ctx
        playback_feedback.try_drain(store)
        report['http'] = []
        def browse():
            for path in ('/','/playlists','/track/'+video,'/radio/status','/storage-maintenance/status'):
                began = time.monotonic()
                print(json.dumps(dict(event='http_start',path=path)),flush=True)
                response = client.get(path)
                report['http'].append(dict(path=path,status=response.status_code,seconds=time.monotonic()-began))
                print(json.dumps(report['http'][-1]),flush=True)
                assert response.status_code==200,(path,response.status_code,response.text[:200])
        browse()
        began = time.monotonic()
        assert client.post('/storage-maintenance/compact').status_code==202
        deadline = began+5400
        while not store.activity_paused and not ctx.storage_maintenance.ready.wait(.05):
            assert time.monotonic()<deadline
        if store.activity_paused:
            response = client.post('/track/like',data={'video_id':video,'on':'1'})
            assert response.status_code==200,response.text[:200]
            playback_feedback.record_next(store,account,video,20,180,None,time.time())
            report['feedback_captured_during_maintenance'] = playback_feedback.inbox(store).pending()
            assert report['feedback_captured_during_maintenance']>=2
            assert client.post('/sync').status_code==503
        while not ctx.storage_maintenance.ready.wait(30):
            print(json.dumps(dict(event='maintenance',phase=ctx.storage_maintenance.phase)),flush=True)
            browse()
            assert time.monotonic()<deadline
        report['request_to_resumed_seconds'] = time.monotonic()-began
        report['maintenance_status'] = ctx.storage_maintenance.status()
        report['compaction'] = ctx.storage_maintenance.report
        assert ctx.storage_maintenance.phase=='done',report['maintenance_status']
        assert not store.activity_paused and not playback_feedback.inbox(store).pending()
        assert store.tracks.item_rating(account,video)['status']=='LIKE'
        assert retained_copy._format(store.conn)==report['installed_version_before']
        store.settings.set_setting('658-isolated-post-cutover','accepted')
        browse()
        report['installed_files_verified'] = copy_compaction._owned_files(store.conn)
        assert actual_samples == {row['id']:Path(row['filename']).read_bytes()
                                  for row in store.conn.execute('SELECT id,filename FROM sample_attributes')}
        assert expected_samples == {run:store.audio_vibes.samples(run) for run in sample_runs}
        report['actual_sample_reader_verified'] = len(actual_samples)
        report['free_bytes_after'] = shutil.disk_usage(fixture.parent).free
        report['status'] = 'passed'
    except BaseException as error:
        report.update(status='failed',error=repr(error))
        if store is not None:
            report['durable_state'] = copy_compaction.db_handles.state(fixture)
        raise
    finally:
        if ctx is not None:
            ctx.storage_maintenance.stop()
            ctx.storage_maintenance.join(60)
        if client is not None:
            client.close()
        if store is not None:
            store.conn.close()
        output.write_text(json.dumps(report,indent=2)+'\n')


def test_actual_library_cutover():
    # This check_* module is selected explicitly, outside normal pytest
    # discovery. It uses the documented disposable fixture prepared above.
    root = Path('.pytest_cache/658-actual-direct-copy')
    check(Path('.pytest_cache/621-takeover/tuneconsole-startup.db'),
          root/'retained.db',root/'cutover.json')


def test_actual_library_cutover_on_roomy_filesystem():
    source = Path('.pytest_cache/658-actual-direct-copy')
    root = Path('/tmp/tuneconsole-658-actual-cutover')
    root.mkdir(mode=0o700, exist_ok=True)
    fixture = root/'retained.db'
    if not fixture.exists():
        with closing(sqlite3.connect((source/'retained.db').resolve().as_uri()+'?mode=ro',uri=True)) as reader, \
                closing(sqlite3.connect(fixture)) as target:
            reader.backup(target, pages=512)
        shutil.copyfile(source/'fixture-preparation.json',root/'fixture-preparation.json')
    check(Path('.pytest_cache/621-takeover/tuneconsole-startup.db'),fixture,root/'cutover.json')


def test_actual_library_cutover_from_surviving_exports():
    source = Path('/tmp/tuneconsole-658-actual-cutover/'
        '.retained.db.compaction-c5893cada2f24f53a4cc539e48df853e/verified-original.db')
    root = Path('backups/658-actual-rehearsal')
    fixture = rebuild_exported_fixture(source,root)
    check(source,fixture,root/'cutover.json')


def test_reopened_actual_library_reads_without_recovery_files():
    """In-place compaction retains live file trees and needs no recovery copies."""
    root = Path('backups/658-actual-rehearsal').resolve(strict=True)
    fixture = root/'retained.db'
    report = json.loads((root/'cutover.json').read_text())
    assert report['status']=='passed'
    prepared = json.loads((root/'fixture-preparation.json').read_text())
    source = Path(prepared['source'])
    assert copy_compaction._digest(source)==prepared['sql_sha256']
    with closing(sqlite3.connect(source.as_uri()+'?mode=ro',uri=True)) as original:
        original_samples = {row[0]:(list(row[:-1]),Path(row[-1]).stem)
                            for row in original.execute(sample_files.REFERENCES)}
    state = copy_compaction.db_handles.state(fixture)
    assert state['phase']=='complete'
    destination = Path(state['destination'])
    assert destination.parent==root
    originals = [destination]
    if Path(state['work'])!=destination:
        originals.append(Path(state['work']))
    hidden, store = [], None
    result = dict(installed_version=report['installed_version_before'],
        source_sql_unchanged=True,hidden_original_roots=[str(path) for path in originals],
        scope='Actual previously filtered v93 fixture; recovery files unavailable before Store startup, '
              'installed external-file directories retained unchanged')
    try:
        for original in originals:
            unavailable = original.with_name(original.name+'.unavailable')
            original.rename(unavailable)
            hidden.append((original,unavailable))
        store = Store(fixture)
        store.init_schema(defer_metadata=True)
        assert retained_copy._format(store.conn)==result['installed_version']
        result['verified_files'] = copy_compaction._owned_files(store.conn)
        samples = {}
        for row in store.conn.execute(sample_files.REFERENCES):
            identity, _, video, run, point, _, filename = row
            assert (list(row[:-1]),Path(filename).stem)==original_samples[identity]
            assert not Path(filename).is_relative_to(destination)
            samples[identity] = sample_files.read(filename,expected=dict(
                id=identity,run_id=run,point=point,video_id=video))
        assert len(samples)==715
        observed = {sample['id']:sample for run, in store.conn.execute(
            'SELECT DISTINCT run_id FROM sample_attributes') for sample in store.audio_vibes.samples(run)}
        assert observed==samples
        result['normal_samples_exact'] = len(samples)
        accounts = store.accounts.get_accounts()
        app = create_app(store,lambda:{account.id:FakeClient() for account in accounts},background_work=False)
        video, = store.conn.execute('SELECT video_id FROM youtube_items LIMIT 1').fetchone()
        with TestClient(app,base_url='http://127.0.0.1') as client:
            result['http'] = {}
            for path in ('/','/playlists','/track/'+video,'/radio/status'):
                response = client.get(path)
                assert response.status_code==200,(path,response.status_code)
                result['http'][path] = response.status_code
        store.settings.set_setting('658-isolated-restart-write','accepted')
        assert not playback_feedback.inbox(store).pending()
        wal = fixture.with_name(fixture.name+'-wal')
        result['database_files'] = dict(sql_bytes=fixture.stat().st_size,
            wal_bytes=wal.stat().st_size if wal.exists() else 0,
            free_bytes=shutil.disk_usage(root).free)
        result['status'] = 'passed'
    except BaseException as error:
        result.update(status='failed',error=repr(error))
        raise
    finally:
        if store is not None:
            store.conn.close()
        for original,unavailable in reversed(hidden):
            if original.exists():
                # Normal reader admission can recreate a canonical directory's
                # lock file. Keep that separate too; never discard fixture data.
                recreated = original.with_name(original.name+'.recreated-during-read')
                assert not recreated.exists()
                original.rename(recreated)
            unavailable.rename(original)
        (root/'reopened-custody.json').write_text(json.dumps(result,indent=2)+'\n')


if __name__=='__main__':
    check(*(Path(value) for value in sys.argv[1:]))
