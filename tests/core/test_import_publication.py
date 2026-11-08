"""Published import reports keep account attribution, fields and evidence intact."""
from dataclasses import replace
import json
from pathlib import Path
import sqlite3

import pytest

from tests.core.test_predecessor_writes import installed
from tests.independent_schema_support import independent_regular_ddl
from tests.core.test_collection_document_preparation import finish, seed
from yt_playlist.core import entity_names, spotify_job_state
from yt_playlist.core.migrations import MigrationError, regular
from yt_playlist.core.preparation import collection_documents as preparation, import_publication as publication
from yt_playlist.core.store import Store
from yt_playlist.web import spotify_import


def prepare(conn):
    publication.OWNER.worker.install(conn)
    return finish(publication.OWNER.worker, conn).prepared_revision


def rejection_inputs(store):
    _, raw, _, _ = seed(store)
    revision = prepare(store.conn)
    assert publication.OWNER.selected(store.conn) is None
    return raw, revision


@pytest.mark.parametrize('installed', [79, 93, 96], indirect=True)
def test_preopened_and_reopened_readers_use_exact_reports_and_original_account(installed):
    store, _ = installed
    value, raw, _, _ = seed(store)
    other = Store(store._db_path)
    try:
        other.init_schema(defer_metadata=True)
        publication.OWNER.publish(store.conn, revision=prepare(store.conn))
        assert publication.OWNER.selected(other.conn) == 0
        assert other.settings.get_setting(spotify_job_state.KEY) == raw
        assert spotify_import.load_job(other) == entity_names.account_document(value)
        assert not spotify_job_state.available(other.conn)
        regular.validate(other.conn)
    finally:
        other.conn.close()
    reopened = Store(store._db_path)
    try:
        reopened.init_schema(defer_metadata=True)
        assert spotify_import.load_job(reopened) == entity_names.account_document(value)
    finally:
        reopened.conn.close()


@pytest.mark.parametrize('installed', [79], indirect=True)
@pytest.mark.parametrize('raw', ['absent', None, '', 'null'])
def test_missing_null_and_blank_reports_remain_distinct(installed, raw):
    store, _ = installed
    if raw != 'absent':
        with store.conn:
            store.conn.execute('INSERT INTO settings(key,value) VALUES (?,?)', (spotify_job_state.KEY, raw))
    publication.OWNER.publish(store.conn, revision=prepare(store.conn))
    assert store.settings.get_setting(spotify_job_state.KEY, 'missing') == ('missing' if raw == 'absent' else raw)
    assert spotify_import.load_job(store) is None


@pytest.mark.parametrize('installed', [93], indirect=True)
@pytest.mark.parametrize('kind', ['missing', 'null', 'unresolved'])
def test_account_field_presence_and_unresolved_identity_do_not_admit_accounts(installed, kind):
    store, _ = installed
    value = {'status':'done', 'finished_at':None, 'report':{'count':0}, 'preview_token':None}
    if kind != 'missing':
        value['identity_id'] = 999 if kind == 'unresolved' else None
    raw = json.dumps(value)
    store.settings.set_setting(spotify_job_state.KEY, raw)
    before = list(map(tuple, store.conn.execute('SELECT * FROM main.identities')))
    publication.OWNER.publish(store.conn, revision=prepare(store.conn))
    assert spotify_import.load_job(store) == entity_names.account_document(value)
    header = publication.OWNER.capture(store.conn)
    assert header['account_present'] == (kind != 'missing')
    assert header['account_id'] is None
    assert header['unresolved_account_id'] == (999 if kind == 'unresolved' else None)
    assert before == list(map(tuple, store.conn.execute('SELECT * FROM main.identities')))


@pytest.mark.parametrize('installed', [93], indirect=True)
@pytest.mark.parametrize('failure', ['fields', 'account', 'recovery', 'original', 'plan'])
def test_unverified_fields_or_files_never_select_a_report(installed, staged_custody, failure):
    store, _ = installed
    raw, revision = staged_custody(installed, 'spotify-import', rejection_inputs)
    header = publication.OWNER.capture(store.conn)
    with store.conn:
        if failure == 'fields':
            store.conn.execute('UPDATE prepared_spotify_import_generations SET track_entries=12')
        elif failure == 'account':
            store.conn.execute('UPDATE prepared_spotify_import_generations SET account_id=NULL,unresolved_account_id=999')
        else:
            filename = (header[failure+'_filename'] if failure != 'original' else
                        store.conn.execute('SELECT filename FROM prepared_spotify_import_parts LIMIT 1').fetchone()[0])
            Path(filename).write_bytes(b'corrupt')
    with pytest.raises((MigrationError, ValueError, OSError)):
        publication.OWNER.publish(store.conn, revision=revision)
    assert publication.OWNER.selected(store.conn) is None
    assert store.settings.get_setting(spotify_job_state.KEY) == raw


@pytest.mark.parametrize('installed', [93], indirect=True)
@pytest.mark.parametrize('timing', ['files', 'rows'])
def test_source_or_custody_changes_during_verification_cannot_publish_stale_reports(installed, timing):
    store, _ = installed
    seed(store)
    owner = publication.OWNER
    revision = prepare(store.conn)
    other = sqlite3.connect(store._db_path, timeout=.01)
    def files(conn, header, described):
        assert not conn.in_transaction
        result = publication.verify_files(conn, header, described)
        with other:
            other.execute('UPDATE settings SET value=value WHERE key=?', (owner.source,))
        return result
    def rows(conn, header, described):
        publication.verify_rows(conn, header, described)
        with other:
            other.execute('UPDATE prepared_spotify_import_generations SET started_at=started_at+1')
    try:
        target = replace(owner, **({'verify_files':files} if timing == 'files' else {'verify_rows':rows}))
        with pytest.raises(MigrationError, match='changed|reconciled'):
            target.publish(store.conn, revision=revision)
        assert owner.selected(store.conn) is None
    finally:
        other.close()


@pytest.mark.parametrize('installed', [79, 93], indirect=True)
def test_real_import_worker_keeps_account_spelling_and_reconciles_continuing_updates(installed):
    store, _ = installed
    value, raw, _, _ = seed(store)
    owner = publication.OWNER
    owner.publish(store.conn, revision=prepare(store.conn))
    with store.read_snapshot() as snapshot:
        assert spotify_import.load_job(snapshot.store)['account_id'] == value['identity_id']
        updated = {**spotify_import.load_job(store), 'status':'done', 'finished_at':15}
        spotify_import.save_job(store, updated)
        assert not snapshot.publish(lambda:None)
    assert owner.selected(store.conn) is None
    saved = json.loads(store.settings.get_setting(owner.source))
    assert saved['identity_id'] == value['identity_id'] and 'account_id' not in saved
    with pytest.raises(ValueError, match='competing account fields'):
        store.settings.save_spotify_import_job({**updated, 'identity_id':999})
    assert json.loads(store.settings.get_setting(owner.source)) == saved
    owner.publish(store.conn, revision=prepare(store.conn))
    assert spotify_import.load_job(store)['finished_at'] == 15
    assert owner.value(store.conn, 0) == raw
    store.settings.delete_setting(owner.source)
    assert owner.selected(store.conn) is None
    assert spotify_import.load_job(store) is None
    assert store.settings.get_setting(owner.source, 'absent') == 'absent'
    assert store.conn.execute('SELECT value FROM settings WHERE key=?', (owner.source,)).fetchone() is None
    owner.publish(store.conn, revision=prepare(store.conn))
    assert store.settings.get_setting(owner.source, 'absent') == 'absent'


@pytest.mark.parametrize('installed', [93], indirect=True)
def test_missing_file_explicit_recovery_failure_keeps_regular_ddl_independent(installed, independent_regular_ddl):
    store, _ = installed
    _, raw, _, _ = seed(store)
    owner = publication.OWNER
    owner.publish(store.conn, revision=prepare(store.conn))
    Path(owner.capture(store.conn)['recovery_filename']).unlink()
    def fail(*args):
        assert not store.conn.in_transaction
        raise OSError('controlled import recovery failure')
    with pytest.raises(OSError, match='controlled import recovery failure'):
        replace(owner.worker, repair_files=fail).step(store.conn, retry=True)
    before = owner.worker.progress(store.conn)
    assert before.status == 'failed'
    store.init_schema(defer_metadata=True)
    regular.migrate(store.conn)
    assert owner.worker.progress(store.conn) == before
    with pytest.raises(MigrationError, match='retry explicitly'):
        owner.worker.step(store.conn)
    owner.worker.step(store.conn, retry=True)
    owner.publish(store.conn, revision=owner.worker.progress(store.conn).prepared_revision)
    assert store.settings.get_setting(owner.source) == raw
    with pytest.raises(sqlite3.IntegrityError, match='retained'):
        with store.conn:
            store.conn.execute('DELETE FROM prepared_spotify_import_parts')


@pytest.mark.parametrize('installed', [93], indirect=True)
def test_large_report_rollback_and_numeric_affinity_keep_exact_original(installed):
    store, _ = installed
    value, _, _, _ = seed(store)
    value.update(started_at=9007199254740993, report={'ordered':[{'position':index} for index in range(8192)]})
    raw = json.dumps(value, indent=2)
    store.settings.set_setting(spotify_job_state.KEY, raw)
    owner = publication.OWNER
    revision = prepare(store.conn)
    owner.install(store.conn)
    with store.conn:
        store.conn.execute('CREATE TRIGGER controlled_import_interrupt BEFORE INSERT ON '+owner.tables[1]+
                           " BEGIN SELECT RAISE(ABORT,'controlled interruption'); END")
    with pytest.raises(sqlite3.IntegrityError, match='controlled interruption'):
        owner.publish(store.conn, revision=revision)
    assert not store.conn.execute('SELECT 1 FROM '+owner.tables[0]).fetchone()
    with store.conn:
        store.conn.execute('DROP TRIGGER controlled_import_interrupt')
    owner.publish(store.conn, revision=revision)
    assert store.settings.get_setting(owner.source) == raw
    assert spotify_import.load_job(store)['report'] == value['report']
