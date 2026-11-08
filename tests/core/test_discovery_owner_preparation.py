"""Discovery staging preserves origins, visit evidence and candidate output."""
from dataclasses import replace
import json
from pathlib import Path
import sqlite3

import pytest

from tests.core.test_predecessor_writes import consumer_installed as installed
from tests.prepared_fixture_support import assert_upgrade_phase, custody_predecessor, custody_upgrade_cases
from tests.store_fixture_support import publication_consumer_reads, reuse_reporting_reader
from yt_playlist.core import collection_recovery, discovery_provenance as provenance, policy_documents
from yt_playlist.core.migrations import MigrationError, regular, runner
from yt_playlist.core.preparation import discovery_artists as documents, discovery_subjects as rows
from yt_playlist.core.store import Store


pytestmark = pytest.mark.usefixtures('publication_consumer_reads')


@pytest.fixture(autouse=True)
def disable_periodic_library_sync():
    """Discovery preparation checks do not construct an HTTP application."""


@pytest.mark.parametrize('version', [79, 93, 100])
def test_discovery_input_keeps_each_installed_contract_and_snapshot_guards(_bounded_installed_schema, version):
    from contextlib import closing

    template = _bounded_installed_schema(version)
    source = template.source
    with closing(sqlite3.connect(source._db_path.as_uri() + '?mode=ro', uri=True)) as reader:
        reader.row_factory = sqlite3.Row
        runner.validate_version(reader, version)
        regular.validate(reader)
    assert source.conn.execute('PRAGMA query_only').fetchone()[0] == 1
    assert not source.conn.execute('SELECT 1 FROM main.discovered_artists').fetchone()
    assert template.revision_guards


def finish(job, conn, size=128):
    for _ in range(180):
        progress = job.step(conn, batch_size=size)
        if progress.status == 'prepared':
            return progress
    pytest.fail('Discovery preparation did not finish bounded fixture')


def seed(store):
    # Ingestion is fixture setup here. Keep its real SQL and file-backed reader,
    # without repeatedly admitting reporting connections for each source.
    with pytest.MonkeyPatch.context() as patch, reuse_reporting_reader(store, patch):
        account = store.accounts.upsert_account('Main','main.json',None,True)
        store.discovery.upsert_discovered_track('song|artist','song','Song','Artist',None,None,None,None,'radio:song',1)
        store.discovery.upsert_discovered_album('release','Artist','Release',None,None,1)
        store.discovery.upsert_discovered_artist('Candidate',2,['Anchor','Anchor'],['Playlist'],None,1)
        aid = store.conn.execute('SELECT artist_id FROM main.discovered_artists').fetchone()[0]
        rid = store.conn.execute("SELECT recording_id FROM main.catalog_entries WHERE video_id='song'").fetchone()[0]
        album = store.conn.execute("SELECT album_id FROM main.album_provider_ids WHERE provider='youtube' AND provider_id='release'").fetchone()[0]
        with store.conn:
            store.conn.execute('INSERT INTO main.artist_scans VALUES (?,NULL,?)', ('@artist:' + str(aid), aid))
            store.conn.executemany('INSERT INTO main.discovery_artist_sources VALUES (?,?,?,?)',
                [(aid, 'spotify_export:' + str(account), account, 3), (aid, 'artist:missing-provider', None, 1)])
            store.conn.executemany('INSERT INTO main.discovery_visible_impressions VALUES (?,?,?,?)',
                [('visit','track','song|artist',3),('visit','artist',str(aid),3),('other','artist',str(aid),4),
                 ('missing','album','absent',5),('unsupported','mystery','unknown',6)])
            store.conn.execute('UPDATE main.discovered_artists SET offered_count=9,first_shown=0,last_shown=4,legacy_offered_count=7,legacy_first_shown=NULL,legacy_last_shown=1')
        return account, aid, rid, album


def artist_job(version):
    return documents.JOBS['artists_legacy' if version < 86 else 'artists_classified']


def artist_source(conn):
    return conn.execute('SELECT artist FROM main.discovered_artists').fetchone()[0]


@pytest.mark.parametrize('installed',[79,93,100],indirect=True)
def test_exact_sources_origins_counters_and_explanations(installed):
    store, version = installed
    account, aid, rid, _ = seed(store)
    jobs = (rows.JOB, artist_job(version)) + ((rows.CLASSIFICATION_JOB,) if version >= 86 else ())
    for job in jobs:
        job.install(store.conn)
        finish(job, store.conn, size=2)
    for owner, target in rows.TARGETS.items():
        staged = list(store.conn.execute(f'SELECT * FROM {target} WHERE source_owner=?', (owner,)))
        assert len(staged) == store.conn.execute('SELECT count(*) FROM main.' + owner).fetchone()[0]
        for row in staged:
            original = rows.original(store.conn, owner, row['source_key'], row['source_digest'])
            assert json.dumps([original[key] for key in rows.KEYS[owner]],ensure_ascii=False,separators=(',',':')) == row['source_key']
    origin = store.conn.execute("SELECT * FROM prepared_discovery_source_rows WHERE kind='radio'").fetchone()
    assert (origin['seed_video_id'], origin['recording_id'], origin['first_found_at'], origin['last_found_at']) == ('song',rid,1,1)
    imported = store.conn.execute("SELECT * FROM prepared_discovery_artist_source_rows WHERE kind='spotify_export'").fetchone()
    assert (imported['account_id'], imported['resolution_status'], imported['import_key']) == (account,'legacy_account',None)
    unresolved = store.conn.execute("SELECT legacy_source_key,resolution_status FROM prepared_discovery_artist_source_rows WHERE kind='legacy_unknown'").fetchone()
    assert tuple(unresolved) == ('artist:missing-provider','unresolved')
    assert list(map(tuple,store.conn.execute('SELECT original_key,resolution_reason FROM prepared_discovery_impression_rows WHERE resolution_reason IS NOT NULL ORDER BY original_key'))) == [('absent','missing_owner'),('unknown','unsupported_kind')]
    assert not store.conn.execute("SELECT 1 FROM youtube_items WHERE video_id='missing-provider'").fetchone()
    source = artist_source(store.conn)
    name = 'artists_legacy' if version < 86 else 'artists_classified'
    head = documents.head(store.conn,name,source)
    original = documents.read_original(store.conn,name,head)
    assert original == dict(store.conn.execute('SELECT * FROM main.discovered_artists').fetchone())
    assert (head['artist_id'],head['offered_count'],head['legacy_offered_count'],head['legacy_first_shown']) == (aid,9,7,None)
    assert collection_recovery.read(head['recovery_filename'],head['recovery_digest'],head['recovery_bytes'],kind='discovery-candidate',owner=aid) == {'because':['Anchor','Anchor'],'fits':['Playlist']}
    assert not store.conn.execute('PRAGMA foreign_key_check').fetchone()


def prepare_native_comparison(store, version):
    _, aid, _, _ = seed(store)
    job = artist_job(version)
    for candidate in (rows.JOB,job):
        candidate.install(store.conn)
        finish(candidate,store.conn)
    name = 'artists_legacy' if version < 86 else 'artists_classified'
    source = artist_source(store.conn)
    original_rows = {(owner, row['source_key']): rows.original(store.conn, owner, row['source_key'], row['source_digest'])
        for owner, target in rows.TARGETS.items()
        for row in store.conn.execute(f'SELECT * FROM {target} WHERE source_owner=?', (owner,))}
    return aid, name, source, original_rows, documents.read_original(store.conn, name, documents.head(store.conn, name, source))


@pytest.fixture(scope='module')
def native_inputs(upgraded_custody_inputs):
    from functools import partial

    def get(version, target, **options):
        return upgraded_custody_inputs(version, target, 'discovery-native-agreement',
            partial(prepare_native_comparison, version=version), **options)

    get.release = lambda version: upgraded_custody_inputs.release(version, 'discovery-native-agreement')
    return get


def assert_native_originals(template):
    _, name, source, originals, document = template.result
    conn = template.source.conn
    for owner, target in rows.TARGETS.items():
        for row in conn.execute(f'SELECT * FROM {target} WHERE source_owner=?', (owner,)):
            assert rows.original(conn, owner, row['source_key'], row['source_digest']) == originals[owner, row['source_key']]
    assert documents.read_original(conn, name, documents.head(conn, name, source)) == document


@pytest.mark.parametrize('version', [79, 100])
def test_native_comparison_input_keeps_exact_preparation(native_inputs, version):
    template = native_inputs(version, version)
    assert template.source.conn.execute('PRAGMA query_only').fetchone()[0] == 1
    assert_native_originals(template)


@pytest.mark.parametrize('version,target,phase', custody_upgrade_cases((79, 100)))
def test_native_upgrade_phase_keeps_exact_preparation(native_inputs, version, target, phase):
    template = native_inputs(version, target, phase=phase)
    assert_upgrade_phase(template, target if phase == 'converted' else custody_predecessor(version, target))
    assert_native_originals(template)


@pytest.mark.parametrize('version', [79, 100])
def test_native_serving_input_keeps_private_read_guards(native_inputs, version):
    template = native_inputs(version, 107)
    assert_upgrade_phase(template, 107)
    assert template.source._snapshot_revisions.conn is template.source.conn


@pytest.mark.parametrize('version', [79, 100])
def test_frozen_upgrade_agrees_without_stamping_history(native_inputs, version, tmp_path):
    prepared = native_inputs(version, version)
    store = prepared.source
    aid = prepared.result[0]
    with native_inputs(version, 107).open(tmp_path / 'discovery-upgrade.db') as native:
        staged = store.conn.execute('SELECT * FROM prepared_discovery_source_rows').fetchone()
        actual = native.conn.execute('SELECT s.*,o.* FROM discovery_sources s JOIN discovery_origins o ON o.id=s.origin_id').fetchone()
        for field in ('recording_id','input_video_id','first_found_at','last_found_at','kind','seed_video_id','resolution_status'):
            assert staged[field] == actual[field]
        assert native.conn.execute('SELECT count(*) FROM discovery_impressions').fetchone()[0] == store.conn.execute('SELECT count(*) FROM prepared_discovery_impression_rows WHERE resolution_reason IS NULL').fetchone()[0]
        # Offers remain a separate source fact. Native publication computes the
        # unverified residue after deduplicating canonical visits.
        candidate = native.conn.execute('SELECT * FROM discovered_artists WHERE artist_id=?',(aid,)).fetchone()
        assert (candidate['offered_count'],candidate['legacy_offered_count']) == (2,14)
        assert provenance.explanation(native.conn,candidate) == {'because':['Anchor','Anchor'],'fits':['Playlist']}
        assert not native.conn.execute('PRAGMA foreign_key_check').fetchone()


@pytest.mark.parametrize('installed',[93],indirect=True)
def test_bounded_rows_continuing_writes_missing_owners_and_ambiguity(installed):
    store, _ = installed
    _, aid, rid, _ = seed(store)
    other = store.tracks.upsert_track('other','Other','Other',None,10)
    other_rid = store.tracks.catalog_subject(other)['recording_id']
    with store.conn:
        # Two complete 16-row pages and a tail preserve bounded continuation.
        store.conn.executemany('INSERT INTO main.discovery_visible_impressions VALUES (?,\'track\',\'song|artist\',1)', [('visit-'+str(i),) for i in range(33)])
        store.conn.executemany("INSERT INTO main.recording_aliases VALUES ('ambiguous', ?, 'captured')", [(rid,),(other_rid,)])
        store.conn.execute("INSERT INTO main.discovery_visible_impressions VALUES ('ambiguous','track','ambiguous',2)")
        store.conn.execute("INSERT INTO main.artist_scans VALUES ('unresolved',NULL,NULL)")
    rows.JOB.install(store.conn)
    assert rows.JOB.step(store.conn,batch_size=512).processed <= 16
    finish(rows.JOB,store.conn)
    assert store.conn.execute("SELECT resolution_reason FROM prepared_discovery_impression_rows WHERE view_id='ambiguous'").fetchone()[0] == 'ambiguous_owner'
    assert store.conn.execute("SELECT resolution_reason FROM prepared_discovery_scan_rows WHERE artist_id IS NULL").fetchone()[0] == 'missing_canonical_artist'
    with store.conn:
        store.conn.execute("INSERT INTO main.artist_provider_ids VALUES ('youtube', 'missing-provider', ?)",(aid,))
        store.conn.execute("DELETE FROM main.discovery_visible_impressions WHERE view_id='visit-0'")
    assert rows.JOB.progress(store.conn).prepared_revision is None
    finish(rows.JOB,store.conn)
    assert not store.conn.execute("SELECT 1 FROM prepared_discovery_impression_rows WHERE view_id='visit-0'").fetchone()
    assert store.conn.execute("SELECT artist_id FROM prepared_discovery_artist_source_rows WHERE kind='artist'").fetchone()[0] == aid
    assert store.conn.execute("SELECT 1 FROM prepared_discovery_row_originals WHERE snapshot LIKE '%visit-0%'").fetchone()
    with store.conn:
        store.conn.execute("DELETE FROM main.artist_scans WHERE artist='unresolved'")


@pytest.mark.parametrize('installed',[93],indirect=True)
def test_large_artist_key_only_resume_failure_and_retry(installed,monkeypatch):
    store, _ = installed
    _, aid, _, _ = seed(store)
    with store.conn:
        store.conn.execute('UPDATE main.discovered_artists SET because=?', (json.dumps(['anchor']*24000),))
    job = artist_job(93)
    name = 'artists_classified'
    job.install(store.conn)
    job.step(store.conn,batch_size=1)
    def authorizer(action,owner,column,*unused):
        return sqlite3.SQLITE_DENY if action == sqlite3.SQLITE_READ and owner == 'discovered_artists' and column in ('because','fits','thumbnail') else sqlite3.SQLITE_OK
    store.conn.set_authorizer(authorizer)
    try:
        while isinstance(json.loads(job.progress(store.conn).cursor),dict):
            job.step(store.conn,batch_size=1)
    finally:
        store.conn.set_authorizer(None)
    finish(job,store.conn)
    source = artist_source(store.conn)
    old = documents.head(store.conn,name,source)
    with store.conn:
        store.conn.execute("UPDATE main.discovered_artists SET because='malformed',fits='[\"Still retained\"]'")
    publish = policy_documents._publish_encoded
    def fail(*args):
        assert not store.conn.in_transaction
        raise OSError('controlled discovery file failure')
    monkeypatch.setattr(policy_documents,'_publish_encoded',fail)
    with pytest.raises(OSError,match='file failure'):
        job.step(store.conn)
    failed = job.progress(store.conn)
    def install(conn,context):
        conn.execute('CREATE TABLE independent_discovery_ddl(value TEXT)')
    def validate(conn):
        assert conn.execute("SELECT 1 FROM sqlite_schema WHERE name='independent_discovery_ddl'").fetchone()
    monkeypatch.setattr(regular,'CHANGES',(regular.Change(1,'independent_discovery_ddl',install,validate),))
    regular.migrate(store.conn)
    assert job.progress(store.conn) == failed
    monkeypatch.setattr(policy_documents,'_publish_encoded',publish)
    with pytest.raises(MigrationError,match='retry explicitly'):
        job.step(store.conn)
    job.step(store.conn,retry=True)
    finish(job,store.conn)
    current = documents.head(store.conn,name,source)
    assert current['explanation_status'] == 'malformed'
    assert documents.read_original(store.conn,name,current)['fits'] == '["Still retained"]'
    assert len(json.loads(documents.read_original(store.conn,name,old)['because'])) == 24000


@pytest.mark.parametrize('installed',[79],indirect=True)
def test_interruption_missing_plan_reopened_retry_and_planning_fence(installed,monkeypatch):
    store, _ = installed
    seed(store)
    job = artist_job(79)
    job.install(store.conn)
    finish(job,store.conn)
    # Force a multi-part original before exercising a resumed checkpoint.
    with store.conn:
        store.conn.execute('UPDATE main.discovered_artists SET fits=?',(json.dumps(['fit']*20000),))
    checkpoint = job.step(store.conn,batch_size=1)
    assert isinstance(json.loads(checkpoint.cursor),dict)
    def interrupt(*args):
        documents.copy('artists_legacy',*args)
        raise KeyboardInterrupt('controlled discovery interruption')
    with pytest.raises(KeyboardInterrupt):
        replace(job,copy=interrupt).step(store.conn,batch_size=1)
    assert job.progress(store.conn) == checkpoint
    manifest = store.conn.execute('SELECT plan_filename FROM prepared_discovery_artists_legacy_generations ORDER BY preparation_revision DESC LIMIT 1').fetchone()[0]
    Path(manifest).unlink()
    with pytest.raises(FileNotFoundError):
        job.step(store.conn)
    reopened = Store(store._db_path)
    try:
        job.step(reopened.conn,retry=True)
        finish(job,reopened.conn)
    finally:
        reopened.conn.close()
    # A source edit during file planning must reject the entire stale batch.
    with store.conn:
        store.conn.execute("UPDATE main.discovered_artists SET fits='[]'")
    prepare = job.prepare
    def changed(conn,row,**kwargs):
        plan = prepare(conn,row,**kwargs)
        with sqlite3.connect(store._db_path) as writer:
            writer.execute("UPDATE main.discovered_artists SET fits='[\"new\"]'")
        return plan
    captured = job.progress(store.conn)
    progress = replace(job,prepare=changed).step(store.conn)
    assert progress.processed == captured.processed
    finish(job,store.conn)
    head = documents.head(store.conn,'artists_legacy',artist_source(store.conn))
    assert documents.read_original(store.conn,'artists_legacy',head)['fits'] == '["new"]'


@pytest.mark.parametrize('installed',[79],indirect=True)
def test_artist_key_changes_withdraw_heads_and_preserve_originals(installed):
    store, _ = installed
    _, aid, _, _ = seed(store)
    job = artist_job(79)
    for candidate in (rows.JOB,job):
        candidate.install(store.conn)
        finish(candidate,store.conn)
    staged = store.conn.execute('SELECT * FROM prepared_discovery_scan_rows').fetchone()
    assert staged['artist_id'] == aid
    original_scan = rows.original(store.conn,'artist_scans',staged['source_key'],staged['source_digest'])
    assert original_scan['artist_id'] == aid
    source = artist_source(store.conn)
    old = documents.head(store.conn,'artists_legacy',source)
    assert documents.read_original(store.conn,'artists_legacy',old)['artist'] == source
    with store.conn:
        store.conn.execute("UPDATE main.discovered_artists SET artist='Renamed'")
        store.conn.execute("UPDATE main.artist_scans SET artist='Renamed'")
    finish(rows.JOB,store.conn)
    assert not store.conn.execute('SELECT 1 FROM prepared_discovery_scan_rows WHERE source_key=?',(staged['source_key'],)).fetchone()
    finish(job,store.conn)
    assert documents.head(store.conn,'artists_legacy',source) is None
    current = documents.head(store.conn,'artists_legacy',artist_source(store.conn))
    assert (current['artist_id'],documents.read_original(store.conn,'artists_legacy',current)['artist']) == (aid,'Renamed')
    assert documents.read_original(store.conn,'artists_legacy',old)['artist'] == source


@pytest.mark.parametrize('installed',[93],indirect=True)
def test_nonfinite_output_and_oversized_source_keep_last_good_custody(installed):
    store, _ = installed
    seed(store)
    job = artist_job(93)
    with store.conn:
        store.conn.execute('UPDATE main.discovered_artists SET because=\'{"origin":"Anchor"}\',fits=\'null\'')
    for candidate in (rows.JOB,job):
        candidate.install(store.conn)
        finish(candidate,store.conn)
    source = artist_source(store.conn)
    previous = documents.head(store.conn,'artists_classified',source)
    assert collection_recovery.read(previous['recovery_filename'],previous['recovery_digest'],previous['recovery_bytes'],
        kind='discovery-candidate',owner=previous['artist_id']) == {'because':{'origin':'Anchor'},'fits':[]}
    with store.conn:
        store.conn.execute("UPDATE main.discovered_artists SET because='[NaN]'")
    with pytest.raises(ValueError,match='JSON compliant'):
        job.step(store.conn)
    assert documents.head(store.conn,'artists_classified',source)['preparation_revision'] == previous['preparation_revision']
    old = store.conn.execute('SELECT * FROM prepared_discovery_source_rows').fetchone()
    with store.conn:
        store.conn.execute('UPDATE main.discovery_sources SET source_token=?',('x'*(rows.MAX_ROW_BYTES+1),))
    with pytest.raises(ValueError,match='byte budget'):
        rows.JOB.step(store.conn)
    assert store.conn.execute('SELECT source_digest FROM prepared_discovery_source_rows').fetchone()[0] == old['source_digest']
    with store.conn:
        store.conn.execute("UPDATE main.discovery_sources SET source_token='radio:song'")
        store.conn.execute("UPDATE main.discovered_artists SET because='[]'")


@pytest.mark.parametrize('version', [79, 100])
def test_native_sources_finish_with_committed_read_only_inputs(native_inputs, version):
    for source in native_inputs.release(version):
        with pytest.raises(sqlite3.ProgrammingError, match='closed'):
            source.conn.execute('SELECT 1')
