"""Direct source imports use a fresh schema and preserve installed evidence."""
from contextlib import closing
from dataclasses import replace
from functools import wraps
import hashlib
import json
from pathlib import Path
import sqlite3
import struct
import time

import pytest
from tests.metadata_provenance_support import metadata_provenance

from tests.db_rebuild_support import (admitted_rebuild_format, completed_destination_schema, completed_destination_sql,
                                      production64)
from tests.rebuild_startup_support import startup_destination
from yt_playlist.core.db_rebuild import build, destination, source
from yt_playlist.core import vibe_versions
from yt_playlist.core.migrations import registry, regular, runner
from yt_playlist.core.store import Store


@pytest.fixture(scope='module')
def installation_destination(startup_destination):
    """Completed catalog conversion below installation and feedback replay.

    Registered populated-domain conversion has independent real owners in
    test_db_rebuild_populated. Keep this contract's actual source-history copy,
    retained journal heads/files, checkpoint, compaction and installer.
    """
    def journals(reader, target, cursor, limit):
        for table in ('legacy_archive_heads', 'legacy_archive_cutovers'):
            if reader.execute('SELECT 1 FROM sqlite_schema WHERE name=?', (table,)).fetchone():
                fields = source.columns(reader, table)
                target.executemany('INSERT INTO '+table+'('+','.join(fields)+') VALUES ('+
                    ','.join('?' for _ in fields)+')', reader.execute('SELECT '+','.join(fields)+' FROM '+table))
        return build.Batch(done=True)

    return replace(startup_destination, version='completed-catalog-installation-v1',
        stages=(startup_destination.stages[0], build.Stage('retained-journals', journals)),
        runtime_stages=None, compact=False)


def test_installation_destination_keeps_private_current_schema_and_catalog(installation_destination):
    with closing(sqlite3.connect(':memory:')) as conn, conn:
        installation_destination.initialize(conn)
        assert conn.execute('SELECT id FROM recordings ORDER BY id').fetchall() == [(3,), (8,), (19,)]
        assert not conn.execute('PRAGMA foreign_key_check').fetchone()


@pytest.fixture
def development93(development93_template, tmp_path, record_property):
    # The numbered definition supplies this explicit second source. Replaying
    # every earlier bulk conversion would conceal a dependency on that chain.
    template, seconds = development93_template
    path = tmp_path / 'development93.db'
    started = time.perf_counter()
    with closing(sqlite3.connect(path)) as conn:
        template.backup(conn)
    record_property('development93_prefix_seconds', seconds)
    record_property('development93_clone_seconds', time.perf_counter() - started)
    return path


@pytest.fixture(scope='module')
def development_admission(development93_template, tmp_path_factory):
    path = tmp_path_factory.mktemp('admitted-development93') / 'source.db'
    source = Store(path)
    with closing(source.conn):
        development93_template[0].backup(source.conn)
        yield source


def test_development_admission_input_keeps_the_installed_catalog(development_admission):
    conn = development_admission.conn
    assert conn.execute('SELECT max(version) FROM schema_migrations').fetchone()[0] == 93
    assert [row[0] for row in conn.execute('SELECT id FROM recordings ORDER BY id')] == [3, 8, 19]
    assert not conn.execute('PRAGMA foreign_key_check').fetchone()


@pytest.mark.parametrize('version', range(1, len(regular.CHANGES) + 1))
def test_each_real_development_admission_owner_keeps_installed_history(development_admission, version):
    conn = development_admission.conn
    regular.migrate(conn, changes=regular.CHANGES[:version])
    assert regular.validate(conn) == version
    assert conn.execute('SELECT max(version) FROM schema_migrations').fetchone()[0] == 93
    assert not conn.execute('PRAGMA foreign_key_check').fetchone()


@pytest.fixture(scope='module')
def admitted_development93_template(development_admission):
    from tests.prepared_fixture_support import capture_template

    development_admission.init_schema()
    return capture_template(development_admission, None)


def test_admitted_development_input_keeps_real_regular_history_and_private_guards(
        admitted_development93_template):
    from yt_playlist.core.migrations import regular

    conn = admitted_development93_template.source.conn
    assert conn.execute('SELECT max(version) FROM schema_migrations').fetchone()[0] == 93
    assert regular.validate(conn) == len(regular.CHANGES)
    assert conn.execute('PRAGMA query_only').fetchone()[0] == 1
    assert admitted_development93_template.revision_guards
    assert not conn.execute('PRAGMA foreign_key_check').fetchone()


@pytest.fixture
def admitted_development93(admitted_development93_template, tmp_path):
    path = tmp_path / 'development93.db'
    with admitted_development93_template.open(path):
        return path


@pytest.fixture(autouse=True)
def destination_phase_costs(monkeypatch, record_property, completed_destination_schema):
    """Keep actual conversions and verifiers; report inclusive phase costs."""
    costs = {}

    def timed(name, operation):
        @wraps(operation)
        def call(*args, **kwargs):
            wall, cpu = time.perf_counter(), time.thread_time()
            try:
                return operation(*args, **kwargs)
            finally:
                value = costs.setdefault(name, [0, 0., 0.])
                value[0] += 1
                value[1] += time.perf_counter() - wall
                value[2] += time.thread_time() - cpu
        return call

    select = destination.for_source

    @wraps(select)
    def contract(*args, **kwargs):
        result = timed('definition', select)(*args, **kwargs)
        return replace(result, initialize=timed('schema', result.initialize),
                       verify=timed('verification', result.verify),
                       stages=tuple(replace(stage, copy=timed('stage:' + stage.name, stage.copy))
                                    for stage in result.stages))

    monkeypatch.setattr(destination, 'for_source', contract)
    for name in ('construct', 'build'):
        monkeypatch.setattr(build, name, timed(name, getattr(build, name)))
    try:
        yield
    finally:
        record_property('destination_inclusive_phase_costs', json.dumps(costs, sort_keys=True))


def test_release_admission_performs_no_writes(production64):
    with closing(sqlite3.connect(production64.as_uri()+'?mode=ro',uri=True)) as conn:
        conn.execute('PRAGMA query_only=ON')
        assert source.admit(conn)==64
        assert conn.total_changes==0
        conn.execute('BEGIN')
        with pytest.raises(sqlite3.OperationalError):
            conn.execute('DELETE FROM schema_migrations')


def seed_versioned_scores(conn):
    conn.execute('INSERT INTO measurements(id,recording_id,vibe,score,support,source,model_version,input_digest,measured_at) '
                 "VALUES (401,8,'relax',.6,.9,'provider_measurement','exact-model-version','original-inputs',17)")
    conn.execute('INSERT INTO recording_vibes(recording_id,vibe,score,support,source,measurement_id,inputs_version) '
                 "VALUES (8,'relax',.6,.9,'provider_measurement',401,'exact-current-version')")


def assert_versioned_scores(conn):
    assert tuple(conn.execute('SELECT m.score,m.support,v.value FROM measurements m '
        'JOIN vibe_versions v ON v.id=m.model_version_id WHERE m.id=401').fetchone())==(.6,.9,'exact-model-version')
    assert conn.execute('SELECT d.value FROM recording_vibes v JOIN vibe_versions d '
        "ON d.id=v.inputs_version_id WHERE v.recording_id=8 AND v.vibe='relax'").fetchone()[0]=='exact-current-version'


@pytest.mark.parametrize('source_fixture', ['production64', 'development93'])
@pytest.mark.parametrize('progress', [None, {}, {'phase': 'deferred', 'at': 17.25, 'reason': 'work-budget'}])
def test_rebuild_verifies_legacy_progress_in_an_empty_additive_owner(request,source_fixture,progress,tmp_path):
    from yt_playlist.core import group_progress
    from yt_playlist.core.db_rebuild import verification

    path = request.getfixturevalue(source_fixture)
    with closing(sqlite3.connect(path)) as conn:
        if source_fixture == 'development93':
            group_progress.install(conn)
            assert not conn.execute('SELECT 1 FROM group_preparation_progress').fetchall()
        if progress is not None:
            conn.execute('INSERT INTO settings VALUES (?,?)', ('rec_group_shadow_progress', json.dumps(progress)))
        conn.commit()
    original = path.read_bytes()
    contract = destination.for_source(path)
    # All three progress states own the registered settings conversion and its
    # verifier. Whole catalog imports and compaction have independent owners;
    # repeating them adds no progress-state behavior here.
    settings = tuple(stage for stage in contract.stages if stage.name == 'settings')
    assert len(settings) == 1
    contract = replace(contract, stages=settings, verify=verification._group_progress, compact=False)
    output = tmp_path/'progress.db'
    report = build.construct(path,output,contract)
    assert path.read_bytes() == original
    assert set(report['rows']) == {'settings'}
    with closing(sqlite3.connect(path)) as reader, closing(sqlite3.connect(output)) as target:
        actual = list(target.execute('SELECT singleton,phase,reported_at,reason FROM group_preparation_progress'))
        expected = [] if progress is None else [(1,progress.get('phase'),progress.get('at'),progress.get('reason'))]
        assert actual == expected
        verification._group_progress(reader,target)
        target.execute('DELETE FROM group_preparation_progress')
        if progress is None:
            target.execute("INSERT INTO group_preparation_progress VALUES (1,'ready',1,'invented')")
        with pytest.raises(ValueError,match='changed captured grouping progress'):
            verification._group_progress(reader,target)


def test_import_refuses_an_unclassified_owner(development93):
    with closing(sqlite3.connect(development93)) as conn:
        conn.execute('CREATE TABLE unique_paid_result(id INTEGER PRIMARY KEY,body BLOB)')
        conn.commit()
    with pytest.raises(ValueError,match='Unclassified source owners: unique_paid_result'):
        destination.for_source(development93)


def seed_development_metadata(path):
    import time
    from yt_playlist.core import artist_credits, artists, catalog, credit_retention
    from yt_playlist.core import metadata, metadata_attempts, metadata_legacy, metadata_native
    from yt_playlist.core import metadata_runtime, metadata_runtime_files, served_facts
    from yt_playlist.core.metadata_observations import MetadataObservation

    at = time.time()
    fields = {'genre': 'Jazz', 'bpm': 120}
    trace = {'policy_kind': 'prepared', 'ranker': 'captured', 'candidate': {
        'video_id': 'video-8', 'genre': 'Jazz', 'coverage': {'genre': True}, 'last_played': None}}
    with closing(sqlite3.connect(path)) as conn:
        conn.row_factory = sqlite3.Row
        build._configure(conn)
        conn.execute('BEGIN IMMEDIATE')
        seed_versioned_scores(conn)
        delivery, _ = metadata_attempts.capture_result(conn,108,'provider',fields,run_id='captured',now=at)
        artists.observe(conn, 108, 'Supplied artist', [{'name': 'Supplied artist', 'id': 'supplied'}],
                        source='provider', now=at, metadata_delivery_id=delivery)
        metadata.apply_provider(conn,108,'provider',fields,('provider',),delivery_id=delivery,now=at)
        metadata_attempts.record(conn,108,'provider',fields,cycle='original',delivery_id=delivery,now=at)
        catalog.choose_field(conn,108,'bpm',None,now=at+1)
        catalog.choose_field(conn,108,'bpm',130,now=at+2)
        metadata_legacy.copy_observations(conn,'metadata_decisions',[{
            '_legacy_id': 44, 'entry_id': 108, 'field': 'bpm', 'value_json': 'null',
            'source': 'unknown-clock', 'chosen_at': None}])
        metadata_native.record(conn,108,'native',[MetadataObservation('mood_party',.8,'recording',
            'native-id','prediction',unit='classifier_probability')],run_id='native-run',now=at,recording_id=8)
        conn.execute('INSERT INTO rec_served_impressions(id,request_id,surface,lane,identity_key,'
            'rank,model_version,provenance,served_at) VALUES (503,\'captured\',\'home\',\'\','
            '\'song 8|artist\',1,\'missing-policy\',?,?)',(json.dumps(trace),at))
        native_key = conn.execute('SELECT source_key FROM rec_musical_sources').fetchone()[0]
        expected_native = metadata_native.source_values(conn,native_key)
        conn.commit()
    def verify(store, *, compacted=True):
        assert_versioned_scores(store.conn)
        assert metadata_runtime.active(store.conn) and served_facts.active(store.conn)
        from yt_playlist.core.metadata_retention import RETIRED
        if compacted:
            assert all(store.conn.execute('SELECT count(*) FROM '+table).fetchone()[0] == 0 for table in RETIRED)
            assert store.conn.execute('SELECT provenance FROM rec_served_impressions WHERE id=503').fetchone()[0] == '{}'
        receipt = store.conn.execute('SELECT * FROM artist_credit_replays WHERE delivery_key=?',
                                     ('metadata-delivery:'+str(delivery),)).fetchone()
        assert receipt['metadata_bound'] == 1
        assert store.conn.execute('SELECT bpm FROM catalog_tracks WHERE id=108').fetchone()[0] == 130
        decisions = metadata_provenance(store, 108)['decisions']
        assert len(decisions) == 3
        assert any(row['source']=='unknown-clock' and row['chosen_at'] is None
                   and row['value_json']=='null' for row in decisions)
        assert metadata_native.source_values(store.conn,native_key) == expected_native
        assert store.enrichment_state.enrichment_provider_state(8,include_previous=True)['provider']['fields'] == fields
        row = store.conn.execute('SELECT i.id,i.served_at,f.* FROM rec_served_impressions i '
            'JOIN served_facts f ON f.impression_id=i.id WHERE i.id=503').fetchone()
        assert served_facts.diagnostic(store.conn,row) == trace
        assert not store.conn.execute('PRAGMA foreign_key_check').fetchall()
    def continuing_writes(store):
        receipt = store.conn.execute('SELECT * FROM artist_credit_replays WHERE delivery_key=?',
                                     ('metadata-delivery:'+str(delivery),)).fetchone()
        with store.conn:
            store.conn.execute('BEGIN IMMEDIATE')
            assert artist_credits.capture(store.conn, 108, receipt['credit_id'], 'provider', at+4,
                                          metadata_delivery_id=delivery) == (receipt['observation_id'], False)
            while credit_retention.step(store.conn, at+4, activate=True):
                pass
        assert store.conn.execute('SELECT metadata_bound FROM artist_credit_replays WHERE delivery_key=?',
                                  (receipt['delivery_key'],)).fetchone()[0] == 1
        before = store.conn.execute('SELECT count(*) FROM metadata_observations').fetchone()[0]
        with store.conn:
            catalog.choose_field(store.conn,108,'bpm','',now=at+3)
        assert store.conn.execute('SELECT bpm FROM catalog_tracks WHERE id=108').fetchone()[0] == ''
        assert store.conn.execute('SELECT count(*) FROM metadata_observations').fetchone()[0] == before
        choice = store.conn.execute('SELECT id,revision FROM metadata_runtime_choices WHERE entry_id=108').fetchone()
        assert metadata_runtime_files.read(store.conn,'choices',(choice['id'],),choice['revision'])['value'] == ''
        assert not store.conn.execute('PRAGMA foreign_key_check').fetchall()
    return verify, continuing_writes


def seed_metadata_preparation(path, *, active):
    from yt_playlist.core import metadata_runtime, served_facts
    from yt_playlist.core.preparation import metadata_runtime as preparation
    with closing(sqlite3.connect(path)) as conn:
        conn.row_factory = sqlite3.Row
        build._configure(conn)
        with conn:
            metadata_runtime.install(conn)
            served_facts.install(conn)
            # One old converted trace and one unconverted trace must converge
            # without discarding facts or reinterpreting an already thin trace.
            trace = {'candidate': {'genre': 'Jazz'}}
            for identity in (41, 42):
                conn.execute('INSERT INTO rec_served_impressions(id,request_id,surface,lane,identity_key,'
                    'rank,model_version,provenance,served_at) VALUES (?, ?,\'home\',\'\',\'song 8|artist\',1,'
                    '\'missing-policy\',?,17)', (identity, str(identity), json.dumps({} if identity == 41 else trace)))
            served_facts.publish(conn, 41, 17, trace)
        with conn:
            conn.execute('BEGIN IMMEDIATE')
            preparation.prepare_batch(conn, 'recordings', None, 128, at=20)
        if active:
            for stage in preparation.STAGES[1:]:
                cursor = None
                while True:
                    with conn:
                        conn.execute('BEGIN IMMEDIATE')
                        cursor, count = preparation.prepare_batch(conn, stage, cursor, 128, at=20)
                    if count < 128:
                        break
            with conn:
                metadata_runtime.activate(conn)
        else:
            with conn:
                conn.execute("INSERT INTO metadata_runtime_providers VALUES (999,'stale-provider')")
                conn.execute('INSERT INTO metadata_runtime_choices VALUES (999,108,2,999,1,4,NULL,999,NULL)')


def test_rebuild_reconciles_existing_metadata_preparation(development93, tmp_path):
    from yt_playlist.core import catalog

    with closing(sqlite3.connect(development93)) as conn:
        conn.row_factory = sqlite3.Row
        build._configure(conn)
        with conn:
            catalog.choose_field(conn, 108, 'bpm', 130, now=17)
            seed_versioned_scores(conn)
        assert not conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='vibe_versions'").fetchone()
    seed_metadata_preparation(development93, active=True)
    output = tmp_path/'reconciled.db'
    build.construct(development93, output, destination.for_source(development93))
    store = Store(output)
    try:
        store.init_schema()
        assert store.conn.execute('SELECT bpm FROM catalog_tracks WHERE id=108').fetchone()[0] == 130
        assert not store.conn.execute('SELECT 1 FROM metadata_runtime_choices WHERE id=999').fetchone()
        assert store.conn.execute('SELECT count(*) FROM served_facts').fetchone()[0] == 2
        assert_versioned_scores(store.conn)
        assert not store.conn.execute('PRAGMA foreign_key_check').fetchone()
    finally:
        store.conn.close()


def test_rebuild_retires_history_with_partial_installed_preparation(development93, tmp_path):
    import os
    from yt_playlist.core import artist_credits, artists, credit_retention, mode_retention
    from yt_playlist.core.db_rebuild import runtime_history, verification

    at = 40*86400.
    with closing(sqlite3.connect(development93)) as conn:
        conn.row_factory = sqlite3.Row
        build._configure(conn)
        conn.execute('BEGIN IMMEDIATE')
        for index in range(130):
            artists.observe(conn, 108, 'Artist '+str(index), [{'name': 'Artist '+str(index)}],
                            source='provider', delivery_key='revision-'+str(index), now=index+1)
        credit_retention.install(conn)
        mode_retention.install(conn)
        conn.execute('UPDATE artist_credit_retention SET enabled=1,cursor=999999,next_scan_at=?', (at+86400,))
        conn.executemany('INSERT INTO taste_modes VALUES (?,?,?,?,?,?,?,?,?,?)', [
            (1,'Current','[["House",2]]',struct.pack('ff',.25,.75),2,'["one","two"]',1,1.,2.,'basis'),
            (900,'Historical','[["Jazz",2]]',struct.pack('ff',.5,.5),2,'["old"]',0,1.,2.,'basis')])
        conn.execute('UPDATE taste_mode_retention SET enabled=1,cursor=999999,high_water=9000,next_scan_at=?',
                     (at+86400,))
        selected = [tuple(row) for row in conn.execute('SELECT * FROM entry_artist_selections')]
        conn.commit()
    os.utime(development93, (at,at))
    original = development93.read_bytes()
    output = tmp_path/'retained-history.db'
    contract = destination.for_source(development93)
    def interrupted(phase, detail):
        if detail.get('stage') == 'history:credits':
            raise InterruptedError('committed credit prefix')
    with pytest.raises(InterruptedError, match='committed credit prefix'):
        build.construct(development93, output, contract, progress=interrupted)
    build.construct(development93, output, contract)
    assert development93.read_bytes() == original
    store = Store(output)
    try:
        store.init_schema()
        assert [tuple(row) for row in store.conn.execute('SELECT * FROM entry_artist_selections')] == selected
        assert store.conn.execute('SELECT count(*) FROM artist_credit_observations').fetchone()[0] == 1
        assert store.conn.execute('SELECT count(*) FROM artist_credit_replays').fetchone()[0] == 130
        old = store.conn.execute("SELECT * FROM artist_credit_replays WHERE delivery_key='revision-0'").fetchone()
        with store.conn:
            store.conn.execute('BEGIN IMMEDIATE')
            assert artist_credits.capture(store.conn,108,old['credit_id'],'provider',at+1,
                                          delivery_key=old['delivery_key']) == (old['observation_id'],False)
        assert store.modes.next_mode_id() == 9001
        assert tuple(store.conn.execute('SELECT centroid,families,rep_keys FROM taste_modes WHERE mode_id=900')
                     .fetchone()) == (b'','[]','[]')
        assert store.conn.execute('SELECT centroid FROM taste_modes WHERE mode_id=1').fetchone()[0] == struct.pack('ff',.25,.75)
        with closing(sqlite3.connect(development93)) as reader:
            reader.row_factory = sqlite3.Row
            runtime_history.verify(reader,store.conn,at=at)
            with store.conn:
                store.conn.execute("DELETE FROM artist_credit_replays WHERE delivery_key='revision-0'")
            with pytest.raises(ValueError,match='replay outcome'):
                runtime_history.verify(reader,store.conn,at=at)
            mapping = {field:field for field in source.columns(reader,'taste_modes')}
            with store.conn:
                store.conn.execute("UPDATE taste_modes SET centroid=X'0000' WHERE mode_id=1")
            with pytest.raises(ValueError,match='retained values'):
                verification.retained(reader,store.conn,'taste_modes','taste_modes',mapping,at=at)
    finally:
        store.conn.close()


@pytest.fixture(scope='module', params=[False, True], ids=['during-copy', 'after-copy'])
def compaction_resume_input(development93_template, tmp_path_factory, request, installation_destination):
    root = tmp_path_factory.mktemp('compaction-resume')
    original = root / 'development93.db'
    with closing(sqlite3.connect(original)) as conn:
        development93_template[0].backup(conn)
    return original, root / 'resumable.db', request.param, replace(installation_destination, compact=True)


def test_final_compaction_keeps_restartable_output_at_each_interruption(
        compaction_resume_input, monkeypatch):
    development93, output, after_copy, contract = compaction_resume_input
    monkeypatch.setattr(build, '_format', admitted_rebuild_format)
    converted = []
    stopped = False
    if after_copy:
        original = build.build
        def complete_then_cancel(*args, **kwargs):
            nonlocal stopped
            result = original(*args, **kwargs)
            stopped = True
            return result
        monkeypatch.setattr(build, 'build', complete_then_cancel)
    def interrupt(phase, detail):
        if detail.get('stage'):
            converted.append(detail['stage'])
        if not after_copy and phase == 'copy' and detail.get('table') == 'metadata_observations':
            raise InterruptedError('during final retained copy')
    with pytest.raises(InterruptedError):
        build.construct(development93, output, contract, progress=interrupt, cancelled=lambda: stopped)
    assert converted
    assert output.with_name(output.name+'.compacted').exists() == after_copy


def test_rebuild_resumes_final_compaction_without_repeating_converters(
        compaction_resume_input, monkeypatch):
    development93, output, _after_copy, contract = compaction_resume_input
    if not output.exists():
        test_final_compaction_keeps_restartable_output_at_each_interruption(compaction_resume_input, monkeypatch)
    monkeypatch.setattr(build, '_format', admitted_rebuild_format)
    resumed = []
    report = build.construct(development93, output, contract,
                             progress=lambda phase, detail: resumed.append(detail.get('stage')))
    assert not any(resumed)
    assert report['compaction']['freelist_pages'] == 0
    with closing(sqlite3.connect(output)) as conn:
        assert not conn.execute('SELECT 1 FROM sqlite_schema WHERE name=?', (build.CHECKPOINT,)).fetchone()


def test_rebuild_prepares_choices_before_retiring_group_history(development93, tmp_path):
    from yt_playlist.core import evidence_ledger, group_documents, group_evidence, group_storage, home_outputs, mix_choices
    from yt_playlist.core.db_rebuild import mix_verification, verification
    from yt_playlist.core import group_reads as group_queries
    from yt_playlist.rec import choice_evidence

    with closing(sqlite3.connect(development93)) as conn:
        conn.row_factory = sqlite3.Row
        build._configure(conn)
        conn.execute('BEGIN IMMEDIATE')
        mix_choices.install(conn)
        conn.execute("INSERT INTO mix_choice_preparation VALUES (1,'roots','')")
        resolve = lambda key: 8 if key == 'song 8|artist' else None
        for serial, name in enumerate(('obsolete', 'protected', 'current'), 1):
            metadata = {'recording_inputs': {'song 8|artist': 'a' * 64}}
            prepared = dict(generation=name, at=serial, serial=serial, policy='{}', fingerprint='captured',
                payload=json.dumps(metadata), groups=[dict(group_id='leaf', version=name + '-leaf', parent_version=None,
                    payload=json.dumps(dict(name='Captured group', leaf=True, legacy=False, membership_known=True,
                        size=1, depth=0, rep_keys=['song 8|artist'], naming_policy='captured', genres=[['House', 1]])),
                    members=[('song 8|artist', .7, .8)])])
            manifest = group_storage.install_manifest(conn, metadata['recording_inputs'], resolve)
            generation = group_storage.install_header(conn, prepared, {}, {}, manifest)
            group_storage.install_versions(conn, prepared, generation, resolve)
            conn.execute('INSERT INTO group_publications VALUES (?,?)', (generation, serial))
        conn.execute('INSERT INTO group_head VALUES (1,?)', (generation,))
        group_evidence.retain(conn, 'pending', 'required-reader', {'group_generation': 'protected'}, 99)
        group_evidence.retain(conn, 'offer', 'legacy-menu', {'group_generation': 'obsolete'}, 100)
        ledger = evidence_ledger.EvidenceLedger(evidence_ledger.directory(conn), 2**63 - 1)
        context = ledger.artifact({'at': 99, 'group_generation': 'obsolete', 'groups': []})
        offer = dict(schema=1, id='legacy-menu', format='mixes', at=100, observed_at=100, episode_id='captured-episode',
            context=context, prediction=None, options=[dict(id='partial', label='Explore', tracks=[{'genre': 'House'}, {'genre': None}])])
        ledger.put('opportunity', 'legacy-menu', offer)
        conn.commit()
        expected = group_queries.head(conn, members=True)
        original_documents = {path: path.read_bytes() for path in group_documents.directory(conn).glob('*/*.json')}
    installed = Store(development93)
    try:
        installed._bind_repositories()
        display = home_outputs.prepare('home_choice', json.dumps({'schema': 1,
            'context': {'group_generation': 'protected'}, 'choice_context': {'artifact': context},
            'protos': [{'label': 'Captured card'}]}), 77)
        with installed.conn:
            installed.conn.execute('BEGIN IMMEDIATE')
            home_outputs.publish(installed.conn, display, store=installed, input_revision=23)
    finally:
        installed.conn.close()
    original = development93.read_bytes()
    output = tmp_path / 'choices.db'
    contract = destination.for_source(development93)
    def interrupt(phase, detail):
        if detail.get('stage') == 'mixes:choices':
            raise InterruptedError('after choice bookmark')
    with pytest.raises(InterruptedError, match='choice bookmark'):
        build.construct(development93, output, contract, progress=interrupt)
    build.construct(development93, output, contract)
    assert development93.read_bytes() == original
    store = Store(output)
    try:
        store.init_schema()
        assert group_queries.head(store.conn, members=True) == expected
        assert {row[0] for row in store.conn.execute('SELECT external_key FROM group_generations')} == {'protected', 'current'}
        assert not store.conn.execute("SELECT 1 FROM group_evidence_owners WHERE kind='offer'").fetchone()
        assert store.conn.execute("SELECT 1 FROM group_evidence_owners WHERE external_key='required-reader'").fetchone()
        option = store.rec_ledger.evidence().get('opportunity', 'legacy-menu')['options'][0]
        assert mix_choices.expand(store.conn, option)['genres'] == ['house']
        assert not option['genres_complete']
        assert choice_evidence.respond(store, 'legacy-menu', 'choice', 110, option_id='partial', action_id='late-response')
        root = group_documents.directory(store.conn)
        from pathlib import Path
        assert {Path(reference[3]) for reference in group_documents.references(store.conn)} <= set(root.glob('*/*.json'))
        # The original and candidate share immutable files. Repacking must not
        # collect documents still needed by the retained original or its readers.
        assert {path: path.read_bytes() for path in original_documents} == original_documents
        with closing(sqlite3.connect(development93)) as reader:
            reader.row_factory = sqlite3.Row
            damaged = json.loads(home_outputs.encoded(store.conn, 'home_choice'))
            damaged['protos'][0]['label'] = 'A different card'
            with store.conn:
                store.conn.execute('BEGIN IMMEDIATE')
                home_outputs.publish(store.conn, home_outputs.prepare('home_choice', json.dumps(damaged), 77),
                                     store=store, input_revision=23)
            with pytest.raises(ValueError, match='display contents'):
                mix_verification._displays(reader, store.conn)
            required = mix_verification.protected(reader, at=development93.stat().st_mtime)
            mapping = {field: field for field in source.columns(reader, 'group_members')}
            # Remove one operational member while leaving its parent and FK
            # intact. The independent check must reject a plausible subset.
            triggers = store.conn.execute("SELECT name FROM sqlite_schema WHERE type='trigger' AND tbl_name='group_members'").fetchall()
            with store.conn:
                for trigger, in triggers:
                    store.conn.execute('DROP TRIGGER ' + build.quote(trigger))
                store.conn.execute('DELETE FROM group_members')
            with pytest.raises(ValueError, match='retained values'):
                verification.retained(reader, store.conn, 'group_members', 'group_members', mapping,
                                      required=required['group_members'])
    finally:
        store.conn.close()


def test_rebuild_checks_compact_metadata_after_activation(development93,tmp_path):
    from yt_playlist.core import catalog
    with closing(sqlite3.connect(development93)) as conn:
        conn.row_factory = sqlite3.Row
        build._configure(conn)
        with conn:
            catalog.choose_field(conn,108,'bpm',130,now=17)
    output = tmp_path/'damaged.db'
    contract = destination.for_source(development93)
    def interrupted(phase,detail):
        if detail.get('stage') == 'guards':
            raise InterruptedError('before final independent verification')
    with pytest.raises(InterruptedError):
        build.construct(development93,output,contract,progress=interrupted)
    with closing(sqlite3.connect(output)) as conn:
        with conn:
            conn.execute('UPDATE metadata_runtime_choices SET number_value=999 WHERE kind=4')
    with pytest.raises(ValueError,match='Prepared metadata differs from source: selected manual choice'):
        build.construct(development93,output,contract)


@pytest.mark.parametrize('kind',['recording','artist'])
def test_retained_embedding_verification_checks_logical_values_and_exact_bytes(store,kind):
    from yt_playlist.core import embedding_files
    from yt_playlist.core.db_writes import atomic_write
    from yt_playlist.core.db_rebuild import verification

    table = kind+'_embeddings'
    subject = kind+'_id'
    store.tracks.upsert_track('vector','Vector','Artist','',180)
    owner = store.conn.execute('SELECT id FROM '+kind+'s ORDER BY id').fetchone()[0]
    vector = struct.pack('=fff',.1234567,-.0,1e-30)
    with closing(sqlite3.connect(':memory:')) as reader:
        reader.row_factory = sqlite3.Row
        reader.execute('CREATE TABLE '+table+'('+subject+' INTEGER,space TEXT,model_version TEXT,'
                       'dimensions INTEGER,vector BLOB,built_at REAL)')
        reader.execute('INSERT INTO '+table+' VALUES (?,\'collaborative\',\'basis\',3,?,11)',(owner,vector))
        with atomic_write(store):
            embedding_files.publish(store.conn,kind,'collaborative','basis',3,[(owner,vector,11,1)])
        verification.retained(reader,store.conn,table,table,{})
        with atomic_write(store):
            store.conn.execute('UPDATE '+kind+'_embedding_files SET built_at=12')
        with pytest.raises(ValueError,match='changed embedding ownership'):
            verification.retained(reader,store.conn,table,table,{})
        with atomic_write(store):
            embedding_files.publish(store.conn,kind,'collaborative','basis',3,[(owner,vector[::-1],11,1)])
        with pytest.raises(ValueError,match='changed exact embedding bytes'):
            verification.retained(reader,store.conn,table,table,{})


def seed_journal_policy(path):
    from yt_playlist.core import legacy_evidence, legacy_journals, policy_documents, policy_history

    original = '\n {"recording_id": 8, "paid": true, "original": "é"} '
    configuration = {'weights': {'rock': 1.23456789}, 'captured': ['exact', True]}
    with closing(sqlite3.connect(path)) as conn:
        conn.row_factory = sqlite3.Row
        build._configure(conn)
        conn.execute('BEGIN IMMEDIATE')
        legacy_evidence.retain_many(conn,'recommendation',[
            ('rec_feedback','decision',original,'paid user decision')])
        policy_documents.install(conn,[('historical',json.dumps(configuration),1)])
        conn.executemany('INSERT INTO rec_served_impressions(id,request_id,surface,lane,identity_key,'
            'rank,model_version,provenance,served_at) VALUES (?,?,\'home\',\'\',\'song 8|artist\',1,?,?,2)',[
                (501,'known','historical','{"pool_policy":"historical"}'),
                (502,'unknown','missing-policy','{}')])
        conn.commit()
    def verify(conn):
        assert legacy_evidence.original(conn,'recommendation','rec_feedback','decision')==original
        captured = legacy_journals.read(conn,'recommendation','rec_feedback','decision')
        assert captured['resolution_status']=='resolved' and captured['subjects']=={'recording_id':8}
        assert not conn.execute('SELECT 1 FROM recommendation_legacy_evidence').fetchone()
        assert policy_history.configuration(conn,'historical')==configuration
        assert list(map(tuple,conn.execute('SELECT impression_id,kind,policy_version FROM '
            'historical_impression_policies ORDER BY kind')))==[
                (501,'pool','historical'),(501,'primary','historical')]
        assert tuple(conn.execute('SELECT impression_id,raw_version,policy_version,resolution_reason '
            'FROM recommendation_impression_policies WHERE impression_id=502').fetchone())==(502,'missing-policy',None,'missing_policy')
        assert not conn.execute('PRAGMA foreign_key_check').fetchall()
    return verify


def test_retained_journal_verification_compares_contents_when_publication_changes(store,tmp_path):
    from yt_playlist.core import file_map, legacy_journals
    from yt_playlist.core.db_writes import atomic_write
    from yt_playlist.core.db_rebuild import verification

    original = {'payload':'{"paid":true,"decision":"keep"}', 'subjects':{}, 'state':'retained'}
    with closing(sqlite3.connect(tmp_path/'original.db')) as reader:
        reader.row_factory = sqlite3.Row
        legacy_journals.install(reader)
        reader.execute('BEGIN IMMEDIATE')
        legacy_journals.put(reader,'recommendation',[('rec_feedback','original',original)])
        reader.commit()
        with atomic_write(store):
            legacy_journals.put(store.conn,'recommendation',[
                ('rec_feedback','original',original), ('rec_feedback','new',{'payload':'new conversion'})])
        verification.retained(reader,store.conn,'legacy_archive_heads','legacy_archive_heads',{})
        with atomic_write(store):
            publication = file_map.update(legacy_journals.directory(store.conn)/'recommendation',
                legacy_journals.head(store.conn,'recommendation'),
                {legacy_journals.key('rec_feedback','original'):{**original,'payload':'wrong'}})
            store.conn.execute('UPDATE legacy_archive_heads SET publication=? WHERE domain=\'recommendation\'',
                               (bytes.fromhex(publication),))
        with pytest.raises(ValueError,match='changed retained archive contents'):
            verification.retained(reader,store.conn,'legacy_archive_heads','legacy_archive_heads',{})


def seed_discovery_visibility(path):
    with closing(sqlite3.connect(path)) as conn:
        conn.execute("INSERT INTO albums(id,title,metadata_rank) VALUES (26,'Discovery',0)")
        conn.execute("INSERT INTO album_provider_ids(provider,provider_id,album_id,is_primary) VALUES ('youtube','DISCOVERY',26,1)")
        conn.execute('INSERT INTO discovery_memberships(recording_id,entry_id,found_at,offered_count,first_shown,last_shown) '
                     'VALUES (8,108,10,5,11,17)')
        conn.execute('INSERT INTO album_discovery_memberships VALUES (26,10,17,11,4,3,8,9)')
        conn.executemany('INSERT INTO discovery_visible_impressions VALUES (?,?,?,?)',[
            ('first','track','song 8|artist',12),('second','track','song 8|artist',14),
            ('first','album','DISCOVERY',13)])
        conn.commit()
    def verify(conn):
        assert tuple(conn.execute('SELECT offered_count,first_shown,last_shown FROM discovery_memberships').fetchone())==(2,12.,14.)
        assert tuple(conn.execute('SELECT offered_count,first_shown,last_shown,legacy_offered_count,'
            'legacy_first_shown,legacy_last_shown FROM album_discovery_memberships WHERE album_id=26').fetchone())==(1,13.,13.,6,8.,17.)
        assert tuple(conn.execute('SELECT offered_count,first_shown,last_shown FROM discovery_legacy_offers '
            'WHERE recording_id=8').fetchone())==(3,11.,17.)
    return verify


def source_contents(path):
    """Capture committed SQL bytes and all retained bodies of this private source."""
    from tests.prepared_fixture_support import coordinates

    files = {path}
    for root in path.parent.glob(path.name+'.*'):
        if root.is_dir():
            files.update(value for value in root.rglob('*') if value.is_file())
        elif root.is_file():
            files.add(root)
    with closing(sqlite3.connect(path.as_uri()+'?mode=ro',uri=True)) as reader:
        for _, _, filename in coordinates(reader):
            if Path(filename).is_absolute():
                files.add(Path(filename))
    return {str(value):hashlib.sha256(value.read_bytes()).hexdigest() for value in files}


def seed_populated_decisions(path):
    draft = {'revision':'held','picks':['selected','selected','video-8'],'banned':['excluded'],
             'seed':17,'building':True,'opaque':{'edited':True}}
    radio = {'version':2,'episode_id':'held-episode','identity':11,'phase':'appending','playlist':'RADIO',
             'tracks':[{'video_id':'video-19'},{'video_id':'queued-only','title':'Captured title','artist':'Artist'}],
             'initial_tracks':[{'video_id':'selected'}],'position':0,
             'pending':{'video_id':'pending-only'},'pending_sent':True,'pending_started_at':14}
    recipe = {'media':'audio','model':'held-policy','facets':{'genres':['Synth Pop','Synth Pop']},
              'opaque':{'keep':[1,2]}}
    undo = json.dumps({'deletions':[{'identity':11,'ytm':'DELETING','phase':'pending'}]})
    with closing(sqlite3.connect(path)) as conn:
        build._configure(conn)
        columns = source.columns(conn,'road_trip_recipes')
        value = dict(id=7,name='Edited trip',own_pct=50,target_minutes=60,created_at=10,updated_at=11,
                     artists='[]',genres='[]',blacklist_genres='[]')
        fields = [key for key in value if key in columns]
        conn.execute('INSERT INTO road_trip_recipes('+','.join(fields)+') VALUES ('+
                     ','.join('?' for _ in fields)+')',tuple(value[key] for key in fields))
        conn.execute('INSERT INTO road_trip_drafts VALUES (?,?,?)',(7,json.dumps(draft),12))
        conn.execute('INSERT INTO rec_recipes VALUES (?,?,?)',('@account:11:LM',json.dumps(recipe),13))
        conn.execute('INSERT INTO cluster_canvas VALUES (?,?,?)',('@account:11:LM','{"edited":[3,2,1]}',14))
        conn.execute('INSERT INTO settings VALUES (?,?)',('radio_current_session',json.dumps(radio)))
        conn.execute('INSERT INTO actions VALUES (?,?,?,?,?,?,?,?)',
                     (77,'delete_playlist','{}','{}',undo,'delete_pending',15,None))
        conn.execute('INSERT INTO play_events(id,identity_id,identity_key,video_id,played_at) VALUES (?,?,?,?,?)',
                     (92,11,'@unresolved-video:queued-only','queued-only',16))
        conn.execute("INSERT INTO albums(id,title,metadata_rank) VALUES (25,'Duplicate occurrences',0)")
        conn.execute("INSERT INTO album_provider_ids(album_id,provider,provider_id,is_primary) VALUES (25,'youtube','RELEASE',1)")
        conn.executemany('INSERT INTO album_tracks VALUES (?,?,?,?,?,?,?,?,?,?)',[
            (401,25,119,None,None,4,1,1,1,'youtube'),(403,25,119,None,None,6,1,2,1,'youtube'),
            (410,25,None,3,None,9,2,1,1,'youtube'),(417,25,108,None,None,None,None,None,0,'library')])
        conn.execute('INSERT INTO album_fetch_state VALUES (25,17,1,3)')
        conn.commit()
    return draft, radio, recipe


@pytest.mark.parametrize('source_fixture',['production64'])
def test_populated_import_preserves_catalog_decisions_and_evidence(request,source_fixture,tmp_path,record_property):
    started = time.perf_counter()
    path = request.getfixturevalue(source_fixture)
    record_property('installed_source_seconds', time.perf_counter() - started)
    draft, radio, recipe = seed_populated_decisions(path)
    discovery_checks = seed_discovery_visibility(path)
    # Development-domain obligations have focused real owners in
    # test_db_rebuild_populated.py.
    released_checks = seed_released_evidence(path)
    before = source_contents(path)
    contract = destination.for_source(path)
    output = tmp_path/'decisions.db'
    # Interruption after a committed conversion batch must not duplicate files,
    # decisions or occurrence rows when this same candidate resumes.
    checkpoints = ['road_trip_drafts']
    # Only interrupted domains need tiny batches. Applying that limit to the
    # vocabulary, indexes and guards creates thousands of unrelated commits.
    contract = replace(contract, stages=tuple(
        replace(stage, batch_size=2) if stage.name in checkpoints else stage
        for stage in contract.stages))
    for checkpoint in checkpoints:
        def interrupt(phase,detail):
            if detail.get('stage')==checkpoint:
                raise InterruptedError('controlled committed '+checkpoint+' batch')
        with pytest.raises(InterruptedError,match='controlled committed'):
            build.construct(path,output,contract,progress=interrupt)
    checked_discovery = []
    def challenge_discovery(phase,detail):
        if phase != 'verify' or detail.get('destination') != contract.version:
            return
        # The registered import verifier owns the construction before final
        # retained-copy compaction transforms its historical artist captures.
        with closing(sqlite3.connect(output,uri=True)) as candidate, closing(
                sqlite3.connect(path.as_uri()+'?mode=ro',uri=True)) as reader:
            candidate.row_factory = reader.row_factory = sqlite3.Row
            candidate.execute('ATTACH DATABASE ? AS rebuild_source',(path.as_uri()+'?mode=ro',))
            discovery_checks(candidate)
            candidate.execute('BEGIN IMMEDIATE')
            try:
                candidate.execute('UPDATE discovery_legacy_offers SET offered_count=0 WHERE recording_id=8')
                with pytest.raises(ValueError,match='changed legacy discovery offers'):
                    contract.verify(reader,candidate)
            finally:
                candidate.rollback()
            discovery_checks(candidate)
        checked_discovery.append(True)
    report = build.construct(path,output,contract,progress=challenge_discovery)
    assert checked_discovery == [True]
    assert report['destination']==contract.version
    # Read leases and compatibility materialization can create new cache/lock
    # files. Every SQL/body file captured before conversion must remain exact.
    after = source_contents(path)
    assert {name:after.get(name) for name in before}==before
    from yt_playlist.core import playlist_recipe_state, radio_storage, road_trip_draft_storage
    store = Store(output)
    try:
        store.init_schema()
        conn = store.conn
        assert runner.history(conn,registry.MIGRATIONS)==registry.MIGRATIONS[-1].version
        assert conn.execute('SELECT max(version) FROM database_rebuild_source_history').fetchone()[0] == 64
        assert [tuple(row) for row in conn.execute('SELECT id,label FROM accounts ORDER BY id')] == [
            (11,'Main'),(29,'Other account')]
        assert [tuple(row) for row in conn.execute('SELECT id,account_id FROM playlists ORDER BY id')] == [(41,11),(73,29)]
        assert tuple(conn.execute('SELECT id,account_id,recording_id,membership_ref_id FROM play_events '
                                  'WHERE id=91').fetchone()) == (91,11,8,None)
        assert [row[0] for row in conn.execute('SELECT video_id FROM playlist_items '
            'WHERE playlist_id=41 ORDER BY position')] == ['video-19','video-3','video-8']
        assert road_trip_draft_storage.get(conn,7)==draft
        assert radio_storage.load_published(conn)[0]==radio
        assert playlist_recipe_state.load(conn,'@account:11:LM')['opaque']==recipe['opaque']
        assert playlist_recipe_state.load_canvas(conn,'@account:11:LM')=='{"edited":[3,2,1]}'
        assert tuple(conn.execute('SELECT recording_id,unresolved_subject_key FROM play_events WHERE id=92').fetchone())==(None,'@unresolved-video:queued-only')
        assert tuple(conn.execute('SELECT phase,outcome FROM action_steps WHERE action_id=77').fetchone())==('uncertain','unknown')
        assert [tuple(row) for row in conn.execute('SELECT o.legacy_row_id,o.position,s.entry_id,o.recording_id '
                'FROM album_track_occurrences o LEFT JOIN album_occurrence_sources s ON s.occurrence_id=o.id '
                'ORDER BY o.legacy_row_id')]==[(401,4,119,19),(403,6,119,19),(410,9,None,3)]
        assert tuple(conn.execute('SELECT legacy_row_id,entry_id FROM album_item_observations').fetchone())==(417,108)
        assert conn.execute('SELECT status FROM album_tracklists').fetchone()[0]=='partial'
        assert conn.execute('SELECT current_tracklist_id FROM albums WHERE id=25').fetchone()[0] is None
        assert conn.execute('SELECT count(*) FROM metadata_observations').fetchone()[0]==0
        discovery_checks(conn)
        released_checks(conn)
        store.settings.set_setting('post_rebuild_write','works')
        assert store.settings.get_setting('post_rebuild_write')=='works'
        assert not conn.execute('PRAGMA foreign_key_check').fetchall()
    finally:
        store.conn.close()


def seed_released_evidence(path):
    raw_input = json.dumps({'library':{'genre':'Synth Pop','year':'2001','audio':{'bpm':123.5}},'discovered':None})
    paid = json.dumps({'status':'ready','description':'Preserved paid prose. é','model':'deepseek-flash',
                      'at':86405,'usage':{'total_tokens':18},'opaque':['retained',1]},ensure_ascii=False,indent=2)
    vector = struct.pack('=ff',.25,.75)
    with closing(sqlite3.connect(path)) as conn:
        build._configure(conn)
        conn.execute("UPDATE recordings SET genre='Synth Pop' WHERE id=8")
        conn.execute("INSERT INTO rec_weights VALUES ('genre:Synth Pop',1.25,10)")
        conn.execute('INSERT INTO rec_mood(rowid,created_at,direction,keys) VALUES (31,11,1,?)',
                     (json.dumps(['song 8|artist','song 8|artist','missing captured key']),))
        conn.execute('INSERT INTO rec_content_vectors VALUES (?,?)',('song 8|artist',vector))
        conn.execute('INSERT INTO rec_prepared_content VALUES (?,?,?,?,?)',
                     ('song 8|artist',raw_input,hashlib.sha256(raw_input.encode()).hexdigest(),12,7))
        conn.execute('INSERT INTO metadata_findings(id,entry_id,recording_id,field,provider,value_json,observed_at) '
                     "VALUES (71,108,8,'title','youtube','\"Captured title\"',13)")
        conn.execute("INSERT INTO entry_field_choices VALUES (108,'title','\"Chosen title\"','user')")
        conn.execute("INSERT INTO audio_vibe_feedback VALUES ('missing-run','[]','null','',14)")
        conn.execute('INSERT INTO rec_proposals VALUES (?,?,?)',('album_description:unresolved-hash',paid,86405.5))
        conn.executemany('INSERT INTO rec_group_identities VALUES (?,NULL,1)',[('parent',),('leaf',)])
        for generation,at in (('first',1),('second',2)):
            payload = json.dumps({'recording_inputs':{'song 8|artist':'1'*64,'missing captured key':'2'*64},
                                  'diagnostics':{'captured':at}})
            conn.execute('INSERT INTO rec_group_generations VALUES (?,?,?,?,?,?)',
                         (generation,'{"algorithm":"grouping-1"}','input-fingerprint',at,at,payload))
            for group,parent in (('parent',None),('leaf',generation+'-parent')):
                version = generation+'-'+group
                definition = json.dumps({'name':group,'leaf':group=='leaf','legacy':False,
                    'membership_known':True,'size':1,'depth':int(group=='leaf'),
                    'rep_keys':['song 8|artist'],'naming_policy':'naming-1'})
                conn.execute('INSERT INTO rec_group_definitions VALUES (?,?,?,?,?)',
                             (version,generation,group,parent,definition))
                conn.execute('INSERT INTO rec_group_members VALUES (?,?,.9,.8)',(version,'song 8|artist'))
            conn.execute('INSERT INTO rec_group_unknown VALUES (?,?,?)',(generation,'missing captured key','unmeasured'))
        conn.execute('INSERT INTO rec_group_lineage VALUES (?,?,?,?)',('first-leaf','second-leaf','continuation',
                     '{"old_share":1,"new_share":1,"overlap":1,"compatible":true}'))
        conn.executemany('INSERT INTO rec_group_publications VALUES (?)',[('first',),('second',)])
        conn.execute("INSERT INTO rec_group_head VALUES (1,'second')")
        conn.commit()
    from yt_playlist.core import album_description_files, embedding_files
    def verify(conn):
        assert tuple(conn.execute('SELECT id,created_at,direction FROM mood_events').fetchone())==(31,11,1)
        assert tuple(conn.execute('SELECT recording_id,position FROM mood_event_recordings').fetchone())==(8,0)
        assert tuple(conn.execute('SELECT legacy_key,position FROM mood_event_unresolved_seeds').fetchone())==('missing captured key',2)
        head = conn.execute('SELECT recording_id,vector,dimensions,publication,position FROM '+
                            embedding_files.relation(conn,'recording')).fetchone()
        assert head['recording_id']==8 and head['dimensions']==2
        assert embedding_files.load_many(conn,'recording',[head])==[(8,vector)]
        assert tuple(conn.execute('SELECT recording_id,library_bpm,input_revision FROM prepared_recording_inputs').fetchone())==(8,123.5,7)
        assert conn.execute('SELECT count(*) FROM metadata_legacy_records').fetchone()[0]==0
        from yt_playlist.core import metadata_runtime, metadata_runtime_files
        choice = conn.execute('SELECT id,revision FROM metadata_runtime_choices WHERE entry_id=108 AND field=?',
                              (metadata_runtime.code('title'),)).fetchone()
        assert metadata_runtime_files.read(conn, 'choices', (choice['id'],), choice['revision'])['value']=='Chosen title'
        assert conn.execute('SELECT count(*) FROM audio_annotations WHERE sealed=1').fetchone()[0]==1
        row = conn.execute('SELECT * FROM album_descriptions').fetchone()
        assert album_description_files.read(row['filename'],row['content_digest'])==paid
        assert conn.execute('SELECT count(*) FROM group_versions').fetchone()[0]==2
        assert conn.execute('SELECT count(*) FROM generation_groups').fetchone()[0]==2
        assert conn.execute('SELECT count(*) FROM group_manifest_inputs').fetchone()[0]==1
        assert conn.execute('SELECT count(*) FROM unresolved_group_manifest_inputs').fetchone()[0]==1
        # No operational owner needs the first generation or its lineage.
        assert not conn.execute('SELECT 1 FROM group_lineage').fetchall()
        assert not conn.execute('PRAGMA foreign_key_check').fetchall()
    return verify


@pytest.mark.parametrize('source_fixture',['production64','development93'])
@pytest.mark.parametrize('failure',[None,'staging','reopening'])
def test_real_import_installs_or_rolls_back_with_feedback_retained(
        request, source_fixture, failure, installation_destination):
    from yt_playlist.core import legacy_journals, playback_feedback
    from yt_playlist.core.db_rebuild import job
    path = request.getfixturevalue('admitted_development93' if source_fixture == 'development93' else source_fixture)
    store = Store(path)
    store.init_schema()
    original_files = {}
    if source_fixture == 'development93':
        with store.conn:
            store.conn.execute('BEGIN IMMEDIATE')
            legacy_journals.retain_many(store.conn,'recommendation',[
                ('rec_feedback','already-prepared','{"paid":true,"decision":"keep"}','user decision')])
        original_files = {p.relative_to(legacy_journals.directory(store.conn)):p.read_bytes()
                          for p in legacy_journals.references(store.conn)}
    inode = path.stat().st_ino
    contract = installation_destination
    captured = False
    def progress(phase,detail):
        nonlocal captured
        if phase=='copy' and not captured:
            assert store.activity_paused
            playback_feedback.capture(store,'rating',11,{'video_id':'video-3','status':'LIKE'},20)
            captured = True
            assert playback_feedback.drain(store)==0
        if phase==failure:
            raise RuntimeError('controlled reopen failure')
    try:
        if failure:
            with pytest.raises(RuntimeError,match='controlled reopen'):
                job.run(store,destination=contract,progress=progress)
            assert path.stat().st_ino==inode
            assert store.conn.execute('SELECT max(version) FROM schema_migrations').fetchone()[0]==(
                64 if source_fixture=='production64' else 93)
            assert playback_feedback.inbox(store).pending()==1
            for relative, encoded in original_files.items():
                assert (legacy_journals.directory(store.conn)/relative).read_bytes()==encoded
            if source_fixture=='production64':
                assert store.activity_paused and store.conn.execute('PRAGMA query_only').fetchone()[0]==1
                assert playback_feedback.drain(store)==0
            else:
                assert not store.activity_paused
                assert playback_feedback.drain(store)==1
                assert playback_feedback.drain(store)==0
        else:
            report = job.run(store,destination=contract,progress=progress)
            assert report['phase']=='complete' and path.stat().st_ino!=inode
            assert not store.activity_paused
            assert playback_feedback.drain(store)==1
            assert playback_feedback.drain(store)==0
            assert store.tracks.item_rating(11,'video-3')['status']=='LIKE'
            assert store.conn.execute('SELECT count(*) FROM playback_feedback_receipts').fetchone()[0]==1
            store.settings.set_setting('after rebuild','accepted')
        assert [p.id for p in store.playlists.get_playlists()]==[41,73]
        if source_fixture == 'development93':
            assert legacy_journals.read(store.conn,'recommendation','rec_feedback','already-prepared')['payload']==(
                '{"paid":true,"decision":"keep"}')
        assert not store.conn.execute('PRAGMA foreign_key_check').fetchall()
    finally:
        store.conn.close()
