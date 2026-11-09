"""Retained model trial selection keeps decisions, baselines and assignments."""
from dataclasses import replace
import json
import sqlite3

import pytest

from tests.core.test_model_trial_preparation import head, seed
from tests.publication_fixture_support import installed, installed_reader
from tests.store_fixture_support import (
    native_sql_calls, preparation_ddl_connection, publication_consumer_reads, without_query_timing,
)
from tests.store_fixture_support import numbered_store as unadmitted_installed
from yt_playlist.core.migrations import MigrationError, regular
from yt_playlist.core.preparation import model_trials as preparation, trial_publication as publication
from yt_playlist.core.store import Store


pytestmark = pytest.mark.usefixtures('native_sql_calls', 'without_query_timing', 'publication_consumer_reads')


@pytest.fixture(autouse=True)
def disable_periodic_library_sync():
    pass


def ready(owner, conn):
    owner.worker.install(conn)
    for _ in range(100):
        progress = owner.worker.step(conn, batch_size=128)
        if progress.status == 'prepared':
            return progress.prepared_revision
    pytest.fail('Trial preparation did not finish bounded fixture')


@pytest.mark.parametrize('unadmitted_installed', [79, 93, 94], indirect=True)
def test_preopened_and_reopened_readers_preserve_exact_trial_decisions(
        unadmitted_installed, monkeypatch, preparation_ddl_connection):
    store, _ = unadmitted_installed
    values, originals = seed(store)
    with installed_reader(store._db_path, monkeypatch) as other:
        for owner in publication.OWNERS.values():
            revision = ready(owner, store.conn)
            owner.publish(store.conn, revision=revision)
            assert owner.selected(other.conn) == revision
            assert owner.value(other.conn, revision) == originals[owner.source]
            assert other.rec_proposals.get_proposals(owner.source) == values[owner.source]
            assert other.rec_proposals.has_proposals(owner.source)
            captured = owner.capture(other.conn)
            assert captured['built_at'] is None
            assert captured['run_ref'] == (1 if owner.source == 'model_live_comparison' else None)
        regular.migrate(preparation_ddl_connection)
    with installed_reader(store._db_path, monkeypatch) as reopened:
        assert reopened.rec_proposals.get_proposals('model_album_assignment') == values['model_album_assignment']


@pytest.mark.parametrize('installed', [93], indirect=True)
@pytest.mark.parametrize('failure', ['mask', 'run', 'clock', 'baseline', 'extra', 'missing', 'original', 'original_clock'])
def test_incorrect_fields_baseline_or_original_cannot_publish(installed, failure):
    store, _ = installed
    seed(store)
    owner = publication.OWNERS['listening-monitor']
    revision = ready(owner, store.conn)
    generations, _, originals = preparation.tables(owner.source)
    with store.conn:
        if failure == 'mask':
            store.conn.execute('UPDATE '+generations+' SET field_mask=0')
        elif failure == 'run':
            store.conn.execute('UPDATE '+generations+' SET run_id=998')
        elif failure == 'clock':
            store.conn.execute('UPDATE '+generations+' SET built_at=15')
        elif failure == 'baseline':
            store.conn.execute('UPDATE '+preparation.BASELINE+' SET value=.9 WHERE position=0')
        elif failure == 'extra':
            store.conn.execute('INSERT INTO '+preparation.BASELINE+' VALUES (?,?,3,.9)', (owner.source, revision))
        elif failure == 'missing':
            store.conn.execute('DELETE FROM '+preparation.BASELINE+' WHERE position=1')
        elif failure == 'original':
            store.conn.execute('UPDATE '+originals+" SET raw_value='{}'")
        elif failure == 'original_clock':
            store.conn.execute('UPDATE '+originals+' SET built_at=15')
    with pytest.raises(MigrationError):
        owner.publish(store.conn, revision=revision)
    assert owner.selected(store.conn) is None


@pytest.mark.parametrize('installed', [93], indirect=True)
def test_compatible_writes_snapshot_fence_owner_independence_and_run_retirement(installed):
    store, _ = installed
    values, originals = seed(store)
    revisions = {}
    for name, owner in publication.OWNERS.items():
        revisions[name] = ready(owner, store.conn)
        owner.publish(store.conn, revision=revisions[name])
    owner = publication.OWNERS['album-assignment']
    with store.read_snapshot() as snapshot:
        assert snapshot.store.rec_proposals.get_proposals(owner.source) == values[owner.source]
        store.rec_proposals.put_proposals(owner.source, {'epoch':'new', 'comparison':None}, 17)
        assert not snapshot.publish(lambda:None)
    assert owner.selected(store.conn) is None
    assert publication.OWNERS['live-trial'].selected(store.conn) == revisions['live-trial']
    owner.publish(store.conn, revision=ready(owner, store.conn))
    assert owner.value(store.conn, revisions['album-assignment']) == originals[owner.source]
    assert store.rec_proposals.get_proposals(owner.source) == {'epoch':'new', 'comparison':None}
    with pytest.raises(sqlite3.IntegrityError, match='future cleanup'):
        with store.conn:
            store.conn.execute('UPDATE '+preparation.tables(owner.source)[2]+" SET raw_value='{}'")
    live = publication.OWNERS['live-trial']
    with store.conn:
        store.conn.execute('DELETE FROM rec_model_selection_runs WHERE id=1')
    assert live.selected(store.conn) is None
    live.publish(store.conn, revision=ready(live, store.conn))
    assert live.capture(store.conn)['run_resolution'] == 'missing_run'
    assert store.rec_proposals.get_proposals(live.source) == values[live.source]


@pytest.mark.parametrize('installed', [93], indirect=True)
def test_full_baseline_null_blank_and_missing_nested_inputs_remain_exact(installed):
    store, _ = installed
    owner = publication.OWNERS['listening-monitor']
    value = {'run_id':None, 'setup':{}, 'policy':None, 'complete':False,
        'baseline':[index/4095 for index in range(4096)]}
    store.rec_proposals.put_proposals(owner.source, value, 9007199254740993)
    owner.publish(store.conn, revision=ready(owner, store.conn))
    assert store.rec_proposals.get_proposals(owner.source) == value
    with pytest.raises(sqlite3.IntegrityError, match='retained'):
        with store.conn:
            store.conn.execute('DELETE FROM '+preparation.BASELINE)
    for value in ({'baseline':None}, {'baseline':[]}, {}):
        store.rec_proposals.put_proposals(owner.source, value, None)
        owner.publish(store.conn, revision=ready(owner, store.conn))
        assert store.rec_proposals.get_proposals(owner.source) == value


@pytest.mark.parametrize('installed', [93], indirect=True)
def test_source_race_interrupted_cutover_and_failed_preparation_do_not_block_ddl(installed):
    store, _ = installed
    seed(store)
    owner = publication.OWNERS['live-trial']
    revision = ready(owner, store.conn)
    other = Store(store._db_path)
    try:
        def race(conn, header, value):
            publication.verify_rows(conn, header, value)
            with other.conn:
                other.conn.execute('UPDATE rec_proposals SET built_at=built_at WHERE surface=?', (owner.source,))
        with pytest.raises(MigrationError):
            replace(owner, verify_rows=race).publish(store.conn, revision=revision)
        revision = ready(owner, store.conn)
        with store.conn:
            store.conn.execute('CREATE TRIGGER controlled_trial_interruption BEFORE INSERT ON '+owner.tables[1]+
                " BEGIN SELECT RAISE(ABORT,'controlled trial interruption'); END")
        with pytest.raises(sqlite3.IntegrityError, match='controlled trial interruption'):
            owner.publish(store.conn, revision=revision)
        assert not store.conn.execute('SELECT 1 FROM '+owner.tables[0]+' WHERE revision=?', (revision,)).fetchone()
        with store.conn:
            store.conn.execute('DROP TRIGGER controlled_trial_interruption')
        owner.publish(store.conn, revision=revision)
        store.rec_proposals.put_proposals(owner.source, {'phase':'retained'}, 17)
        def fail(*args, **kwargs):
            assert not store.conn.in_transaction
            raise OSError('controlled trial preparation failure')
        with pytest.raises(OSError, match='controlled trial preparation failure'):
            replace(owner.worker, prepare=fail).step(store.conn)
        failed = owner.worker.progress(store.conn)
        assert failed.status == 'failed'
        store.init_schema(defer_metadata=True)
        regular.migrate(store.conn)
        assert owner.worker.progress(store.conn) == failed
        owner.worker.step(store.conn, retry=True)
        owner.publish(store.conn, revision=ready(owner, store.conn))
    finally:
        other.conn.close()


@pytest.mark.parametrize('installed', [93], indirect=True)
def test_absent_deleted_and_renamed_sources_keep_retained_originals(installed):
    store, _ = installed
    owner = publication.OWNERS['album-assignment']
    owner.publish(store.conn, revision=ready(owner, store.conn))
    assert owner.value(store.conn, owner.selected(store.conn)) is None
    store.rec_proposals.put_proposals(owner.source, {'epoch':'0000'}, None)
    first = ready(owner, store.conn)
    owner.publish(store.conn, revision=first)
    with store.conn:
        store.conn.execute('UPDATE rec_proposals SET surface=? WHERE surface=?', ('retained-trial-original', owner.source))
    assert owner.selected(store.conn) is None
    owner.publish(store.conn, revision=ready(owner, store.conn))
    assert store.rec_proposals.get_proposals(owner.source) is None
    assert json.loads(owner.value(store.conn, first)) == {'epoch':'0000'}
