"""Home clicked publication proves complete ranked state before bounded cutover."""
from dataclasses import replace
import json
from pathlib import Path
import sqlite3

import pytest

from tests.core.test_ranked_state_preparation import seed
from tests.store_fixture_support import (
    guarded_numbered_store as guarded_installed,
    numbered_store as installed,
    native_sql_calls,
    preparation_ddl_connection,
    publication_consumer_reads,
    without_query_timing,
)
from yt_playlist.core import library_pool_state, recommendation_outputs as files
from yt_playlist.core.migrations import MigrationError, regular
from yt_playlist.core.preparation import clicked_publication as publication, ranked_state as preparation
from yt_playlist.core.store import Store


@pytest.fixture(autouse=True)
def disable_periodic_library_sync():
    pass


def ready(conn, size=128, *, worker_size=128):
    owner = publication.OWNER
    owner.worker.install(conn)
    for _ in range(2000):
        if owner.worker.step(conn, batch_size=worker_size).status == 'prepared':
            break
    else:
        pytest.fail('Clicked preparation did not finish bounded fixture')
    for _ in range(100):
        stage = owner.step(conn, batch_size=size)
        if stage.complete:
            return stage.id
    pytest.fail('Clicked publication map did not finish bounded fixture')


def rejection_inputs(store, conn):
    seed(store)
    owner = publication.OWNER
    owner.worker.install(conn)
    for _ in range(100):
        if owner.worker.step(conn).status == 'prepared':
            assert owner.selected(conn) is None
            return
    pytest.fail('Clicked preparation did not finish bounded rejection prefix')


@pytest.mark.parametrize('installed', [79, 93, 94], indirect=True)
@pytest.mark.usefixtures('publication_consumer_reads', 'native_sql_calls', 'without_query_timing')
def test_preopened_readers_preserve_all_lanes_unknown_clock_and_original_bytes(installed, monkeypatch):
    store, _ = installed
    originals, _ = seed(store)
    owner = publication.OWNER
    other = Store(store._db_path)
    try:
        other.conn.execute('PRAGMA synchronous=OFF')
        other.init_schema(defer_metadata=True)
        stage = ready(store.conn, size=1)
        assert owner.stage(store.conn, stage).copied == 4
        def no_file_writes(*args, **kwargs):
            pytest.fail('Publication must not publish or fsync files')
        monkeypatch.setattr(files, 'publish', no_file_writes)
        owner.publish(store.conn, stage=stage)
        assert owner.selected(other.conn) == stage
        for surface in library_pool_state.SURFACES:
            assert owner.value(other.conn, stage, surface) == originals[surface]
            assert other.rec_proposals.get_proposals(surface) == json.loads(originals[surface])
            assert other.rec_proposals.has_proposals(surface)
            header = owner.capture(other.conn, stage, surface)
            assert header['built_at'] is None
            assert header['run_id'] is None and header['display_digest'] is None
            assert not other.conn.execute('SELECT 1 FROM recommendation_heads WHERE surface=?', (surface,)).fetchone()
        regular.validate(other.conn)
    finally:
        other.conn.close()
    reopened = Store(store._db_path)
    try:
        reopened.conn.execute('PRAGMA synchronous=OFF')
        reopened.init_schema(defer_metadata=True)
        assert reopened.rec_proposals.get_proposals('home_pool:wheelhouse') == json.loads(originals['home_pool:wheelhouse'])
    finally:
        reopened.conn.close()


@pytest.mark.parametrize('installed', [93], indirect=True)
@pytest.mark.parametrize('failure', ['header', 'candidate', 'missing', 'extra', 'policy', 'genre', 'slot', 'output', 'original', 'omitted'])
def test_incorrect_ranked_fields_members_or_files_cannot_publish(installed, preparation_ddl_connection, failure):
    store, _ = installed
    conn = preparation_ddl_connection
    rejection_inputs(store, conn)
    owner, surface = publication.OWNER, 'home_pool:wheelhouse'
    header = preparation.head(conn, surface)
    with conn:
        if failure == 'header':
            conn.execute('UPDATE prepared_clicked_pools_generations SET epoch=epoch+1 WHERE surface=?', (surface,))
        elif failure == 'candidate':
            conn.execute('UPDATE prepared_clicked_pools_candidates SET score=99 WHERE surface=?', (surface,))
        elif failure == 'missing':
            conn.execute('DELETE FROM prepared_clicked_pools_candidates WHERE surface=? AND rank=1', (surface,))
        elif failure == 'extra':
            conn.execute('INSERT INTO prepared_clicked_pools_candidates SELECT surface,preparation_revision,pool_digest,'
                '99,recording_id,raw_key,score,intensity_band,genre_key FROM prepared_clicked_pools_candidates '
                'WHERE surface=? AND rank=0', (surface,))
        elif failure == 'policy':
            conn.execute("UPDATE prepared_clicked_pools_policies SET policy_version=NULL,resolution_reason='missing_policy' WHERE surface=?", (surface,))
        elif failure == 'genre':
            conn.execute("UPDATE prepared_clicked_pools_genres SET raw_key='invented' WHERE surface=? AND position=0", (surface,))
        elif failure == 'slot':
            conn.execute("UPDATE prepared_clicked_pools_slots SET mode_key='invented' WHERE surface=?", (surface,))
        elif failure == 'output':
            conn.execute("UPDATE prepared_clicked_pools_outputs SET filename='/missing' WHERE surface=?", (surface,))
        elif failure == 'original':
            conn.execute("UPDATE prepared_clicked_pools_parts SET filename='/missing' WHERE surface=?", (surface,))
        elif failure == 'omitted':
            conn.execute('DELETE FROM prepared_clicked_pools_heads WHERE surface=?', (surface,))
    stage = owner.step(conn).id
    with pytest.raises((MigrationError, OSError, ValueError)):
        owner.publish(conn, stage=stage)
    assert owner.selected(conn) is None
    if failure == 'omitted':
        assert preparation.head(conn, surface) is None
    else:
        assert preparation.head(conn, surface)['preparation_revision'] == header['preparation_revision']
    regular.migrate(conn)


@pytest.mark.parametrize('guarded_installed', [93], indirect=True)
@pytest.mark.usefixtures('publication_consumer_reads')
def test_compatible_writes_deletes_and_reference_changes_withdraw_selection(guarded_installed):
    store, _ = guarded_installed
    originals, _ = seed(store)
    owner, surface = publication.OWNER, 'home_pool:wheelhouse'
    first = ready(store.conn)
    owner.publish(store.conn, stage=first)
    with store.read_snapshot() as snapshot:
        assert snapshot.store.rec_proposals.get_proposals(surface) == json.loads(originals[surface])
        store.rec_proposals.put_proposals(surface, {'parameters':{'lane':'wheelhouse', 'epoch':1,
            'include_genres':[], 'avoid_genres':[]}, 'tracks':[]}, 17)
        assert not snapshot.publish(lambda:None)
    assert owner.selected(store.conn) is None
    assert store.rec_proposals.get_proposals(surface)['parameters']['epoch'] == 1
    second = ready(store.conn)
    owner.publish(store.conn, stage=second)
    assert owner.value(store.conn, first, surface) == originals[surface]
    with store.conn:
        store.conn.execute('DELETE FROM rec_proposals WHERE surface=?', (surface,))
    assert owner.selected(store.conn) is None
    assert store.rec_proposals.get_proposals(surface) is None
    third = ready(store.conn)
    owner.publish(store.conn, stage=third)
    assert owner.capture(store.conn, third, surface) is None
    with store.conn:
        store.conn.execute("INSERT INTO genre_family(key,name) VALUES ('arrived','Arrived')")
    assert owner.selected(store.conn) is None
    fourth = ready(store.conn)
    owner.publish(store.conn, stage=fourth)
    header = owner.capture(store.conn, first, 'home_pool:explore')
    with pytest.raises(sqlite3.IntegrityError, match='retained'):
        with store.conn:
            store.conn.execute('DELETE FROM prepared_clicked_pools_candidates WHERE surface=? AND preparation_revision=?',
                ('home_pool:explore', header['preparation_revision']))


@pytest.mark.parametrize('installed', [93], indirect=True)
@pytest.mark.usefixtures('publication_consumer_reads')
def test_source_race_and_interrupted_cutover_leave_no_partial_publication(installed):
    store, _ = installed
    seed(store)
    owner = publication.OWNER
    stage = ready(store.conn)
    other = Store(store._db_path)
    try:
        def race(conn, header):
            publication.verify_document(conn, header)
            with other.conn:
                other.conn.execute('UPDATE rec_proposals SET built_at=built_at WHERE surface=?', (header['surface'],))
        with pytest.raises(MigrationError):
            replace(owner, verify_document=race).publish(store.conn, stage=stage)
        assert owner.selected(store.conn) is None
        stage = ready(store.conn)
        with store.conn:
            store.conn.execute('CREATE TRIGGER controlled_clicked_interruption BEFORE INSERT ON '+owner.table('head')+
                " BEGIN SELECT RAISE(ABORT,'controlled interruption'); END")
        with pytest.raises(sqlite3.IntegrityError, match='controlled interruption'):
            owner.publish(store.conn, stage=stage)
        assert not store.conn.execute('SELECT 1 FROM '+owner.table('generations')+' WHERE revision=?', (stage,)).fetchone()
        with store.conn:
            store.conn.execute('DROP TRIGGER controlled_clicked_interruption')
        owner.publish(store.conn, stage=stage)
    finally:
        other.conn.close()


@pytest.mark.parametrize('installed', [93], indirect=True)
@pytest.mark.usefixtures('publication_consumer_reads')
def test_missing_original_or_output_and_failed_preparation_do_not_block_ddl(installed):
    store, _ = installed
    seed(store)
    owner, surface = publication.OWNER, 'home_pool:wheelhouse'
    stage = ready(store.conn)
    owner.publish(store.conn, stage=stage)
    for suffix in ('parts', 'outputs'):
        filename = store.conn.execute('SELECT filename FROM prepared_clicked_pools_'+suffix+' WHERE surface=? LIMIT 1', (surface,)).fetchone()[0]
        Path(filename).unlink()
        with pytest.raises(OSError):
            owner.publish(store.conn, stage=stage)
        store.init_schema(defer_metadata=True)
        regular.migrate(store.conn)
        publication.repair(store.conn, surface)
        owner.publish(store.conn, stage=stage)
    with store.conn:
        store.conn.execute('UPDATE rec_proposals SET built_at=17 WHERE surface=?', (surface,))
    def fail(*args, **kwargs):
        assert not store.conn.in_transaction
        raise OSError('controlled failed ranked preparation')
    with pytest.raises(OSError, match='controlled failed'):
        replace(owner.worker, prepare=fail).step(store.conn)
    progress = owner.worker.progress(store.conn)
    assert progress.status == 'failed'
    store.init_schema(defer_metadata=True)
    regular.migrate(store.conn)
    assert owner.worker.progress(store.conn) == progress
    owner.worker.step(store.conn, retry=True)
    owner.publish(store.conn, stage=ready(store.conn))
