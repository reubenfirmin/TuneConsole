"""Album-note selection preserves exact files, current writers and native precedence."""
import json
from pathlib import Path
import sqlite3

import pytest

from tests.core.test_album_note_preparation import NOTES, finish, header, seed
from tests.core.test_predecessor_writes import installed
from yt_playlist.core import recommendation_outputs as files
from yt_playlist.core.migrations import MigrationError, registry, regular, runner
from yt_playlist.core.preparation import album_notes as preparation, album_note_publication as publication
from yt_playlist.core.store import Store


def prepared(conn):
    preparation.JOB.install(conn)
    for _ in range(160):
        result = preparation.JOB.step(conn, batch_size=8)
        if result.status == 'prepared':
            return result.prepared_revision
    pytest.fail('Album-note preparation did not finish bounded fixture')


@pytest.mark.parametrize('installed', [83], indirect=True)
def test_held_menu_poll_keeps_visible_demand_with_published_notes(installed):
    from starlette.testclient import TestClient
    import uvloop
    from tests.test_album_web import _client
    from yt_playlist.rec import choice_evidence

    store, _ = installed
    album = {'browse_id':'release', 'artist':'Artist', 'title':'Album'}
    notes = {'release':{'description':{'text':'Retained prose', 'kind':'generated', 'url':'https://www.deepseek.com/'}}}
    seed(store, notes)
    publication.publish(store.conn, revision=prepared(store.conn))
    # A previously used, currently prepared format is allowed to resume through
    # the shared journey gate. Supply that separate prerequisite explicitly.
    store.rec_proposals.put_proposals('listening_choices', {'home_format':{'episodes':{'albums':1}}}, 1)
    store.rec_proposals.put_proposals('album_prepared', {'items':[
        {**album, 'score':.9}, {'browse_id':'second', 'artist':'Other', 'title':'Second', 'score':.8}],
        'context':{}, 'built_at':1}, 1)
    base, _, _ = _client(store)
    # The default asyncio cross-thread portal stalls even for an unrelated
    # minimal Starlette app on this runtime. Exercise the actual sync route
    # with the application's uvloop implementation instead.
    with TestClient(base.app, base_url='http://127.0.0.1',
            backend_options={'loop_factory':uvloop.new_event_loop}) as client:
        opportunity = choice_evidence.present(store, 'albums', [{**album, 'id':'release'}], 1,
            context={}, window=0)
        response = client.get('/home/album-notes', params={'browse':'release', 'opportunity':opportunity})
    assert response.status_code == 200
    store.rec_proposals.request_album_notes([], 2, current_only=True)
    assert 'release' in store.rec_proposals.get_proposals('album_note_requests')
    assert store.rec_proposals.get_proposals('album_notes') == notes


@pytest.mark.parametrize('installed', [79,83], indirect=True)
def test_preopened_readers_select_verified_files_and_match_native_outputs(installed, tmp_path):
    store, version = installed
    raw = seed(store)
    preopened, native = Store(store._db_path), Store(tmp_path/'native.db')
    try:
        preopened.init_schema(defer_metadata=True)
        store.conn.backup(native.conn)
        runner.migrate(native.conn, registry=registry.MIGRATIONS[:84])
        native.init_schema(defer_metadata=True)
        revision = prepared(store.conn)
        publication.publish(store.conn, revision=revision)
        expected = native.rec_proposals.get_proposals('album_notes')
        assert expected == NOTES
        def deny_source(operation, owner, column, *unused):
            return sqlite3.SQLITE_DENY if operation == sqlite3.SQLITE_READ and owner == 'rec_proposals' and column == 'payload' else sqlite3.SQLITE_OK
        with preopened._lock:
            preopened.conn.set_authorizer(deny_source)
            try:
                assert preopened.rec_proposals.get_proposals('album_notes') == expected
                assert preopened.rec_proposals.has_proposals('album_notes')
            finally:
                preopened.conn.set_authorizer(None)
        assert preparation.read_original(header(store.conn)) == raw
        assert store.conn.execute("SELECT payload FROM main.rec_proposals WHERE surface='album_notes'").fetchone()[0] == raw
        assert not store.conn.execute("SELECT 1 FROM album_provider_ids WHERE provider_id='unadmitted'").fetchone()
        assert store.conn.execute('SELECT count(*) FROM main.albums').fetchone()[0] == 1
        regular.validate(preopened.conn)
        assert version == store.conn.execute('SELECT max(version) FROM schema_migrations').fetchone()[0]
    finally:
        native.conn.close()
        preopened.conn.close()


@pytest.mark.parametrize('installed', [83], indirect=True)
@pytest.mark.parametrize('failure', ['field','reference','extra','omitted','time','original','file'])
def test_incorrect_files_headers_references_or_coverage_cannot_publish(installed, failure):
    store, _ = installed
    seed(store)
    revision = prepared(store.conn)
    with store.conn:
        if failure == 'field':
            store.conn.execute("UPDATE prepared_album_note_files SET browse_id='other' WHERE position=0")
        elif failure == 'reference':
            store.conn.execute("UPDATE prepared_album_note_files SET album_provider_id=NULL,unresolved_reason='missing' WHERE position=0")
        elif failure == 'extra':
            store.conn.execute("INSERT INTO prepared_album_note_files SELECT generation,5,'extra',NULL,'missing',filename,digest,byte_size FROM prepared_album_note_files LIMIT 1")
        elif failure == 'omitted':
            store.conn.execute('DELETE FROM prepared_album_note_files WHERE position=0')
        elif failure == 'time':
            store.conn.execute('UPDATE prepared_album_note_generations SET built_at=0')
        elif failure == 'original':
            Path(header(store.conn)['filename']).write_text('{}')
        elif failure == 'file':
            Path(store.conn.execute('SELECT filename FROM prepared_album_note_files LIMIT 1').fetchone()[0]).unlink()
    with pytest.raises((MigrationError, ValueError, OSError)):
        publication.publish(store.conn, revision=revision)
    assert publication.selected(store.conn) is None
    regular.migrate(store.conn)


@pytest.mark.parametrize('installed', [83], indirect=True)
def test_large_map_resume_snapshot_real_writer_and_custody(installed):
    store, _ = installed
    value = {str(position):{'description':'Observed text','positions':list(range(8192)) if position == 0 else [2,2,1]} for position in range(128)}
    raw = seed(store, value)
    preparation.JOB.install(store.conn)
    first = preparation.JOB.step(store.conn, batch_size=8)
    assert first.processed == 8 and first.status == 'copying'
    reopened = Store(store._db_path)
    try:
        resumed = preparation.JOB.step(reopened.conn, batch_size=8)
        assert resumed.processed == 16
        while resumed.status != 'prepared':
            resumed = preparation.JOB.step(reopened.conn, batch_size=8)
    finally:
        reopened.conn.close()
    publication.publish(store.conn, revision=resumed.prepared_revision)
    assert store.rec_proposals.get_proposals('album_notes') == value
    assert preparation.read_original(header(store.conn)) == raw
    old = header(store.conn)['generation']
    with store.read_snapshot() as snapshot:
        assert snapshot.store.rec_proposals.get_proposals('album_notes') == value
        store.rec_proposals.publish_album_note('0', {'description':'Continuing result'}, 200)
        assert snapshot.store.rec_proposals.get_proposals('album_notes') == value
        assert not snapshot.publish(lambda:None)
    assert publication.selected(store.conn) is None
    current = store.rec_proposals.get_proposals('album_notes')
    assert current['0'] == {'description':'Continuing result'} and len(current) == 128
    publication.publish(store.conn, revision=prepared(store.conn))
    assert store.rec_proposals.get_proposals('album_notes') == current
    assert store.conn.execute('SELECT count(*) FROM prepared_album_note_files WHERE generation=?', (old,)).fetchone()[0] == 128
    for sql in ['DELETE FROM prepared_album_note_files', 'UPDATE prepared_album_note_generations SET built_at=0',
            'INSERT INTO prepared_album_note_files SELECT generation,0,\'forbidden\',album_provider_id,unresolved_reason,filename,digest,byte_size FROM prepared_album_note_files LIMIT 1']:
        with pytest.raises(sqlite3.IntegrityError, match='retained'):
            with store.conn:
                store.conn.execute(sql)
    assert files.collect_orphans(store.conn) == 0


@pytest.mark.parametrize('installed', [83], indirect=True)
@pytest.mark.parametrize('when', ['during','after','empty-after'])
def test_continuing_sources_and_reference_races_reject_stale_publication(installed, when, monkeypatch):
    store, _ = installed
    if when != 'empty-after':
        seed(store)
    revision = prepared(store.conn)
    other = sqlite3.connect(store._db_path)
    changed = False
    def change():
        nonlocal changed
        if not changed:
            changed = True
            with other:
                if when == 'empty-after':
                    other.execute("INSERT INTO rec_proposals VALUES ('album_notes',?,0)", (json.dumps(NOTES),))
                else:
                    other.execute('UPDATE album_provider_ids SET is_primary=is_primary')
    read_file, verify = files._read, publication.verify
    def during(*args, **kwargs):
        assert not store.conn.in_transaction
        result = read_file(*args, **kwargs)
        change()
        return result
    def after(*args):
        result = verify(*args)
        change()
        return result
    monkeypatch.setattr(files, '_read', during if when == 'during' else read_file)
    monkeypatch.setattr(publication, 'verify', verify if when == 'during' else after)
    try:
        with pytest.raises(MigrationError, match='changed|reconciled|differs'):
            publication.publish(store.conn, revision=revision)
    finally:
        other.close()
    assert publication.selected(store.conn) is None


@pytest.mark.parametrize('installed', [83], indirect=True)
def test_interrupted_cutover_missing_files_recovery_and_failed_job_do_not_block_ddl(installed, monkeypatch):
    store, _ = installed
    raw = seed(store)
    revision = prepared(store.conn)
    publication.install(store.conn)
    with store.conn:
        store.conn.execute('CREATE TRIGGER controlled_note_cutover BEFORE INSERT ON '+publication.HEAD+
            " BEGIN SELECT RAISE(ABORT,'controlled note interruption'); END")
    with pytest.raises(sqlite3.IntegrityError, match='controlled note interruption'):
        publication.publish(store.conn, revision=revision)
    assert not store.conn.execute('SELECT 1 FROM '+publication.GENERATIONS).fetchone()
    with store.conn:
        store.conn.execute('DROP TRIGGER controlled_note_cutover')
    publication.publish(store.conn, revision=revision)
    before = {owner:list(map(tuple, store.conn.execute('SELECT * FROM '+owner))) for owner in (preparation.HEAD,preparation.FILES)}
    Path(header(store.conn)['filename']).unlink()
    Path(store.conn.execute('SELECT filename FROM '+preparation.FILES+' LIMIT 1').fetchone()[0]).unlink()
    store.init_schema(defer_metadata=True)
    with pytest.raises(OSError):
        publication.publish(store.conn, revision=revision)
    publication.repair(store.conn)
    assert {owner:list(map(tuple, store.conn.execute('SELECT * FROM '+owner))) for owner in before} == before
    publication.publish(store.conn, revision=revision)
    assert preparation.read_original(header(store.conn)) == raw
    store.rec_proposals.put_proposals('album_notes', {'changed':{}}, 200)
    publish_file = files.publish
    def fail(*args):
        raise OSError('controlled note file failure')
    monkeypatch.setattr(files, 'publish', fail)
    with pytest.raises(OSError, match='controlled note file failure'):
        preparation.JOB.step(store.conn)
    failed = preparation.JOB.progress(store.conn)
    def install(conn, context):
        conn.execute('CREATE TABLE independent_note_publication(value TEXT)')
    def validate(conn):
        assert conn.execute("SELECT 1 FROM sqlite_schema WHERE name='independent_note_publication'").fetchone()
    monkeypatch.setattr(regular, 'CHANGES', (*regular.CHANGES,regular.Change(len(regular.CHANGES) + 1,'independent_note_publication',install,validate)))
    regular.migrate(store.conn)
    assert preparation.JOB.progress(store.conn) == failed
    with pytest.raises(MigrationError, match='retry explicitly'):
        preparation.JOB.step(store.conn)
    monkeypatch.setattr(files, 'publish', publish_file)
    preparation.JOB.step(store.conn, retry=True)
    publication.publish(store.conn, revision=finish(store.conn).prepared_revision)
    assert store.rec_proposals.get_proposals('album_notes') == {'changed':{}}


@pytest.mark.parametrize('installed', [83], indirect=True)
@pytest.mark.parametrize('value', [None,{}])
def test_absent_and_empty_sources_select_native_empty_output_without_losing_presence(installed, value):
    store, _ = installed
    if value is not None:
        seed(store, value)
    publication.publish(store.conn, revision=prepared(store.conn))
    assert store.rec_proposals.get_proposals('album_notes') is None
    assert not store.rec_proposals.has_proposals('album_notes')
    assert header(store.conn)['source_present'] == int(value is not None)


def test_frozen_native_upgrade_keeps_retained_files_and_guard_validation(tmp_path, _pristine_installed_schema):
    store = Store(tmp_path/'upgrade.db')
    try:
        _pristine_installed_schema(83).backup(store.conn)
        store.init_schema(defer_metadata=True)
        seed(store)
        publication.publish(store.conn, revision=prepared(store.conn))
        original = header(store.conn)
        store.conn.close()
        store = Store(tmp_path/'upgrade.db')
        runner.migrate(store.conn, registry=registry.MIGRATIONS)
        store.init_schema(defer_metadata=True)
        assert publication.selected(store.conn) is None
        assert store.rec_proposals.get_proposals('album_notes') == NOTES
        assert preparation.read_original(original) is not None
        regular.validate(store.conn)
        with store.conn:
            store.conn.execute('DROP TRIGGER prepared_album_note_files_published_delete')
        with pytest.raises(MigrationError, match='publication object'):
            regular.validate(store.conn)
    finally:
        store.conn.close()
