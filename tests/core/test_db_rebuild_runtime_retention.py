"""Runtime admission excludes history while retaining independent live owners."""
from contextlib import closing
from copy import deepcopy
import hashlib
import inspect
import json
from pathlib import Path
import shutil
import sqlite3

import pytest

from tests.core.test_db_rebuild_destination import production64, development93
from yt_playlist.core import group_storage
from yt_playlist.core.db_rebuild import build, destination


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
def test_runtime_keeps_current_checkpoint_and_unpublished_lineage_without_obsolete_graph(request,fixture,tmp_path):
    source = request.getfixturevalue(fixture)
    with closing(sqlite3.connect(source)) as conn:
        conn.row_factory = sqlite3.Row
        build._configure(conn)
        with conn:
            conn.execute('BEGIN IMMEDIATE')
            groups(conn,released=fixture=='production64')
    original = source.read_bytes()
    target = tmp_path/'runtime.db'
    build.construct(source,target,destination.for_source(source,split=True),runtime_only=True)
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


def test_released_runtime_retains_compressed_comparison_body_context(production64,tmp_path):
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
    build.construct(production64,target,destination.for_source(production64,split=True),runtime_only=True)
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


@pytest.mark.parametrize('fixture',['production64','development93'])
@pytest.mark.parametrize('failure',['staging','reopening'])
def test_failed_fresh_install_retries_original_instead_of_blocking_on_uninstalled_analytics(request,fixture,failure):
    from yt_playlist.core import db_handles
    from yt_playlist.core.db_rebuild import job
    from yt_playlist.core.storage_maintenance import StorageMaintenance
    from yt_playlist.core.store import Store
    path = request.getfixturevalue(fixture)
    store = Store(path)
    maintenance = StorageMaintenance(store)
    try:
        store.init_schema()
        def interrupt(phase,detail):
            if phase==failure:
                raise InterruptedError('runtime was never admitted')
        with pytest.raises(InterruptedError,match='runtime was never admitted'):
            job.run(store,rebuild=True,progress=interrupt)
        failed = db_handles.state(path)
        assert failed['phase']=='failed' and failed['analytics_pending']
        assert not Path(failed['original']).exists()
        assert store.conn.execute('SELECT max(version) FROM schema_migrations').fetchone()[0] in (64,93)
        maintenance.start()
        maintenance.join(30)
        assert maintenance.phase=='done',maintenance.error
        assert maintenance.ready.is_set()
        assert not db_handles.state(path)['analytics_pending']
        store.settings.set_setting('accepted-after-staging-retry','keep')
        assert store.settings.get_setting('accepted-after-staging-retry')=='keep'
    finally:
        maintenance.stop()
        maintenance.join(30)
        store.conn.close()


def test_unknown_time_captures_and_known_delivery_receipts_survive_without_raw_metadata(development93,tmp_path):
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
    build.construct(development93,target,destination.for_source(development93,split=True),runtime_only=True)
    with closing(sqlite3.connect(target)) as conn:
        assert {r[0] for r in conn.execute('SELECT id FROM artist_credit_observations')}=={1,2,4}
        assert conn.execute('SELECT count(*) FROM artist_credit_replays').fetchone()==(2,)
        assert not conn.execute('SELECT 1 FROM metadata_deliveries').fetchone()
        assert not conn.execute('PRAGMA foreign_key_check').fetchall()


def _capture_choice_rebuild(path,new_format):
    from yt_playlist.core import evidence_ledger, fao, mix_choices
    from yt_playlist.core.db_rebuild import job
    from yt_playlist.core.store import Store
    with closing(sqlite3.connect(path)) as conn:
        conn.row_factory = sqlite3.Row
        mix_choices.install(conn)
        ledger = evidence_ledger.EvidenceLedger(evidence_ledger.directory(conn),2**63-1)
        context = ledger.artifact({'at':99,'group_generation':'retired','groups':[]})
        record = dict(schema=1,id='captured-menu',format='mixes',at=100,observed_at=100,
            episode_id='old-episode',context=context,prediction=None,
            options=[dict(id='old-option',label='Original label',genres=['house'])])
        if new_format:
            compact = ledger.artifact(mix_choices.context({'at':99},mixes=True))
            record = mix_choices.publish_menu(conn,record,compact)
        ledger.put('opportunity',record['id'],record)
        conn.commit()
        expected = {str(file.relative_to(ledger.directory)):list(fao._hash(file))
                    for kind in ('artifact','opportunity')
                    for file in (ledger.directory/kind).glob('*/*.json')}
        assert {'artifact','opportunity'}=={Path(name).parts[0] for name in expected}
    store = Store(path)
    try:
        store.init_schema()
        report = job.run(store,rebuild=True)
        pending = build._checkpoint(store.conn)
        assert report['phase']=='complete' and report['analytics_pending']
        assert 0<pending['stage']<len(pending['stages'])
        assert dict(pending['evidence'])==expected
    finally:
        # Flush WAL before copying any database or retained original.
        store.conn.close()
    return record


@pytest.fixture(scope='module')
def captured_choice_templates(tmp_path_factory,development93_template):
    templates = {}
    def get(new_format):
        if new_format not in templates:
            root = tmp_path_factory.mktemp('captured-choices-'+str(new_format))
            with pytest.MonkeyPatch.context() as patch:
                patch.setenv('YT_PLAYLIST_HOME',str(root/'app-home'))
                patch.setenv('YT_PLAYLIST_AUDIO_VIBES','0')
                patch.setenv('YT_PLAYLIST_AUDIO_VIBE_MODEL','')
                patch.delenv('DEEPSEEK_API_KEY',raising=False)
                # Clone the genuinely populated historical prefix without
                # coupling this module owner to function-fixture signatures.
                template, _seconds = development93_template
                path = root/'development93.db'
                with closing(sqlite3.connect(path)) as conn:
                    template.backup(conn)
                record = _capture_choice_rebuild(path,new_format)
            templates[new_format] = root,record
        return templates[new_format]
    return get


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


@pytest.fixture
def captured_choices(captured_choice_templates,new_format,tmp_path):
    source,record = captured_choice_templates(new_format)
    original = _workspace_contents(source)
    try:
        yield _clone_captured_rebuild(source,tmp_path/'captured'),deepcopy(record)
    finally:
        assert _workspace_contents(source)==original,'Captured rebuild template was modified'


@pytest.mark.parametrize('new_format',[False,True])
@pytest.mark.parametrize('legacy,damage',[(False,None),(True,None),(False,'descriptor'),(False,'file')])
def test_analytics_verifies_captured_choices_without_rejecting_new_publications(captured_choices,new_format,legacy,damage):
    from yt_playlist.core import db_handles, evidence_ledger, fao, home_outputs, mix_choices
    from yt_playlist.core.db_rebuild import job
    from yt_playlist.core.store import Store
    from yt_playlist.rec import choice_evidence
    development93,record = captured_choices
    store = Store(development93)
    try:
        store.init_schema()
        if legacy:
            with store.conn:
                pending = build._checkpoint(store.conn)
                pending.pop('evidence')
                build._checkpoint(store.conn,pending)
        captured = build._checkpoint(store.conn).get('evidence')
        published = False
        def progress(phase,detail):
            nonlocal published
            if published or detail['stage']!='mixes:roots':
                return
            published = True
            display = home_outputs.prepare('home_choice',json.dumps(
                {'schema':1,'protos':[{'label':'New accepted card'}]}),200)
            with store.conn:
                store.conn.execute('BEGIN IMMEDIATE')
                home_outputs.publish(store.conn,display,store=store,input_revision=200)
            assert choice_evidence.issue(store,'mixes',
                [dict(id='new-option',label='New descriptor',genres=['jazz'])],201,
                context={'at':200},opportunity_id='new-menu')=='new-menu'
            assert choice_evidence.respond(store,'new-menu','choice',202,
                option_id='new-option',action_id='accepted-choice')
            assert choice_evidence.issue(store,'albums',
                [dict(id='new-album',label='Accepted album')],203,
                context={'at':200},opportunity_id='new-album-menu')=='new-album-menu'
            if damage=='descriptor':
                with store.conn:
                    for name, in store.conn.execute("SELECT name FROM sqlite_schema "
                            "WHERE type='trigger' AND tbl_name='mix_choice_themes'").fetchall():
                        store.conn.execute('DROP TRIGGER '+build.quote(name))
                    store.conn.execute("UPDATE mix_choice_themes SET label='Corrupted label' "
                        "WHERE id=(SELECT d.theme_id FROM mix_choice_options o "
                        "JOIN mix_choice_descriptors d ON d.id=o.descriptor_id WHERE o.opportunity_id='captured-menu')")
            elif damage=='file':
                # Appendable roots are shared with the retained original.
                # A comparison of their current hashes would miss this loss.
                path = store.rec_ledger.evidence()._path('opportunity','captured-menu')
                fao.write_bytes(path,evidence_ledger.canonical({**record,'episode_id':'corrupted'}))
            else:
                raise InterruptedError('restart with accepted choices and captured inventory')
        if damage:
            with pytest.raises(ValueError,match='original listening evidence|displayed mix meaning'):
                job.finish_analytics(store,progress=progress)
            assert db_handles.state(development93)['analytics_pending']
        else:
            with pytest.raises(InterruptedError,match='restart with accepted choices'):
                job.finish_analytics(store,progress=progress)
            assert build._checkpoint(store.conn).get('evidence')==captured
            store.conn.close()
            store = Store(development93)
            store.init_schema()
            assert build._checkpoint(store.conn).get('evidence')==captured
            job.finish_analytics(store)
            store.conn.close()
            store = Store(development93)
            store.init_schema()
            assert not db_handles.state(development93)['analytics_pending']
            assert store.rec_proposals.get_proposals('home_choice')['protos'][0]['label']=='New accepted card'
            offered = store.rec_ledger.evidence().get('opportunity','new-menu')
            assert mix_choices.expand(store.conn,offered['options'][0])['label']=='New descriptor'
            assert store.rec_ledger.evidence().get('response','accepted-choice')['option_id']=='new-option'
            assert store.rec_ledger.evidence().get('opportunity','new-album-menu')
            assert not store.conn.execute('PRAGMA foreign_key_check').fetchall()
        assert published
    finally:
        store.conn.close()


@pytest.fixture(scope='module')
def legacy_runtime_template(tmp_path_factory,production64_template):
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
            first = job.run(store,destination=destination.for_source(path,split=True,legacy=True))
            assert first['phase']=='complete' and first['analytics_pending']
        finally:
            store.conn.close()
    return root


@pytest.fixture
def legacy_runtime(legacy_runtime_template,tmp_path):
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
