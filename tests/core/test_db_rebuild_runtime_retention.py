"""Runtime admission excludes history while retaining independent live owners."""
from contextlib import closing
from dataclasses import replace
import hashlib
import inspect
import json
from pathlib import Path
import shutil
import sqlite3

import pytest

from tests.core.test_db_rebuild_destination import production64, development93
from tests.db_rebuild_support import completed_destination_schema, completed_destination_sql
from tests.rebuild_startup_support import startup_destination
from yt_playlist.core import group_storage
from yt_playlist.core.db_rebuild import build, destination


@pytest.fixture(autouse=True)
def completed_rebuild_input(request):
    """This module owns retained values and recovery after schema creation."""
    if not request.node.name.startswith('test_rebuild_input_'):
        request.getfixturevalue('completed_destination_schema')


@pytest.fixture(autouse=True)
def disable_periodic_library_sync():
    """Database reconstruction has no web lifecycle or periodic sync."""


@pytest.fixture
def convert_runtime(completed_destination_schema, monkeypatch):
    """Exercise the real domain plan below the separately tested job protocol.

    Retention owns which captured rows and files reach the runtime database.
    Supply completed catalog subjects below its real registered converters;
    per-batch checkpoints, compaction and installation have their own tests.
    """
    from yt_playlist.core.db_rebuild import files, source
    from yt_playlist.core.schema_inspection import reuse_tables

    admit = source.admit

    def admit_once(conn):
        with reuse_tables(conn):
            return admit(conn)

    monkeypatch.setattr(source, 'admit', admit_once)

    def convert(original, target, *, domain='groups'):
        contract = destination.for_source(original, split=True)
        assert contract.runtime_stages is not None
        prefixes = {
            'groups': ('rec_group_', 'released_group_', 'copy:group_', 'copy:unresolved_group_',
                       'copy:recommendation_polic'),
            'credits': ('copy:artist_credit', 'copy:entry_artist_', 'history:credits'),
        }[domain]
        names = ({'settings', 'rec_proposals', 'copy:generation_groups', 'copy:taste_groups'}
                 if domain == 'groups' else {'history:replay-identities'})
        stages = tuple(stage for stage in contract.stages[:contract.runtime_stages]
                       if stage.name in names or stage.name.startswith(prefixes))
        assert stages
        files.borrow(original, target)
        with closing(sqlite3.connect(original.as_uri()+'?mode=ro', uri=True)) as reader, \
                closing(sqlite3.connect(target.as_uri(), uri=True)) as writer, reuse_tables(writer):
            build._configure(reader)
            build._configure(writer)
            reader.execute('BEGIN')
            writer.execute('ATTACH DATABASE ? AS rebuild_source', (original.as_uri()+'?mode=ro',))
            with writer:
                writer.execute('BEGIN IMMEDIATE')
                contract.initialize(writer)
                # The catalog converter is below retention. Preserve the donor's
                # exact subject IDs without repeating metadata/album producers.
                for table, fields in (
                        ('recordings', 'id,identity_key,title,artist'),
                        ('youtube_items', 'video_id,recording_id,duration_s,available'),
                        ('catalog_entries', 'id,recording_id,video_id')):
                    columns = fields.split(',')
                    writer.executemany('INSERT INTO '+table+'('+fields+') VALUES ('+
                        ','.join('?' for _ in columns)+')', reader.execute('SELECT '+fields+' FROM '+table))
                for stage in stages:
                    assert not stage.repositories and stage.prepare is None, stage.name
                    cursor = None
                    while True:
                        batch = stage.copy(reader, writer, cursor, stage.batch_size)
                        assert isinstance(batch, build.Batch)
                        if batch.done:
                            break
                        assert batch.cursor != cursor, stage.name
                        cursor = batch.cursor
                assert not writer.execute('PRAGMA foreign_key_check').fetchall()
    return convert


def test_rebuild_input_released_schema_and_catalog(production64_template):
    from yt_playlist.core.db_rebuild import source
    assert source.admit(production64_template) == 64
    assert production64_template.execute('PRAGMA query_only').fetchone() == (1,)
    assert production64_template.execute('SELECT id FROM recordings ORDER BY id').fetchall() == [(3,), (8,), (19,)]
    assert not production64_template.execute('PRAGMA foreign_key_check').fetchall()


def test_rebuild_input_development_schema_and_catalog(development93_template):
    from yt_playlist.core.db_rebuild import source
    template, _ = development93_template
    assert source.admit(template) == 93
    assert template.execute('PRAGMA query_only').fetchone()[0] == 1
    assert [row[0] for row in template.execute('SELECT id FROM recordings ORDER BY id')] == [3, 8, 19]
    assert not template.execute('PRAGMA foreign_key_check').fetchall()


def test_rebuild_input_destination_sql_keeps_installed_contract(completed_destination_sql):
    from yt_playlist.core.db_rebuild import installed_format
    with closing(sqlite3.connect(':memory:')) as conn:
        conn.row_factory = sqlite3.Row
        conn.executescript('BEGIN;\n' + ';\n'.join(completed_destination_sql) + ';\nCOMMIT;')
        # Bulk construction deliberately holds secondary indexes and guards
        # aside. Restore them through their real stages before final admission.
        for copy in (destination.indexes, destination.finish):
            cursor = None
            while True:
                batch = copy(conn, conn, cursor, 128)
                if batch.done:
                    break
                cursor = batch.cursor
        installed_format.validate(conn)
        assert not conn.execute('PRAGMA foreign_key_check').fetchall()


def groups(conn, *, released):
    resolve = lambda key: 8 if key=='song 8|artist' else None
    generations = {}
    for serial,name in enumerate(('obsolete','checkpoint','current','building'),1):
        metadata = {'recording_inputs':{'song 8|artist':'a'*64,'missing input':'b'*64}}
        definition = dict(name='Captured '+name,leaf=True,legacy=False,membership_known=True,
            size=1,depth=0,rep_keys=['song 8|artist'],naming_policy='original')
        prepared = dict(generation=name,at=serial,serial=serial,policy='{}',fingerprint='captured',
            payload=json.dumps(metadata),groups=[dict(group_id='leaf',version=name+'-leaf',parent_version=None,
                payload=json.dumps(definition),members=[('song 8|artist',.7,.8)])])
        if released:
            conn.execute('INSERT OR IGNORE INTO rec_group_identities VALUES (?,NULL,1)',('leaf',))
            conn.execute('INSERT INTO rec_group_generations VALUES (?,?,?,?,?,?)',
                         (name,'{}','captured',serial,serial,json.dumps(metadata)))
            conn.execute('INSERT INTO rec_group_definitions VALUES (?,?,?,?,?)',
                         (name+'-leaf',name,'leaf',None,json.dumps(definition)))
            conn.execute('INSERT INTO rec_group_members VALUES (?,?,.7,.8)',(name+'-leaf','song 8|artist'))
            if name!='building':
                conn.execute('INSERT INTO rec_group_publications VALUES (?)',(name,))
            generations[name] = name
        else:
            manifest = group_storage.install_manifest(conn,metadata['recording_inputs'],resolve)
            identity = group_storage.install_header(conn,prepared,{},metadata,manifest)
            group_storage.install_versions(conn,prepared,identity,resolve)
            if name!='building':
                conn.execute('INSERT INTO group_publications VALUES (?,?)',(identity,serial))
            generations[name] = identity
    conn.execute('INSERT INTO '+('rec_group_head' if released else 'group_head')+' VALUES (1,?)',
                 (generations['current'],))
    conn.execute('INSERT INTO settings VALUES (?,?)',('rec_group_shadow_checkpoint',json.dumps(
        {'generation':'checkpoint','serial':2,'at':2})))
    if released:
        conn.execute('INSERT INTO rec_group_builds VALUES (?,?,?,1)',('building','worker',4e9))
        conn.execute('INSERT INTO rec_group_lineage VALUES (?,?,?,?)',
                     ('current-leaf','building-leaf','continuation','{"compatible":true}'))
        conn.execute('INSERT INTO rec_group_graph VALUES (?,?,?,?,?)',
                     ('obsolete','song 8|artist','missing input',.5,.5))
    else:
        conn.execute('INSERT INTO group_builds VALUES (?,?,?,1,NULL,?)',
                     (generations['building'],'worker',4e9,generations['checkpoint']))
        from yt_playlist.core import group_documents
        file = next(group_documents.publish_many(group_documents.directory(conn),[('lineage',{'compatible':True})]))
        versions = dict(conn.execute('SELECT external_version,group_version_id FROM group_version_aliases'))
        conn.execute('INSERT INTO group_lineage VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)',
            ('current-leaf','building-leaf',versions['current-leaf'],versions['building-leaf'],
             'continuation',1,None,None,None,*file))
        conn.execute('INSERT INTO group_generation_lineage VALUES (?,?,?)',
                     (generations['building'],'current-leaf','building-leaf'))
        conn.execute('INSERT INTO group_graph VALUES (?,?,?,?,?)',(generations['obsolete'],3,8,.5,.5))
    return generations


@pytest.mark.parametrize('fixture',['production64','development93'])
def test_runtime_keeps_current_checkpoint_and_unpublished_lineage_without_obsolete_graph(request,fixture,tmp_path,convert_runtime):
    source = request.getfixturevalue(fixture)
    with closing(sqlite3.connect(source)) as conn:
        conn.row_factory = sqlite3.Row
        build._configure(conn)
        with conn:
            conn.execute('BEGIN IMMEDIATE')
            groups(conn,released=fixture=='production64')
    original = source.read_bytes()
    target = tmp_path/'runtime.db'
    convert_runtime(source, target)
    assert source.read_bytes()==original
    with closing(sqlite3.connect(target)) as conn:
        assert {r[0] for r in conn.execute('SELECT external_key FROM group_generations')}=={
            'current','checkpoint','building'}
        assert conn.execute('SELECT g.external_key FROM group_head h JOIN group_generations g ON g.id=h.generation_id').fetchone()==('current',)
        assert conn.execute('SELECT count(*) FROM group_graph').fetchone()==(0,)
        assert conn.execute('SELECT source_alias,target_alias FROM group_generation_lineage').fetchall()==[
            ('current-leaf','building-leaf')]
        assert conn.execute('SELECT g.external_key FROM group_preparation_state s JOIN group_generations g '
                            'ON g.id=s.generation_id').fetchone()==('checkpoint',)
        assert conn.execute('SELECT count(*) FROM unresolved_group_manifest_inputs').fetchone()[0]>0
        assert not conn.execute('PRAGMA foreign_key_check').fetchall()


def test_released_runtime_retains_compressed_comparison_body_context(production64,tmp_path,convert_runtime):
    import base64
    import zlib
    from yt_playlist.core import model_projections
    from yt_playlist.core.evidence_ledger import canonical
    body = {'context':{'group_generation':'obsolete','groups':[{'version':'obsolete-leaf'}]},'protos':[]}
    raw = canonical(body)
    nodes = ['obsolete', 'obsolete-leaf', {'version': 1}, [2],
             {'group_generation': 0, 'groups': 3}, [], {'context': 4, 'protos': 5}]
    envelopes = []
    for role, codec in [('incumbent', 'json-zlib-v1'), ('challenger', 'json-shared-zlib-v2')]:
        packed = canonical({'nodes': nodes, 'root': 6}) if codec=='json-shared-zlib-v2' else raw
        envelope = {'run_id':1,'codec':codec,'expanded_bytes':len(raw),'packed_bytes':len(packed),
            'context':{'group_generation':'stale-envelope-header'},
            'data':base64.b64encode(zlib.compress(packed)).decode('ascii')}
        envelopes.append(('model_pair:1:'+role+':mixes', json.dumps(envelope), 1))
    with closing(sqlite3.connect(production64)) as conn:
        conn.row_factory = sqlite3.Row
        build._configure(conn)
        with conn:
            conn.execute('BEGIN IMMEDIATE')
            groups(conn,released=True)
            conn.executemany('INSERT INTO rec_proposals VALUES (?,?,?)',envelopes)
    target = tmp_path/'runtime.db'
    convert_runtime(production64, target)
    with closing(sqlite3.connect(target)) as conn:
        conn.row_factory = sqlite3.Row
        for surface, _, _ in envelopes:
            assert model_projections.read(conn,surface)=={'run_id':1,'data':body}, surface
        assert conn.execute("SELECT 1 FROM group_generations WHERE external_key='obsolete'").fetchone()
        assert not conn.execute("SELECT 1 FROM unresolved_group_evidence WHERE raw_key='obsolete'").fetchone()
        assert not conn.execute('PRAGMA foreign_key_check').fetchall()


def test_released_nonobject_group_preview_has_no_context(production64):
    from yt_playlist.core.db_rebuild import released_groups
    with closing(sqlite3.connect(production64)) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute("INSERT INTO rec_proposals VALUES ('adaptive_group_preview','[]',1)")
        released_groups.retained(conn)
        assert not conn.execute('SELECT 1 FROM runtime_released_generations').fetchone()




def test_unknown_time_captures_and_known_delivery_receipts_survive_without_raw_metadata(development93,tmp_path,convert_runtime):
    from yt_playlist.core import artist_credits
    with closing(sqlite3.connect(development93)) as conn:
        conn.row_factory = sqlite3.Row
        build._configure(conn)
        with conn:
            conn.execute('BEGIN IMMEDIATE')
            version = artist_credits.intern(conn,artist_credits.prepare(conn,'Captured',
                [{'name':'Captured','id':'captured-artist'}],'provider'))
            for identity,kind,seen in ((1,'capture',None),(2,'provider_delivery',None),
                                        (3,'legacy_capture',None),(4,'capture',1.),(5,'provider_delivery',1.)):
                conn.execute('INSERT INTO artist_credit_observations('
                    'id,entry_id,credit_id,source,first_seen,last_seen,processing_count,capture_kind,delivery_key) '
                    'VALUES (?,108,?,\'provider\',?,?,1,?,?)',
                    (identity,version,seen,seen,kind,'delivery-'+str(identity) if kind=='provider_delivery' else None))
            conn.execute('INSERT INTO artist_credit_capture_heads VALUES (108,\'provider\',4)')
    target = tmp_path/'runtime.db'
    convert_runtime(development93, target, domain='credits')
    with closing(sqlite3.connect(target)) as conn:
        assert {r[0] for r in conn.execute('SELECT id FROM artist_credit_observations')}=={1,2,4}
        assert conn.execute('SELECT count(*) FROM artist_credit_replays').fetchone()==(2,)
        assert not conn.execute('SELECT 1 FROM metadata_deliveries').fetchone()
        assert not conn.execute('PRAGMA foreign_key_check').fetchall()




def _workspace_contents(root):
    return {str(path.relative_to(root)):
            ('link',str(path.readlink())) if path.is_symlink() else
            ('file',hashlib.sha256(path.read_bytes()).hexdigest())
            for path in root.rglob('*') if path.is_symlink() or path.is_file()}


def _clone_captured_rebuild(source,target,name='development93.db'):
    from yt_playlist.core import db_handles
    from yt_playlist.core.db_rebuild import install
    # Include every owner, retained original and intermediate rebuild artifact.
    # Preserve links while copying, then confine them to this consumer's tree.
    shutil.copytree(source,target,symlinks=True)
    for path in target.rglob('*'):
        if path.is_symlink():
            destination = target/path.readlink().relative_to(source)
            path.unlink()
            path.symlink_to(destination,target_is_directory=True)
    def relocate(value):
        if isinstance(value,dict):
            return {key:relocate(item) for key,item in value.items()}
        if isinstance(value,list):
            return [relocate(item) for item in value]
        if isinstance(value,str) and value.startswith(str(source)+'/'):
            return str(target/Path(value).relative_to(source))
        return value
    path = target/name
    state = relocate(db_handles.state(source/name))
    assert install.digest(state['original'])==state['original_sha256']
    with closing(sqlite3.connect(path)) as conn, conn:
        pending = build._checkpoint(conn)
        # Relocate custody only; preserve the genuine captured evidence and work.
        pending['source']['path'] = relocate(pending['source']['path'])
        build._checkpoint(conn,pending)
    # These bounded historical inputs have no SQL-stored absolute file locators.
    # Fail explicitly if that changes instead of silently borrowing template files.
    for database in (path,Path(state['original'])):
        with closing(sqlite3.connect(database.as_uri()+'?mode=ro',uri=True)) as conn:
            for table, in conn.execute("SELECT name FROM sqlite_schema WHERE type='table'"):
                for column in conn.execute('PRAGMA table_info('+build.quote(table)+')'):
                    if column[1]=='filename' or column[1].endswith('_filename'):
                        assert not any(Path(row[0]).is_absolute() for row in conn.execute(
                            'SELECT '+build.quote(column[1])+' FROM '+build.quote(table)+
                            ' WHERE '+build.quote(column[1])+' IS NOT NULL'))
    install.save(path,state)
    return path




@pytest.fixture(scope='module')
def legacy_repair_contracts(startup_destination):
    """Completed domain output below the genuine old-plan repair protocol."""
    from yt_playlist.core import metadata_runtime
    from yt_playlist.rec.choice_evidence import STORAGE_PENDING

    def initialize(conn):
        startup_destination.initialize(conn)
        conn.execute('INSERT INTO settings VALUES (?,?)', (STORAGE_PENDING, '1'))
        conn.execute("INSERT INTO provider_attempts(id,recording_id,input_video_id,provider,at,status) "
                     "VALUES(1,8,'video-8','fixture',1000,'ok')")
        metadata_runtime.activate(conn)

    current = replace(startup_destination, initialize=initialize,
                      version='controlled-current-repair-v1')
    legacy = replace(current, version='controlled-legacy-repair-v1', stages=(
        current.stages[0], replace(current.stages[1], name='legacy-analytics-input')))
    return current, legacy


@pytest.fixture
def legacy_repair_converter(monkeypatch, legacy_repair_contracts):
    monkeypatch.setattr(destination, 'for_source',
                        lambda *args, legacy=False, **kwargs: legacy_repair_contracts[int(legacy)])


@pytest.fixture(scope='module')
def legacy_runtime_template(tmp_path_factory,production64_template,legacy_repair_contracts):
    from yt_playlist.core.db_rebuild import job
    from yt_playlist.core.store import Store

    root = tmp_path_factory.mktemp('legacy-runtime')
    with pytest.MonkeyPatch.context() as patch:
        patch.setenv('YT_PLAYLIST_HOME',str(root/'app-home'))
        patch.setenv('YT_PLAYLIST_AUDIO_VIBES','0')
        patch.setenv('YT_PLAYLIST_AUDIO_VIBE_MODEL','')
        patch.delenv('DEEPSEEK_API_KEY',raising=False)
        path = inspect.unwrap(production64)(root,production64_template)
        store = Store(path)
        try:
            store.init_schema()
            first = job.run(store,destination=legacy_repair_contracts[1])
            assert first['phase']=='complete' and first['analytics_pending']
        finally:
            store.conn.close()
    return root


def test_rebuild_input_legacy_runtime_keeps_original_and_pending_old_plan(
        legacy_runtime_template, legacy_repair_contracts):
    from yt_playlist.core import db_handles, metadata_runtime
    from yt_playlist.core.db_rebuild import install
    path = legacy_runtime_template/'production64.db'
    state = db_handles.state(path)
    assert state['phase'] == 'complete' and state['analytics_pending']
    assert install.digest(state['original']) == state['original_sha256']
    with closing(sqlite3.connect(path.as_uri()+'?mode=ro', uri=True)) as conn:
        pending = build._checkpoint(conn)
        assert pending['stages'] == [stage.name for stage in legacy_repair_contracts[1].stages]
        assert pending['stage'] == 1
        assert metadata_runtime.active(conn)
        assert conn.execute('SELECT recording_id,provider FROM provider_attempts').fetchall() == [(8, 'fixture')]
        assert not conn.execute('PRAGMA foreign_key_check').fetchall()


@pytest.fixture
def legacy_runtime(legacy_runtime_template,tmp_path,legacy_repair_converter):
    from yt_playlist.core import db_handles

    original = _workspace_contents(legacy_runtime_template)
    try:
        path = _clone_captured_rebuild(legacy_runtime_template,tmp_path/'legacy-runtime','production64.db')
        yield path,db_handles.state(path)
    finally:
        assert _workspace_contents(legacy_runtime_template)==original,'Legacy runtime template was modified'


@pytest.mark.parametrize('interrupt',[False,True])
def test_existing_large_runtime_finishes_original_checkpoint_and_repairs_current_database(legacy_runtime,interrupt):
    from yt_playlist.core import db_handles, metadata_retention
    from yt_playlist.core.db_rebuild import job
    from yt_playlist.core.storage_maintenance import StorageMaintenance
    from yt_playlist.core.store import Store
    production64,first = legacy_runtime
    store = Store(production64)
    maintenance = StorageMaintenance(store)
    try:
        store.init_schema()
        with pytest.raises(RuntimeError,match='Finish library analytics preparation'):
            job.run(store)
        assert db_handles.state(production64)['id']==first['id']
        store.settings.set_setting('accepted-after-old-runtime','keep')
        def progress(phase,detail):
            if detail['stage']=='finalize:copy':
                assert maintenance.status()['active']
                assert not maintenance.status()['recording']
            if interrupt and detail['stage']=='finalize:copy':
                raise InterruptedError('repair can restart')
        if interrupt:
            with pytest.raises(InterruptedError,match='repair can restart'):
                job.finish_analytics(store,progress=progress)
            assert db_handles.state(production64)['analytics_pending']
            assert not store.activity_paused
            store.settings.set_setting('accepted-after-repair-failure','also keep')
            store.conn.close()
            store = Store(production64)
            store.init_schema()
            maintenance = StorageMaintenance(store)
            try:
                maintenance.start()
                maintenance.join(30)
                assert maintenance.phase=='done',maintenance.error
                assert maintenance.ready.is_set()
                final = maintenance.report
            finally:
                maintenance.stop()
                maintenance.join(30)
            assert store.settings.get_setting('accepted-after-repair-failure')=='also keep'
        else:
            final = job.finish_analytics(store,progress=progress)
        assert final['id']==first['id'] and not final['analytics_pending']
        assert final['finalization']['original']!=first['original']
        assert store.settings.get_setting('accepted-after-old-runtime')=='keep'
        for table in metadata_retention.RETIRED:
            assert not store.conn.execute('SELECT 1 FROM '+build.quote(table)+' LIMIT 1').fetchone()
        assert not store.conn.execute('PRAGMA foreign_key_check').fetchall()
    finally:
        store.conn.close()


@pytest.mark.parametrize('phase',['closing','original_moved','installed','reopening','complete'])
def test_old_runtime_repair_recovers_crash_before_publishing_completion(legacy_runtime,monkeypatch,phase):
    from yt_playlist.core import db_handles
    from yt_playlist.core.db_rebuild import job
    from yt_playlist.core.storage_maintenance import StorageMaintenance
    from yt_playlist.core.store import Store
    class ProcessDied(BaseException):
        pass
    production64,first = legacy_runtime
    store = Store(production64)
    try:
        store.init_schema()
        store.settings.set_setting('accepted-before-repair-crash','keep')
        def during_close():
            raise ProcessDied()
        def progress(current,detail):
            if detail['stage']=='finalize:'+phase:
                if phase=='closing':
                    store.conn._before_close_callbacks.append(during_close)
                else:
                    raise ProcessDied()
        with monkeypatch.context() as patch:
            if phase=='complete':
                def before_publication(*args,**kwargs):
                    assert db_handles.state(production64)['phase']=='complete'
                    assert db_handles.state(production64)['analytics_pending']
                    raise ProcessDied()
                patch.setattr(job,'_completed_repair',before_publication)
            with pytest.raises(ProcessDied):
                job.finish_analytics(store,progress=progress)
    finally:
        store.conn.close()
    recovered = Store(production64)
    maintenance = StorageMaintenance(recovered)
    try:
        recovered.init_schema()
        assert db_handles.state(production64)['analytics_pending']
        assert recovered.settings.get_setting('accepted-before-repair-crash')=='keep'
        recovered.settings.set_setting('accepted-after-repair-crash','also keep')
        maintenance.start()
        maintenance.join(30)
        assert maintenance.phase=='done',maintenance.error
        assert maintenance.ready.is_set()
        assert maintenance.report['id']==first['id']
        assert not db_handles.state(production64)['analytics_pending']
        assert recovered.settings.get_setting('accepted-before-repair-crash')=='keep'
        assert recovered.settings.get_setting('accepted-after-repair-crash')=='also keep'
        assert not recovered.conn.execute('PRAGMA foreign_key_check').fetchall()
    finally:
        maintenance.stop()
        maintenance.join(30)
        recovered.conn.close()
