"""Listening publication retains exact files, chunk edges and compatible writers."""
import json
from pathlib import Path
import sqlite3
import time

import pytest

from tests.core.test_listening_output_preparation import chunk, head, seed
from tests.core.test_predecessor_writes import installed
from tests.publication_timing_support import publication_costs
from yt_playlist.core import recommendation_outputs as files
from yt_playlist.core.migrations import MigrationError, registry, regular, runner
from yt_playlist.core.preparation import listening_outputs as preparation, listening_publication as publication
from yt_playlist.core.store import Store
from yt_playlist.repos.listening_analytics import ListeningAnalyticsRepo


def prepared(conn, size=128):
    preparation.JOB.install(conn)
    for _ in range(2000):
        result = preparation.JOB.step(conn, batch_size=size)
        if result.status == 'prepared':
            return result
    pytest.fail('Listening preparation did not finish bounded fixture')


def mapped(conn, size=128):
    for _ in range(100):
        stage = publication.OWNER.step(conn, batch_size=size)
        if stage.complete:
            return stage
    pytest.fail('Listening map did not finish bounded fixture')


@pytest.mark.parametrize('installed', [79,89], indirect=True)
def test_preopened_readers_use_exact_verified_files_and_match_native(installed, tmp_path):
    store, version = installed
    documents = seed(store)
    preopened, native = Store(store._db_path), Store(tmp_path/'native.db')
    try:
        preopened.init_schema(defer_metadata=True)
        store.conn.backup(native.conn)
        runner.migrate(native.conn, registry=registry.MIGRATIONS[:90])
        native.init_schema(defer_metadata=True)
        prepared(store.conn)
        stage = mapped(store.conn, 2)
        publication.OWNER.publish(store.conn, stage=stage.id)
        def deny_source(operation, owner, column, *unused):
            return sqlite3.SQLITE_DENY if operation == sqlite3.SQLITE_READ and owner == 'rec_proposals' and column == 'payload' else sqlite3.SQLITE_OK
        with preopened._lock:
            preopened.conn.set_authorizer(deny_source)
            try:
                for surface, raw in documents.items():
                    assert preopened.rec_proposals._proposal_json(surface) == raw
                    assert ListeningAnalyticsRepo(preopened).encoded(surface) == raw
                    assert preopened.rec_proposals.get_proposals(surface) == native.rec_proposals.get_proposals(surface)
                    assert preopened.rec_proposals.has_proposals(surface)
            finally:
                preopened.conn.set_authorizer(None)
        assert not preopened.rec_proposals.has_proposals('listening_chunk:'+'f'*24)
        assert store.conn.execute('SELECT max(version) FROM schema_migrations').fetchone()[0] == version
        regular.validate(preopened.conn)
        assert files.collect_orphans(store.conn) == 0
    finally:
        preopened.conn.close()
        native.conn.close()


@pytest.mark.parametrize('installed', [89], indirect=True)
@pytest.mark.parametrize('failure', ['field','time','reference','missing-reference','original','file','omitted'])
def test_incorrect_headers_references_files_and_source_coverage_cannot_publish(installed, failure):
    store, _ = installed
    seed(store)
    prepared(store.conn)
    with store.conn:
        if failure == 'field':
            store.conn.execute("UPDATE prepared_listening_output_generations SET timezone='other' WHERE surface='listening_analytics'")
        elif failure == 'time':
            store.conn.execute('UPDATE prepared_listening_output_generations SET source_at=0')
        elif failure == 'reference':
            store.conn.execute('UPDATE prepared_listening_chunk_references SET encoded_bytes=1 WHERE position=0')
        elif failure == 'missing-reference':
            store.conn.execute('DELETE FROM prepared_listening_chunk_references WHERE position=0')
        elif failure == 'original':
            Path(head(store.conn)['filename']).write_text('{}')
        elif failure == 'file':
            Path(head(store.conn)['filename']).unlink()
        else:
            store.conn.execute("DELETE FROM prepared_listening_output_heads WHERE surface='listening_edition'")
    stage = mapped(store.conn)
    with pytest.raises((MigrationError, ValueError, OSError)):
        publication.OWNER.publish(store.conn, stage=stage.id)
    assert publication.OWNER.selected(store.conn) is None
    regular.migrate(store.conn)


@pytest.mark.parametrize('installed', [89], indirect=True)
@pytest.mark.parametrize('failure', ['missing-chunk','wrong-size'])
def test_manifest_must_match_chunks_in_the_same_map(installed, failure):
    store, _ = installed
    documents = seed(store)
    value = json.loads(documents['listening_analytics'])
    value['chunks'][0]['key' if failure == 'missing-chunk' else 'bytes'] = 'f'*24 if failure == 'missing-chunk' else 1
    store.rec_proposals.put_proposals('listening_analytics', value, 101)
    prepared(store.conn)
    stage = mapped(store.conn)
    with pytest.raises(MigrationError, match='missing or mismatched'):
        publication.OWNER.publish(store.conn, stage=stage.id)


@pytest.mark.parametrize('installed', [89], indirect=True)
def test_long_edition_bounded_resume_snapshot_continuing_writer_and_custody(installed, publication_costs, record_property):
    def measured(phase, operation, *args, **kwargs):
        started = time.perf_counter()
        try:
            return operation(*args, **kwargs)
        finally:
            record_property(phase + '_seconds', time.perf_counter() - started)

    store, _ = installed
    started = time.perf_counter()
    chunks = dict(chunk(number) for number in range(600))
    refs = [{'key':key, 'from':'2026-01-01', 'until':'2026-01-02', 'bytes':len(raw.encode())} for key,raw in chunks.items()]
    documents = {'listening_analytics':json.dumps({'chunks':refs}), 'listening_edition':'{"text":"Old edition"}',
        **{'listening_chunk:'+key:raw for key,raw in chunks.items()}}
    with store.conn:
        store.conn.executemany('INSERT INTO rec_proposals VALUES (?,?,100)', documents.items())
    record_property('source_documents_seconds', time.perf_counter() - started)
    measured('initial_preparation', prepared, store.conn)
    first = measured('first_map_checkpoint', publication.OWNER.step, store.conn, batch_size=128)
    assert first.copied == 128 and not first.complete
    reopened = measured('reopen', Store, store._db_path)
    try:
        resumed = measured('resumed_map_checkpoint', publication.OWNER.step, reopened.conn, batch_size=128)
        assert resumed.id == first.id and resumed.copied == 256
        stage = measured('remaining_map_drain', mapped, reopened.conn)
        assert stage.copied == 602
        measured('first_publication', publication.OWNER.publish, reopened.conn, stage=stage.id)
    finally:
        reopened.conn.close()
    old_manifest = head(store.conn)['generation']
    started = time.perf_counter()
    with store.read_snapshot() as snapshot:
        assert snapshot.store.rec_proposals._proposal_json('listening_edition') == documents['listening_edition']
        with store.conn:
            ListeningAnalyticsRepo(store).publish('{"text":"Continuing result"}', 200, 'listening_edition')
        assert snapshot.store.rec_proposals._proposal_json('listening_edition') == documents['listening_edition']
        assert not snapshot.publish(lambda:None)
    record_property('continuing_writer_snapshot_seconds', time.perf_counter() - started)
    assert publication.OWNER.selected(store.conn) is None
    measured('second_preparation', prepared, store.conn)
    next_stage = measured('second_map', mapped, store.conn)
    measured('second_publication', publication.OWNER.publish, store.conn, stage=next_stage.id)
    started = time.perf_counter()
    assert head(store.conn)['generation'] == old_manifest
    assert store.rec_proposals.get_proposals('listening_edition') == {'text':'Continuing result'}
    assert publication.OWNER.value(store.conn, stage.id, 'listening_edition') == documents['listening_edition']
    assert store.rec_proposals.get_proposals('listening_analytics') == {'chunks':refs}
    for sql in ['DELETE FROM prepared_listening_chunk_references',
            'UPDATE prepared_listening_output_generations SET source_at=0',
            'DELETE FROM '+publication.OWNER.table('map')]:
        with pytest.raises(sqlite3.IntegrityError, match='(?i)retained'):
            with store.conn:
                store.conn.execute(sql)
    assert files.collect_orphans(store.conn) == 0
    record_property('retained_custody_readback_seconds', time.perf_counter() - started)


@pytest.mark.parametrize('installed', [89], indirect=True)
@pytest.mark.parametrize('when', ['during','after','empty-after'])
def test_source_writes_during_or_after_verification_reject_stale_cutover(installed, when, monkeypatch):
    store, _ = installed
    if when != 'empty-after':
        seed(store)
    prepared(store.conn)
    stage = mapped(store.conn)
    second = sqlite3.connect(store._db_path)
    changed = False
    def change():
        nonlocal changed
        if not changed:
            changed = True
            with second:
                second.execute("INSERT INTO rec_proposals VALUES ('listening_edition','{}',200) ON CONFLICT(surface) DO UPDATE SET payload='{}',built_at=200")
    read_file, verify = files._read, type(publication.OWNER).verify
    def during(*args, **kwargs):
        assert not store.conn.in_transaction
        result = read_file(*args, **kwargs)
        change()
        return result
    def after(self, *args):
        result = verify(self, *args)
        change()
        return result
    monkeypatch.setattr(files, '_read', during if when == 'during' else read_file)
    monkeypatch.setattr(type(publication.OWNER), 'verify', verify if when == 'during' else after)
    try:
        with pytest.raises(MigrationError, match='changed|reconciled|differs'):
            publication.OWNER.publish(store.conn, stage=stage.id)
    finally:
        second.close()
    assert publication.OWNER.selected(store.conn) is None


@pytest.mark.parametrize('installed', [89], indirect=True)
def test_interruptions_missing_file_repair_and_failed_preparation_leave_ddl_independent(installed, monkeypatch):
    store, _ = installed
    documents = seed(store)
    prepared(store.conn)
    publication.OWNER.install(store.conn)
    with store.conn:
        store.conn.execute('CREATE TRIGGER controlled_listening_map BEFORE INSERT ON '+publication.OWNER.table('map')+
            " BEGIN SELECT RAISE(ABORT,'controlled map interruption'); END")
    with pytest.raises(sqlite3.IntegrityError, match='controlled map interruption'):
        publication.OWNER.step(store.conn, batch_size=2)
    assert not store.conn.execute('SELECT 1 FROM '+publication.OWNER.table('stages')).fetchone()
    with store.conn:
        store.conn.execute('DROP TRIGGER controlled_listening_map')
    stage = mapped(store.conn)
    with store.conn:
        store.conn.execute('CREATE TRIGGER controlled_listening_cutover BEFORE INSERT ON '+publication.OWNER.table('head')+
            " BEGIN SELECT RAISE(ABORT,'controlled cutover interruption'); END")
    with pytest.raises(sqlite3.IntegrityError, match='controlled cutover interruption'):
        publication.OWNER.publish(store.conn, stage=stage.id)
    assert not store.conn.execute('SELECT 1 FROM '+publication.OWNER.table('generations')).fetchone()
    with store.conn:
        store.conn.execute('DROP TRIGGER controlled_listening_cutover')
    publication.OWNER.publish(store.conn, stage=stage.id)
    before = {owner:list(map(tuple, store.conn.execute('SELECT * FROM '+owner))) for owner in (preparation.HEAD,preparation.REFS)}
    Path(head(store.conn)['filename']).unlink()
    assert store.rec_proposals.get_proposals('listening_analytics') is None
    assert not store.rec_proposals.has_proposals('listening_analytics')
    store.init_schema(defer_metadata=True)
    with pytest.raises(OSError):
        publication.OWNER.publish(store.conn, stage=stage.id)
    publication.repair(store.conn, 'listening_analytics')
    assert before == {owner:list(map(tuple, store.conn.execute('SELECT * FROM '+owner))) for owner in before}
    publication.OWNER.publish(store.conn, stage=stage.id)
    assert store.rec_proposals._proposal_json('listening_analytics') == documents['listening_analytics']
    store.rec_proposals.put_proposals('listening_edition', {'text':'New'}, 200)
    publish = files.publish
    def fail(*args):
        raise OSError('controlled listening disk failure')
    monkeypatch.setattr(files, 'publish', fail)
    with pytest.raises(OSError, match='controlled listening disk failure'):
        preparation.JOB.step(store.conn)
    failed = preparation.JOB.progress(store.conn)
    def install(conn, context):
        conn.execute('CREATE TABLE independent_listening_publication(value TEXT)')
    def validate(conn):
        assert conn.execute("SELECT 1 FROM sqlite_schema WHERE name='independent_listening_publication'").fetchone()
    monkeypatch.setattr(regular, 'CHANGES', (*regular.CHANGES,regular.Change(len(regular.CHANGES) + 1,'independent_listening_publication',install,validate)))
    regular.migrate(store.conn)
    assert preparation.JOB.progress(store.conn) == failed
    with pytest.raises(MigrationError, match='retry explicitly'):
        preparation.JOB.step(store.conn)
    monkeypatch.setattr(files, 'publish', publish)
    preparation.JOB.step(store.conn, retry=True)
    prepared(store.conn)
    publication.OWNER.publish(store.conn, stage=mapped(store.conn).id)
    assert store.rec_proposals.get_proposals('listening_edition') == {'text':'New'}


def test_native_upgrade_preserves_files_and_requires_retained_custody_guards(
        tmp_path, publication_costs, _pristine_installed_schema):
    store = Store(tmp_path/'upgrade.db')
    try:
        _pristine_installed_schema(89).backup(store.conn)
        store.init_schema(defer_metadata=True)
        documents = seed(store)
        prepared(store.conn)
        publication.OWNER.publish(store.conn, stage=mapped(store.conn).id)
        store.conn.close()
        store = Store(tmp_path/'upgrade.db')
        runner.migrate(store.conn, registry=registry.MIGRATIONS)
        store.init_schema(defer_metadata=True)
        assert publication.OWNER.selected(store.conn) is None
        for surface, raw in documents.items():
            assert store.rec_proposals._proposal_json(surface) == raw
        regular.validate(store.conn)
        with store.conn:
            store.conn.execute('DROP TRIGGER '+preparation.HEAD+'_mapped_delete')
        with pytest.raises(MigrationError, match='Incompatible mapped publication object'):
            regular.validate(store.conn)
    finally:
        store.conn.close()
