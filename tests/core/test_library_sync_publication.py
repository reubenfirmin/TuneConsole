"""Sync report and stamp cutover keeps exact originals and continuing writes."""
import json
from pathlib import Path
import sqlite3

import pytest

from tests.core.test_predecessor_writes import consumer_installed as installed
from tests.core.test_library_sync_preparation import finish, head, seed
from tests.store_fixture_support import publication_consumer_reads
from yt_playlist.core import schema_inspection, sync_state
from yt_playlist.core.migrations import MigrationError, regular
from yt_playlist.core.preparation import library_sync as preparation, library_sync_publication as publication
from yt_playlist.core.store import Store
from yt_playlist.library import sync_outcomes


pytestmark = pytest.mark.usefixtures('publication_consumer_reads')


@pytest.fixture(autouse=True)
def disable_periodic_library_sync():
    """Sync SQL and file publication do not start the HTTP lifecycle."""


def test_long_report_input_keeps_installed_history_and_snapshot_guards(_bounded_installed_schema):
    source = _bounded_installed_schema(93).source
    assert source.conn.execute('SELECT max(version) FROM schema_migrations').fetchone()[0] == 93
    assert source._snapshot_revisions.conn is source.conn
    assert not source.conn.execute('PRAGMA foreign_key_check').fetchone()


def prepare(conn, owner):
    owner.worker.install(conn)
    return finish(owner.worker, conn, size=3 if owner is publication.STAMPS else 128).prepared_revision


def rejection_inputs(store, *, owner):
    seed(store)
    revision = prepare(store.conn, owner)
    assert owner.selected(store.conn) is None
    return revision


@pytest.mark.parametrize('installed', [79, 93], indirect=True)
def test_preopened_and_reopened_readers_find_exact_report_and_stamp_spelling(installed):
    store, _ = installed
    raw = seed(store)
    sources = dict(store.conn.execute('SELECT key,value FROM settings WHERE key IN (?,?,?,?)',
                                    (sync_state.KEY, *sync_state.STAMPS)))
    other = Store(store._db_path)
    try:
        other.init_schema(defer_metadata=True)
        for owner in publication.OWNERS.values():
            revision = prepare(store.conn, owner)
            owner.publish(store.conn, revision=revision)
            owner.publish(store.conn, revision=revision)
            assert owner.selected(other.conn) == revision
        assert {key:other.settings.get_setting(key) for key in sources} == sources
        assert sync_state.read(other.conn) == json.loads(raw)
        assert not sync_state.available(other.conn)
        regular.validate(other.conn)
    finally:
        other.conn.close()
    reopened = Store(store._db_path)
    try:
        reopened.init_schema(defer_metadata=True)
        assert {key:reopened.settings.get_setting(key) for key in sources} == sources
        assert sync_state.read(reopened.conn) == json.loads(raw)
    finally:
        reopened.conn.close()


@pytest.mark.parametrize('installed', [93], indirect=True)
@pytest.mark.parametrize('raw', [None, '', 'null', '{}'])
def test_null_blank_and_empty_report_and_scalar_kinds_are_preserved(installed, raw):
    store, _ = installed
    with store.conn:
        store.conn.executemany('INSERT INTO settings VALUES (?,?)',
            [(sync_state.KEY, raw), ('last_sync_at', None), ('last_sync_attempt_at', '')])
    for owner in publication.OWNERS.values():
        owner.publish(store.conn, revision=prepare(store.conn, owner))
    assert store.settings.get_setting(sync_state.KEY, 'absent') == raw
    assert sync_state.read(store.conn) == (json.loads(raw) if raw else {})
    assert store.settings.get_setting('last_sync_at', 'absent') is None
    assert store.settings.get_setting('last_sync_attempt_at', 'absent') == ''
    assert store.settings.get_setting('last_sync_outcome', 'absent') == 'absent'
    preparation.RESULT_JOB.step(store.conn, retry=True)
    assert store.settings.get_setting(sync_state.KEY, 'absent') == raw


@pytest.mark.parametrize('installed', [93], indirect=True)
def test_continuing_result_stamp_and_reference_writes_withdraw_independently(installed):
    store, _ = installed
    raw = seed(store)
    for owner in publication.OWNERS.values():
        owner.publish(store.conn, revision=prepare(store.conn, owner))
    retained = head(store.conn)
    with store.conn:
        store.conn.execute("UPDATE identities SET label='Renamed' WHERE id=1")
    assert publication.RESULT.selected(store.conn) is None
    assert publication.STAMPS.selected(store.conn) is not None
    publication.RESULT.publish(store.conn, revision=prepare(store.conn, publication.RESULT))
    store.settings.set_setting('last_sync_at', '10.500')
    assert publication.RESULT.selected(store.conn) is not None
    assert publication.STAMPS.selected(store.conn) is None
    publication.STAMPS.publish(store.conn, revision=prepare(store.conn, publication.STAMPS))
    with store.read_snapshot() as snapshot:
        assert snapshot.store.settings.get_setting('last_sync_at') == '10.500'
        store.settings.delete_setting('last_sync_at')
        assert not snapshot.publish(lambda:None)
    publication.STAMPS.publish(store.conn, revision=prepare(store.conn, publication.STAMPS))
    assert store.settings.get_setting('last_sync_at', 'absent') == 'absent'
    assert publication.STAMPS.value(store.conn, 'last_sync_at', 0) == ' 10.50 '
    assert preparation.read_original(store.conn, retained) == raw
    assert not store.conn.execute('SELECT 1 FROM identities WHERE id=999').fetchone()
    assert not store.conn.execute("SELECT 1 FROM main.playlist_scopes WHERE provider_id='missing-playlist'").fetchone()
    sync_outcomes.begin_attempt(store, 25)
    assert publication.RESULT.selected(store.conn) is not None
    assert publication.STAMPS.selected(store.conn) is None
    sync_outcomes.finish_attempt(store, json.loads(raw), 25)
    assert publication.RESULT.selected(store.conn) is None
    assert sync_state.read(store.conn)['last_attempt_at'] == 25
    for owner in publication.OWNERS.values():
        owner.publish(store.conn, revision=prepare(store.conn, owner))
    assert store.settings.get_setting('last_sync_attempt_at') == '25'


@pytest.mark.parametrize('installed', [93], indirect=True)
@pytest.mark.parametrize('name', tuple(publication.OWNERS))
def test_absent_sources_failed_work_and_regular_ddl_are_independent(installed, name):
    store, _ = installed
    for owner in publication.OWNERS.values():
        owner.publish(store.conn, revision=prepare(store.conn, owner))
    assert sync_state.read(store.conn) == {}
    assert store.settings.get_setting(sync_state.KEY, 'absent') == 'absent'
    assert all(store.settings.get_setting(key, 'absent') == 'absent' for key in sync_state.STAMPS)
    owner = publication.OWNERS[name]
    source = sync_state.KEY if name == 'result' else 'last_sync_at'
    store.settings.set_setting(source, '{' if name == 'result' else 'not-a-time')
    with pytest.raises(ValueError):
        finish(owner.worker, store.conn, size=3)
    failed = owner.worker.progress(store.conn)
    assert failed.status == 'failed'
    regular.migrate(store.conn)
    assert owner.worker.progress(store.conn) == failed
    other = next(value for value in publication.OWNERS.values() if value is not owner)
    assert other.selected(store.conn) == 0
    store.settings.set_setting(source, '{}' if name == 'result' else '5')
    owner.worker.step(store.conn, retry=True)
    owner.publish(store.conn, revision=finish(owner.worker, store.conn, size=3).prepared_revision)
    assert owner.selected(store.conn) is not None


@pytest.mark.parametrize('installed', [93], indirect=True)
@pytest.mark.parametrize('failure', ['fields', 'member', 'resolution', 'file', 'stamp-fields', 'stamp-original'])
def test_corrupt_preparation_never_selects_an_owner(installed, failure):
    store, _ = installed
    owner = publication.STAMPS if failure.startswith('stamp-') else publication.RESULT
    revision = rejection_inputs(store, owner=owner)
    with store.conn:
        if failure == 'fields':
            store.conn.execute('UPDATE '+preparation.HEAD+" SET phase='complete'")
        elif failure == 'member':
            store.conn.execute('DELETE FROM '+preparation.CURSORS+' WHERE position=1')
        elif failure == 'resolution':
            store.conn.execute('UPDATE '+preparation.CURSORS+" SET playlist_scope_id=NULL,unresolved_scope_reason='missing_scope' WHERE position=2")
        elif failure == 'file':
            Path(store.conn.execute('SELECT filename FROM '+preparation.PARTS+' LIMIT 1').fetchone()[0]).write_bytes(b'corrupt')
        elif failure == 'stamp-fields':
            store.conn.execute('UPDATE '+preparation.STAMPS+" SET timestamp=50 WHERE source='last_sync_at'")
        else:
            store.conn.execute('UPDATE '+preparation.ORIGINAL_STAMPS+" SET raw_value='wrong' WHERE source='last_sync_at'")
    with pytest.raises((MigrationError, ValueError, OSError)):
        owner.publish(store.conn, revision=revision)
    assert owner.selected(store.conn) is None


@pytest.mark.parametrize('installed', [93], indirect=True)
@pytest.mark.parametrize('mutation', ['source', 'members', 'original'])
def test_stamp_changes_after_verification_reject_publication(installed, monkeypatch, mutation):
    store, _ = installed
    seed(store)
    owner = publication.STAMPS
    revision = prepare(store.conn, owner)
    other = sqlite3.connect(store._db_path)
    verify = owner.verify
    def verified(conn, revision):
        proof = verify(conn, revision)
        with other:
            if mutation == 'source':
                other.execute("UPDATE settings SET value='30' WHERE key='last_sync_at'")
            elif mutation == 'members':
                other.execute('UPDATE '+preparation.STAMPS+" SET timestamp=30 WHERE source='last_sync_at'")
            else:
                other.execute('UPDATE '+preparation.ORIGINAL_STAMPS+" SET raw_value='30' WHERE source='last_sync_at'")
        return proof
    monkeypatch.setattr(owner, 'verify', verified)
    try:
        with pytest.raises(MigrationError, match='changed|reconciled'):
            owner.publish(store.conn, revision=revision)
        assert owner.selected(store.conn) is None
    finally:
        other.close()


@pytest.mark.parametrize('installed', [93], indirect=True)
def test_stamp_publication_rollback_immutable_custody_and_same_original_reconciliation(installed):
    store, _ = installed
    seed(store)
    owner = publication.STAMPS
    revision = prepare(store.conn, owner)
    def interrupted():
        raise RuntimeError('controlled interruption after stamp rows')
    with pytest.raises(RuntimeError, match='after stamp rows'):
        owner.publish(store.conn, revision=revision, clock=interrupted)
    assert not store.conn.execute('SELECT 1 FROM '+publication.VALUES).fetchone()
    assert not store.conn.execute('SELECT 1 FROM '+publication.GENERATIONS).fetchone()
    owner.publish(store.conn, revision=revision)
    for owner_table in (publication.VALUES, publication.GENERATIONS, preparation.ORIGINAL_STAMPS):
        with pytest.raises(sqlite3.IntegrityError, match='retained|future cleanup'):
            with store.conn:
                store.conn.execute('DELETE FROM '+owner_table)
    with pytest.raises(sqlite3.IntegrityError, match='retained'):
        with store.conn:
            store.conn.execute('INSERT OR REPLACE INTO '+preparation.ORIGINAL_STAMPS+
                               ' SELECT source,digest,raw_value FROM '+preparation.ORIGINAL_STAMPS)
    # Reconciliation can reuse a retained original; it does not attempt another
    # insert or mutate its immutable archived text.
    with store.conn:
        store.conn.execute("UPDATE settings SET value=value WHERE key='last_sync_at'")
    owner.publish(store.conn, revision=prepare(store.conn, owner))
    assert store.settings.get_setting('last_sync_at') == ' 10.50 '
    store.settings.set_setting('last_sync_at', '11')
    owner.publish(store.conn, revision=prepare(store.conn, owner))
    store.settings.set_setting('last_sync_at', ' 10.50 ')
    owner.publish(store.conn, revision=prepare(store.conn, owner))
    assert store.settings.get_setting('last_sync_at') == ' 10.50 '


@pytest.fixture(scope='module')
def long_report(_bounded_installed_schema, tmp_path_factory):
    template = _bounded_installed_schema(93)
    path = tmp_path_factory.mktemp('long-library-sync-report')/'source.db'
    with template.open(path) as store, schema_inspection.reuse_tables(store.conn):
        value = json.loads(seed(store))
        value['accounts']['1']['resources']['playlists']['items'] = {
            'unseen-'+str(number):{'status':'partial', 'last_attempt_at':9007199254740993, 'cursor':'same'}
            for number in range(2000)}
        raw = json.dumps(value)
        store.settings.set_setting(sync_state.KEY, raw)
        yield dict(store=store, value=value, raw=raw)


def test_long_report_source_keeps_two_thousand_distinct_unresolved_items_and_exact_clocks(long_report):
    items = long_report['value']['accounts']['1']['resources']['playlists']['items']
    assert set(items) == {'unseen-'+str(number) for number in range(2000)}
    assert all(item['last_attempt_at'] == 9007199254740993 for item in items.values())
    assert long_report['store'].settings.get_setting(sync_state.KEY) == long_report['raw']


@pytest.fixture(scope='module')
def prepared_long_report(long_report):
    long_report['revision'] = prepare(long_report['store'].conn, publication.RESULT)
    return long_report


def test_long_report_preparation_retains_every_unresolved_reference(prepared_long_report):
    conn = prepared_long_report['store'].conn
    assert conn.execute('SELECT count(*) FROM '+preparation.CURSORS+
        " WHERE raw_playlist_id LIKE 'unseen-%' AND unresolved_scope_reason='missing_scope'").fetchone()[0] == 2000
    assert preparation.read_original(conn, head(conn)) == prepared_long_report['raw']
    assert publication.RESULT.selected(conn) is None


@pytest.fixture(scope='module')
def published_long_report(prepared_long_report):
    state = prepared_long_report
    publication.RESULT.publish(state['store'].conn, revision=state['revision'])
    return state


def test_long_report_publication_selects_the_actual_prepared_revision(published_long_report):
    assert publication.RESULT.selected(published_long_report['store'].conn) == published_long_report['revision']


def test_long_report_repeated_reference_queries_and_missing_file_retry(published_long_report):
    store = published_long_report['store']
    assert sync_state.read(store.conn) == published_long_report['value']
    with pytest.raises(sqlite3.IntegrityError, match='retained'):
        with store.conn:
            store.conn.execute('DELETE FROM '+preparation.CURSORS)
    Path(store.conn.execute('SELECT filename FROM '+preparation.PARTS+' LIMIT 1').fetchone()[0]).unlink()
    with pytest.raises(FileNotFoundError):
        sync_state.read(store.conn)
    preparation.RESULT_JOB.step(store.conn, retry=True)
    assert store.settings.get_setting(sync_state.KEY) == published_long_report['raw']
