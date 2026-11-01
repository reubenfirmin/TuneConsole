"""Canonical listening cutover against independently upgraded genuine sources."""
from contextlib import closing
import json
import sqlite3

import pytest

from tests.core.test_listening_preparation import connect, finish, populated
from tests.core.test_predecessor_writes import installed
from yt_playlist.core import entity_names
from yt_playlist.core.migrations import MigrationError, registry, regular, runner
from yt_playlist.core.preparation import listening_subjects as preparation, subject_publication as publication
from yt_playlist.core.store import Store


def ready(conn, size=1):
    preparation.install(conn)
    finish(conn, batch_size=size)
    for _ in range(1000):
        result = publication.step(conn, batch_size=size)
        if result.complete:
            return result.id
    pytest.fail('Listening publication did not finish bounded fixture')


def projection(conn, relation=lambda name:name):
    result = {}
    for name in publication.READS:
        if name == 'play_event_subjects':
            fields = ','.join(field for field in (*preparation.FIELDS['events'], 'identity_key') if field != 'membership_ref_id')
        elif name in publication.KINDS[-2:]:
            fields = ','.join((*publication.KEYS[name], 'first_exact_play_at', 'first_exact_day',
                'first_observed_day', 'first_legacy_unknown_day'))
        else:
            fields = '*'
        result[name] = sorted(map(tuple, conn.execute('SELECT '+fields+' FROM '+relation(name))), key=repr)
    return result


@pytest.mark.parametrize('installed', [79, 99], indirect=True)
def test_preopened_and_new_readers_match_native_precision_and_subjects(installed, tmp_path):
    store, _ = installed
    populated(store)
    other = store.accounts.upsert_account('Other', 'other.json', None, False)
    store.history.record_history_plays(other, 1704672000, ['song 0|artist', 'outside|unknown'], source_timezone='UTC')
    preopened, native = Store(store._db_path), Store(tmp_path / 'native.db')
    try:
        preopened.init_schema(defer_metadata=True)
        store.conn.backup(native.conn)
        runner.migrate(native.conn, registry=registry.MIGRATIONS)
        native.init_schema(defer_metadata=True)
        native.trends.refresh_first_play()
        expected = projection(native.conn)
        with store.read_view() as pinned, connect(store) as job:
            before = list(map(tuple, pinned.conn.execute('SELECT * FROM listening_activity')))
            identity = ready(job)
            publication.publish(job, identity=identity)
            assert publication.selected(job) == identity
            assert list(map(tuple, pinned.conn.execute('SELECT * FROM listening_activity'))) == before
        # This DAO was opened before publication existed. Its read boundary
        # installs explicit read projections; masked old payloads prove selection.
        def mask(operation, owner, column, *unused):
            return sqlite3.SQLITE_IGNORE if operation == sqlite3.SQLITE_READ and owner in preparation.SOURCES.values() else sqlite3.SQLITE_OK
        preopened.conn.set_authorizer(mask)
        try:
            assert preopened.trends.first_play_map('track') == native.trends.first_play_map('track')
            assert projection(preopened.conn) == expected
        finally:
            preopened.conn.set_authorizer(None)
        with store.read_view() as reader:
            assert projection(reader.conn) == expected
            prepared = reader.trends.prepare_first_play()
            assert prepared['predecessor_first_play']
            assert projection(reader.conn) == expected
        with connect(store) as job:
            for kind, source in preparation.SOURCES.items():
                for row in job.execute('SELECT id,source_snapshot FROM '+publication.table(kind)+' WHERE stage=?', (identity,)):
                    assert json.loads(row['source_snapshot']) == dict(job.execute('SELECT * FROM main.'+source+' WHERE id=?', (row['id'],)).fetchone())
            assert not job.execute('SELECT 1 FROM '+publication.table('events')+' WHERE membership_ref_id IS NOT NULL').fetchone()
            assert not job.execute('PRAGMA foreign_key_check').fetchone()
    finally:
        preopened.conn.close()
        native.conn.close()


@pytest.mark.parametrize('installed', [79, 99], indirect=True)
def test_continuing_capture_and_source_removal_withdraw_and_reconcile(installed):
    store, _ = installed
    account = populated(store)
    with connect(store) as job:
        identity = ready(job)
        publication.publish(job, identity=identity)
        retained = projection(job, lambda name:publication.table('read_'+name))
    store.history.record_play_event(account, 'new|unresolved', 'unknown-upload', 1705000000, source_timezone='UTC')
    with connect(store) as job:
        assert publication.selected(job) is None
        assert job.execute('SELECT count(*) FROM '+publication.table('events')+' WHERE stage=?', (identity,)).fetchone()[0] == 3
        with pytest.raises(MigrationError, match='reconciled preparation'):
            publication.publish(job, identity=identity)
        newer = ready(job)
        assert newer != identity
        publication.publish(job, identity=newer)
    assert 'new|unresolved' in store.history.get_recent_history_keys(0)
    with store._lock, store.conn:
        from yt_playlist.core.listening import remove_source
        assert store.conn.execute("SELECT 1 FROM main.play_event_sources WHERE source='live'").fetchone()
        remove_source(store.conn, 'live', account)
    with connect(store) as job:
        assert publication.selected(job) is None
        final = ready(job)
        publication.publish(job, identity=final)
        assert job.execute('SELECT count(*) FROM '+publication.table('events')+' WHERE stage=?', (identity,)).fetchone()[0] == 3
        assert retained['play_event_subjects']
        with pytest.raises(sqlite3.IntegrityError, match='future cleanup'):
            job.execute('DELETE FROM '+publication.table('events')+' WHERE stage=?', (identity,))
        job.rollback()
        regular.migrate(job)


@pytest.mark.parametrize('installed', [79], indirect=True)
def test_final_head_interruption_and_reference_race_keep_selection_withdrawn(installed, monkeypatch):
    store, _ = installed
    populated(store)
    with connect(store) as job:
        identity = ready(job)
        with job:
            job.execute('CREATE TRIGGER interrupt_listening_head BEFORE INSERT ON '+publication.table('head')+
                " BEGIN SELECT RAISE(ABORT,'controlled head failure'); END")
        with pytest.raises(sqlite3.IntegrityError, match='controlled head failure'):
            publication.publish(job, identity=identity)
        assert not job.execute('SELECT 1 FROM '+publication.table('generations')).fetchone()
        assert publication.selected(job) is None
        with job:
            job.execute('DROP TRIGGER interrupt_listening_head')
        verify = publication.verify
        def race(conn, stage):
            checked = verify(conn, stage)
            with store._lock, store.conn:
                store.conn.execute("UPDATE main.play_event_calendar SET timezone='Etc/UTC' WHERE event_id=1")
            return checked
        monkeypatch.setattr(publication, 'verify', race)
        with pytest.raises(MigrationError, match='changed after verification'):
            publication.publish(job, identity=identity)
        assert publication.selected(job) is None
        monkeypatch.setattr(publication, 'verify', verify)
        publication.publish(job, identity=ready(job))


@pytest.mark.parametrize('installed', [79], indirect=True)
def test_source_snapshot_corruption_cannot_be_selected(installed):
    store, _ = installed
    populated(store)
    with connect(store) as job:
        preparation.install(job)
        finish(job)
        with job:
            job.execute("UPDATE prepared_listening_events SET source_snapshot='{}' WHERE id=1")
        identity = ready(job)
        with pytest.raises(MigrationError, match='original differs'):
            publication.publish(job, identity=identity)
        assert publication.selected(job) is None


@pytest.mark.parametrize('installed', [79], indirect=True)
@pytest.mark.parametrize('damage', ['missing', 'extra', 'typed'])
def test_independent_source_check_rejects_incomplete_or_wrong_preparation(installed, damage):
    store, _ = installed
    populated(store)
    with connect(store) as job:
        preparation.install(job)
        finish(job)
        with job:
            if damage == 'missing':
                job.execute('DELETE FROM prepared_listening_events WHERE id=1')
            elif damage == 'extra':
                row = dict(job.execute('SELECT * FROM prepared_listening_events WHERE id=1').fetchone())
                row['id'] = -12
                job.execute('INSERT INTO prepared_listening_events('+','.join(row)+') VALUES ('+
                    ','.join('?' for _ in row)+')', tuple(row.values()))
            else:
                job.execute("UPDATE prepared_listening_events SET raw_identity_key='fabricated capture' WHERE id=1")
        identity = ready(job)
        with pytest.raises(MigrationError, match='original differs|omits installed'):
            publication.publish(job, identity=identity)
        assert publication.selected(job) is None


@pytest.mark.parametrize('installed', [79], indirect=True)
def test_interrupted_batch_keeps_checkpoint_and_can_resume_after_reopen(installed):
    store, _ = installed
    populated(store)
    with connect(store) as job:
        preparation.install(job)
        finish(job)
        first = publication.step(job, batch_size=1)
        with job:
            job.execute('CREATE TRIGGER interrupt_listening_batch BEFORE INSERT ON '+publication.table('events')+
                " WHEN NEW.id=2 BEGIN SELECT RAISE(ABORT,'controlled batch failure'); END")
        with pytest.raises(sqlite3.IntegrityError, match='controlled batch failure'):
            publication.step(job, batch_size=2)
        assert publication.stage(job, first.id) == first
        regular.migrate(job)
        with job:
            job.execute('DROP TRIGGER interrupt_listening_batch')
    store.settings.set_setting('between-listening-batches', 'ordinary write')
    with connect(store) as job:
        identity = ready(job)
        assert identity == first.id
        publication.publish(job, identity=identity)
    assert store.settings.get_setting('between-listening-batches') == 'ordinary write'


@pytest.mark.parametrize('installed', [79], indirect=True)
def test_slow_verification_allows_real_capture_and_rejects_stale_selection(installed, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    import threading
    store, _ = installed
    account = populated(store)
    with connect(store) as job:
        identity = ready(job)
    entered, release = threading.Event(), threading.Event()
    original = publication.bounded_rows
    def slow(conn, *args, **kwargs):
        if not entered.is_set():
            entered.set()
            assert release.wait(10)
        return original(conn, *args, **kwargs)
    def publish():
        with connect(store) as job:
            publication.publish(job, identity=identity)
    monkeypatch.setattr(publication, 'bounded_rows', slow)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(publish)
        assert entered.wait(3)
        try:
            store.history.record_play_event(account, 'during|verification', None, 1706000000, source_timezone='UTC')
            regular.migrate(store.conn)
        finally:
            release.set()
        with pytest.raises(MigrationError, match='reconciled preparation|changed during verification'):
            future.result(5)
    monkeypatch.setattr(publication, 'bounded_rows', original)
    with connect(store) as job:
        publication.publish(job, identity=ready(job))


@pytest.mark.parametrize('installed', [79], indirect=True)
def test_oversized_source_fails_durably_without_blocking_regular_schema(installed):
    store, _ = installed
    populated(store)
    with connect(store) as job:
        preparation.install(job)
        with store.conn:
            store.conn.execute('UPDATE main.play_events SET raw_identity_key=? WHERE id=1', ('x'*(preparation.MAX_ROW_BYTES+1),))
        with pytest.raises(MigrationError, match='byte budget'):
            preparation.step(job)
        failed = preparation.progress(job)
        assert failed.status == 'failed'
        regular.migrate(job)
        assert preparation.progress(job) == failed
        with pytest.raises(MigrationError, match='retry explicitly'):
            preparation.step(job)
        with store.conn:
            store.conn.execute("UPDATE main.play_events SET raw_identity_key='retained capture' WHERE id=1")
        preparation.step(job, retry=True)
        publication.publish(job, identity=ready(job))


@pytest.mark.parametrize('installed', [99], indirect=True)
def test_isolated_native_upgrade_keeps_retained_custody_and_prefers_native(installed, tmp_path):
    store, _ = installed
    populated(store)
    with connect(store) as job:
        identity = ready(job)
        publication.publish(job, identity=identity)
        retained = [tuple(row) for row in job.execute('SELECT * FROM '+publication.table('events'))]
        with closing(sqlite3.connect(tmp_path / 'upgraded.db')) as upgraded:
            upgraded.row_factory = sqlite3.Row
            upgraded.execute('PRAGMA foreign_keys=ON')
            job.backup(upgraded)
            runner.migrate(upgraded, registry=registry.MIGRATIONS)
            regular.migrate(upgraded)
            assert publication.selected(upgraded) is None
            assert [tuple(row) for row in upgraded.execute('SELECT * FROM '+publication.table('events'))] == retained
