"""Runtime receipt selection retains exact recovery and compatible operations."""
from contextlib import contextmanager
from dataclasses import replace
import json
from pathlib import Path
import sqlite3
import time

import pytest

from tests.core.test_workflow_receipt_preparation import seed
from tests.store_fixture_support import (
    _numbered_input, native_sql_calls,
    publication_consumer_reads, without_query_timing,
)
from yt_playlist.core import workflow_settings
from yt_playlist.core.migrations import MigrationError, regular
from yt_playlist.core.preparation import workflow_receipts as preparation, workflow_publication as publication
from yt_playlist.core.store import Store
from yt_playlist.library import playlist_gc_retry
from yt_playlist.library.spotify_library import _playlist_transfers


pytestmark = pytest.mark.usefixtures('native_sql_calls', 'without_query_timing', 'publication_consumer_reads')


@pytest.fixture(autouse=True)
def disable_periodic_library_sync():
    """Workflow SQL and file publication do not start the HTTP lifecycle."""


@pytest.fixture
def installed(request, tmp_path, monkeypatch, native_sql_calls):
    # Publication consumes one completed private predecessor. It still owns
    # real SQL, WAL snapshots, source guards and its explicit later admission.
    with _numbered_input(request.param, tmp_path, monkeypatch, snapshot_guards=True) as value:
        yield value


@pytest.fixture
def unadmitted_installed(request, tmp_path, monkeypatch, native_sql_calls, without_query_timing):
    with _numbered_input(request.param, tmp_path, monkeypatch) as value:
        yield value


def prepare(owner, conn, *, size=128):
    owner.worker.install(conn)
    for _ in range(3000):
        progress = owner.worker.step(conn, batch_size=size)
        if progress.status == 'prepared':
            return progress
    pytest.fail('Workflow preparation did not finish bounded fixture')


def stage(owner, conn, size=128):
    prepare(owner, conn)
    for _ in range(100):
        result = owner.step(conn, batch_size=size)
        if result.complete:
            return result.id
    pytest.fail('Workflow publication map did not finish bounded fixture')


def rejection_inputs(store):
    _, _, _, source = seed(store)
    owner = publication.OWNERS['playlists']
    prepare(owner, store.conn)
    assert owner.selected(store.conn) is None
    return source


@pytest.mark.parametrize('installed', [79, 93, 97], indirect=True)
def test_preopened_readers_and_real_callers_use_exact_selected_receipts(installed, monkeypatch):
    store, _ = installed
    values, originals, account, source = seed(store)
    other = Store(store._db_path)
    try:
        other.init_schema(defer_metadata=True)
        for owner in publication.OWNERS.values():
            identity = stage(owner, store.conn)
            owner.publish(store.conn, stage=identity)
            assert owner.selected(other.conn) == identity
        original = other.settings.files.read
        seen = []
        def read(reference, *, surface, chunk_key):
            assert not other.conn.in_transaction
            seen.append(surface)
            return original(reference, surface=surface, chunk_key=chunk_key)
        monkeypatch.setattr(other.settings.files, 'read', read)
        for key, raw in originals.items():
            assert other.settings.get_setting(key) == raw
        assert set(seen) == set(originals)
        gc_key = next(key for key in originals if key.startswith(workflow_settings.GC))
        assert playlist_gc_retry.states(other) == {gc_key:values[gc_key]}
        assert playlist_gc_retry.next_retry_at(other) == 4
        assert _playlist_transfers(other, account) == (source, values[source])
        assert other.settings.get_settings_with_prefix('spotify_library_imported_playlists:') == {
            key:raw for key, raw in originals.items() if key.startswith('spotify_library_imported_playlists:')}
        assert other.settings.get_setting(workflow_settings.PLAYLIST+':missing', 'absent') == 'absent'
        regular.validate(other.conn)
    finally:
        other.conn.close()
    reopened = Store(store._db_path)
    try:
        reopened.init_schema(defer_metadata=True)
        assert reopened.settings.get_setting(source) == originals[source]
    finally:
        reopened.conn.close()


@pytest.mark.parametrize('installed', [93], indirect=True)
@pytest.mark.parametrize('failure', ['fields', 'account', 'slot', 'extra', 'omitted', 'original', 'recovery', 'plan'])
def test_incorrect_receipt_fields_members_coverage_or_files_cannot_publish(installed, failure):
    store, _ = installed
    source = rejection_inputs(store)
    owner = publication.OWNERS['playlists']
    header = preparation.head(store.conn, 'playlists', source)
    receipts, items = (preparation.table('playlists', suffix) for suffix in ('receipts', 'items'))
    with store.conn:
        if failure == 'fields':
            store.conn.execute('UPDATE '+owner.header+' SET member_count=member_count+1 WHERE source=?', (source,))
        elif failure == 'account':
            store.conn.execute('UPDATE '+receipts+" SET account_id=NULL,playlist_scope_id=NULL,resolution_reason='missing_account' WHERE source=?", (source,))
        elif failure == 'slot':
            store.conn.execute('UPDATE '+items+" SET video_id_snapshot='different' WHERE source=? AND position=0", (source,))
        elif failure == 'extra':
            store.conn.execute('INSERT INTO '+items+' SELECT source,preparation_revision,receipt_position,99,video_id_snapshot FROM '+items+
                ' WHERE source=? AND position=0', (source,))
        elif failure == 'omitted':
            store.conn.execute('DELETE FROM '+owner.current+' WHERE source=?', (source,))
        else:
            filename = (header['plan_filename'] if failure == 'plan' else store.conn.execute('SELECT '+
                ('filename FROM '+preparation.table('playlists', 'parts') if failure == 'original' else 'recovery_filename FROM '+receipts)+
                ' WHERE source=? LIMIT 1', (source,)).fetchone()[0])
            Path(filename).write_bytes(b'corrupt')
    identity = owner.step(store.conn).id
    with pytest.raises((MigrationError, ValueError, OSError)):
        owner.publish(store.conn, stage=identity)
    assert owner.selected(store.conn) is None


@pytest.mark.parametrize('installed', [93], indirect=True)
@pytest.mark.parametrize('failure', ['deadline', 'recovery'])
def test_gc_deadlines_and_failure_evidence_require_verification_without_a_writer(installed, failure, monkeypatch):
    store, _ = installed
    seed(store)
    owner = publication.OWNERS['gc']
    prepare(owner, store.conn)
    attempts = preparation.table('gc', 'attempts')
    if failure == 'deadline':
        with store.conn:
            store.conn.execute('UPDATE '+attempts+' SET next_retry_at=next_retry_at+1')
    else:
        Path(store.conn.execute('SELECT recovery_filename FROM '+attempts+' LIMIT 1').fetchone()[0]).write_bytes(b'changed')
    read = publication.retained
    def outside_writer(*args):
        assert not store.conn.in_transaction
        return read(*args)
    monkeypatch.setattr(publication, 'retained', outside_writer)
    identity = owner.step(store.conn).id
    with pytest.raises((MigrationError, ValueError, OSError)):
        owner.publish(store.conn, stage=identity)
    assert owner.selected(store.conn) is None


@pytest.mark.parametrize('installed', [93], indirect=True)
def test_bounded_map_restart_and_atomic_cutover_keep_earlier_custody(installed, record_property):
    @contextmanager
    def phase(name):
        began = time.perf_counter()
        try:
            yield
        finally:
            record_property('workflow_map_' + name + '_seconds', time.perf_counter() - began)

    store, _ = installed
    # Five originals retain two full pages and a tail. The inherited mapper's
    # production 128-link limit has a real canvas boundary owner.
    inputs = {workflow_settings.ALBUM+':'+str(index): '["first", "first", "last"]'
              for index in range(5)}
    # Only the initial source rows share a transaction. Preparation, map
    # checkpoints, continuing writes and publication still own their commits.
    with phase('source_settings'):
        store.settings.set_values(inputs)
    assert not store.conn.in_transaction
    assert store.settings.get_settings_with_prefix(workflow_settings.ALBUM+':') == inputs
    owner = publication.OWNERS['albums']
    with phase('initial_preparation'):
        prepare(owner, store.conn)
    with phase('first_map_checkpoint'):
        first = owner.step(store.conn, batch_size=2)
    assert first.copied == 2 and not first.complete
    with store.conn:
        store.conn.execute('CREATE TRIGGER controlled_workflow_map BEFORE INSERT ON '+owner.table('map')+
            " BEGIN SELECT RAISE(ABORT,'controlled map interruption'); END")
    with phase('failed_map_continuation'), pytest.raises(sqlite3.IntegrityError, match='controlled map interruption'):
        owner.step(store.conn, batch_size=2)
    assert owner.stage(store.conn, first.id) == first
    assert store.conn.execute('SELECT count(*) FROM '+owner.table('custody')).fetchone()[0] == 2
    with store.conn:
        store.conn.execute('DROP TRIGGER controlled_workflow_map')
    changed = workflow_settings.ALBUM+':1'
    with phase('continuing_source_write'):
        store.settings.set_setting(changed, '["continuing"]')
    with phase('restart_preparation_and_map'):
        identity = stage(owner, store.conn, size=2)
    assert identity != first.id
    completed = owner.stage(store.conn, identity)
    assert completed.copied == 5 and completed.complete
    with store.conn:
        store.conn.execute('CREATE TRIGGER controlled_workflow_cutover BEFORE INSERT ON '+owner.table('head')+
            " BEGIN SELECT RAISE(ABORT,'controlled cutover interruption'); END")
    with phase('failed_selection'), pytest.raises(sqlite3.IntegrityError, match='controlled cutover interruption'):
        owner.publish(store.conn, stage=identity)
    assert not store.conn.execute('SELECT 1 FROM '+owner.table('generations')).fetchone()
    with store.conn:
        store.conn.execute('DROP TRIGGER controlled_workflow_cutover')
    with phase('selection_retry'):
        owner.publish(store.conn, stage=identity)
    with phase('original_readback'):
        assert owner.value(store.conn, identity, changed) == '["continuing"]'
        assert owner.value(store.conn, first.id, changed) == '["first", "first", "last"]'
    with phase('retained_receipt_guard'), pytest.raises(sqlite3.IntegrityError, match='retained'):
        with store.conn:
            store.conn.execute('DELETE FROM '+preparation.table('albums', 'receipts'))


@pytest.mark.parametrize('installed', [93], indirect=True)
def test_source_and_reference_races_reject_selection_without_admitting_missing_identity(installed):
    store, _ = installed
    _, _, _, source = seed(store)
    owner = publication.OWNERS['playlists']
    identity = stage(owner, store.conn)
    other = sqlite3.connect(store._db_path, timeout=.01)
    changed = False
    def race(conn, header):
        nonlocal changed
        publication.verify_document('playlists', conn, header)
        if not changed:
            changed = True
            with other:
                other.execute('UPDATE settings SET value=value WHERE key=?', (source,))
    try:
        with pytest.raises(MigrationError, match='changed|reconciled'):
            replace(owner, verify_document=race).publish(store.conn, stage=identity)
    finally:
        other.close()
    owner.publish(store.conn, stage=stage(owner, store.conn))
    assert not store.conn.execute('SELECT 1 FROM identities WHERE id=999').fetchone()
    assert not store.conn.execute("SELECT 1 FROM main.playlist_scopes WHERE ref='MISSING'").fetchone()
    store.accounts.upsert_account('Now observed', 'observed.json', None, False)
    assert owner.selected(store.conn) is None
    owner.publish(store.conn, stage=stage(owner, store.conn))


@pytest.mark.parametrize('unadmitted_installed', [93], indirect=True)
def test_file_recovery_and_explicit_failure_retry_do_not_block_regular_ddl(unadmitted_installed, monkeypatch):
    store, _ = unadmitted_installed
    _, originals, _, source = seed(store)
    owner = publication.OWNERS['playlists']
    identity = stage(owner, store.conn)
    owner.publish(store.conn, stage=identity)
    header = owner.capture(store.conn, identity, source)
    Path(header['plan_filename']).unlink()
    for suffix, column in (('parts', 'filename'), ('receipts', 'recovery_filename')):
        Path(store.conn.execute('SELECT '+column+' FROM '+preparation.table('playlists', suffix)+
            ' WHERE source=? LIMIT 1', (source,)).fetchone()[0]).unlink()
    with pytest.raises(OSError):
        store.settings.get_setting(source)
    store.init_schema(defer_metadata=True)
    regular.migrate(store.conn)
    publication.repair('playlists', store.conn, source)
    owner.publish(store.conn, stage=identity)
    assert store.settings.get_setting(source) == originals[source]
    store.settings.set_setting(source, '{}')
    install = preparation.policy_documents._publish_encoded
    def fail(*args, age=None):
        assert not store.conn.in_transaction
        raise OSError('controlled workflow file failure')
    monkeypatch.setattr(preparation.policy_documents, '_publish_encoded', fail)
    with pytest.raises(OSError, match='controlled workflow file failure'):
        owner.worker.step(store.conn)
    progress = owner.worker.progress(store.conn)
    def independent(conn, context):
        conn.execute('CREATE TABLE independent_workflow_publication_ddl(value TEXT)')
    monkeypatch.setattr(regular, 'CHANGES', (*regular.CHANGES, regular.Change(
        len(regular.CHANGES) + 1, 'independent_workflow_publication_ddl', independent,
        lambda conn: conn.execute('SELECT * FROM independent_workflow_publication_ddl LIMIT 0'))))
    regular.migrate(store.conn)
    assert owner.worker.progress(store.conn) == progress
    monkeypatch.setattr(preparation.policy_documents, '_publish_encoded', install)
    with pytest.raises(MigrationError, match='retry explicitly'):
        owner.worker.step(store.conn)
    owner.worker.step(store.conn, retry=True)
    owner.publish(store.conn, stage=stage(owner, store.conn))
    assert store.settings.get_setting(source) == '{}'


@pytest.mark.parametrize('installed', [79], indirect=True)
def test_null_blank_empty_and_duplicate_receipts_keep_exact_originals(installed):
    store, _ = installed
    owner = publication.OWNERS['albums']
    owner.publish(store.conn, stage=stage(owner, store.conn))
    assert owner.stage(store.conn, owner.selected(store.conn)).copied == 0
    source = workflow_settings.ALBUM
    for raw in (None, '', 'null', '[]', '["same", "same", "last"]'):
        with store.conn:
            store.conn.execute('INSERT INTO settings VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value', (source, raw))
        identity = stage(owner, store.conn)
        owner.publish(store.conn, stage=identity)
        assert store.settings.get_setting(source, 'missing') == raw
        assert store.settings.get_settings_with_prefix('spotify_library_imported_albums') == {source:raw}
    header = preparation.head(store.conn, 'albums', source)
    assert list(map(tuple, store.conn.execute('SELECT fingerprint,duplicate_position FROM '+preparation.table('albums', 'receipts')+
        ' WHERE source=? AND preparation_revision=? ORDER BY position', (source, header['preparation_revision'])))) == [
            ('same', None), ('same', 0), ('last', None)]
