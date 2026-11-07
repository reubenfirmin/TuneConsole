"""Draft publication preserves decisions, revision ownership and latest selection."""
from dataclasses import replace
import json
from pathlib import Path
import sqlite3

import pytest

from tests.core.test_predecessor_writes import installed
from tests.core.test_road_trip_draft_preparation import recipe, seed
from tests.preparation_timing_support import preparation_costs
from yt_playlist.core.migrations import MigrationError, regular
from yt_playlist.core.preparation import draft_publication as publication, road_trip_drafts as preparation
from yt_playlist.core.store import Store


def stage(conn, size=128, *, worker_size=128):
    owner = publication.OWNER
    owner.worker.install(conn)
    for _ in range(2000):
        progress = owner.worker.step(conn, batch_size=worker_size)
        if progress.status == 'prepared':
            break
    else:
        pytest.fail('Draft preparation did not finish bounded fixture')
    for _ in range(1000):
        value = owner.step(conn, batch_size=size)
        if value.complete:
            return value.id
    pytest.fail('Draft map did not finish bounded fixture')


def rejection_inputs(store):
    identity, raw = seed(store)
    owner = publication.OWNER
    owner.worker.install(store.conn)
    for _ in range(20):
        if owner.worker.step(store.conn).status == 'prepared':
            assert owner.selected(store.conn) is None
            return identity, raw
    pytest.fail('Draft preparation did not finish bounded rejection prefix')


@pytest.mark.parametrize('installed', [79, 93], indirect=True)
def test_preopened_readers_use_exact_decisions_and_unknown_authorship(installed):
    store, _ = installed
    identity, raw = seed(store)
    owner = publication.OWNER
    other = Store(store._db_path)
    try:
        other.init_schema(defer_metadata=True)
        selected = stage(store.conn)
        owner.publish(store.conn, stage=selected)
        assert owner.selected(other.conn) == selected
        assert other.road_trip.get_road_trip_draft(identity) == json.loads(raw)
        assert other.road_trip.latest_road_trip_draft() == {'recipe_id':identity, 'state':json.loads(raw)}
        header = owner.capture(other.conn, selected, identity)
        assert header['account_id'] is None and header['legacy_author_unknown'] == 1
        assert owner.value(other.conn, selected, identity) == raw
        assert other.conn.execute("SELECT 1 FROM youtube_items WHERE video_id='missing'").fetchone() is None
        regular.validate(other.conn)
    finally:
        other.conn.close()
    reopened = Store(store._db_path)
    try:
        reopened.init_schema(defer_metadata=True)
        assert reopened.road_trip.get_road_trip_draft(identity) == json.loads(raw)
    finally:
        reopened.conn.close()


@pytest.mark.parametrize('installed', [93], indirect=True)
def test_empty_map_and_numeric_cursor_keep_latest_recipe_eligibility(installed):
    store, _ = installed
    owner = publication.OWNER
    owner.publish(store.conn, stage=stage(store.conn))
    assert store.road_trip.latest_road_trip_draft() is None
    identities = []
    for index in range(12):
        identity = recipe(store, str(index))
        identities.append(identity)
        store.road_trip.save_road_trip_draft(identity, {'revision':str(index), 'picks':[]}, index)
    selected = stage(store.conn, size=1)
    assert owner.stage(store.conn, selected).copied == 12
    owner.publish(store.conn, stage=selected)
    assert store.road_trip.latest_road_trip_draft()['recipe_id'] == identities[-1]
    assert store.road_trip.get_road_trip_draft(identities[9])['revision'] == '9'
    # The legacy latest query only includes recipes that still exist. A direct
    # orphan does not become a visible draft just because its file is retained.
    with store.conn:
        store.conn.execute('DELETE FROM road_trip_recipes WHERE id=?', (identities[-1],))
    assert store.road_trip.latest_road_trip_draft()['recipe_id'] == identities[-2]
    query = store.conn.execute('EXPLAIN QUERY PLAN SELECT g.* FROM '+owner.table('map')+' m JOIN '+owner.header+
        ' g ON g.recipe_id=m.source AND g.generation=m.generation WHERE m.stage=? AND m.source=?',
        (selected, identities[9])).fetchall()
    assert all('SCAN' not in row[-1] for row in query)


@pytest.mark.parametrize('installed', [93], indirect=True)
@pytest.mark.parametrize('failure', ['time', 'revision', 'reference', 'missing', 'extra', 'original'])
def test_incorrect_draft_fields_members_or_original_cannot_publish(installed, staged_custody, failure):
    store, _ = installed
    identity, raw = staged_custody(installed, 'road-trip-drafts', rejection_inputs)
    owner = publication.OWNER
    with store.conn:
        if failure == 'time':
            store.conn.execute('UPDATE '+preparation.HEAD+' SET updated_at=12')
        elif failure == 'revision':
            store.conn.execute('UPDATE '+preparation.HEAD+" SET source_revision='wrong'")
        elif failure == 'reference':
            store.conn.execute('UPDATE '+preparation.ITEMS+" SET entry_id=NULL,recording_id=NULL,unresolved_reason='unadmitted_video'")
        elif failure == 'missing':
            store.conn.execute('DELETE FROM '+preparation.ITEMS+" WHERE selection_state='picked' AND position=1")
        elif failure == 'extra':
            store.conn.execute('INSERT INTO '+preparation.ITEMS+
                " SELECT recipe_id,generation,'picked',99,'extra',NULL,NULL,'unadmitted_video' FROM "+preparation.HEAD)
        else:
            Path(store.conn.execute('SELECT filename FROM '+preparation.PARTS+' LIMIT 1').fetchone()[0]).write_bytes(b'corrupt')
    selected = owner.step(store.conn).id
    with pytest.raises((MigrationError, ValueError, OSError)):
        owner.publish(store.conn, stage=selected)
    assert owner.selected(store.conn) is None
    assert store.road_trip.get_road_trip_draft(identity) == json.loads(raw)
    regular.migrate(store.conn)


@pytest.mark.parametrize('installed', [93], indirect=True)
def test_real_conditional_writer_compares_atomically_and_rolls_back_failed_sql(installed, monkeypatch):
    store, _ = installed
    identity, raw = seed(store)
    owner = publication.OWNER
    selected = stage(store.conn)
    owner.publish(store.conn, stage=selected)
    other = sqlite3.connect(store._db_path, timeout=.001)
    read, execute = store.road_trip.get_road_trip_draft, store.conn.execute
    writes = []
    def fail(sql, args=()):
        conditional_save = sql.startswith('UPDATE road_trip_drafts SET state=')
        if conditional_save:
            assert store.conn.in_transaction
            with pytest.raises(sqlite3.OperationalError, match='locked'):
                other.execute('BEGIN IMMEDIATE')
            writes.append(args[2])
        result = execute(sql, args)
        if conditional_save:
            raise RuntimeError('controlled draft save failure')
        return result
    monkeypatch.setattr(store.conn, 'execute', fail)
    try:
        with pytest.raises(RuntimeError, match='controlled draft save failure'):
            store.road_trip.replace_road_trip_draft(identity, {'revision':'next'}, 12, expected_revision='held')
        monkeypatch.setattr(store.conn, 'execute', execute)
        assert writes == [identity]
        assert owner.selected(store.conn) == selected
        assert read(identity) == json.loads(raw)
        with store.read_snapshot() as snapshot:
            assert snapshot.store.road_trip.get_road_trip_draft(identity)['revision'] == 'held'
            assert store.road_trip.replace_road_trip_draft(identity, {'revision':'next', 'picks':['missing']}, 12, expected_revision='held')
            assert not snapshot.publish(lambda:None)
        assert owner.selected(store.conn) is None
        assert not store.road_trip.replace_road_trip_draft(identity, {'revision':'stale'}, 13, expected_revision='held')
        owner.publish(store.conn, stage=stage(store.conn))
        assert read(identity)['revision'] == 'next'
        assert owner.value(store.conn, selected, identity) == raw
    finally:
        other.close()


@pytest.mark.parametrize('installed', [93], indirect=True)
def test_catalog_updates_and_deletion_withdraw_selection_without_losing_prior_decisions(installed):
    store, _ = installed
    identity, raw = seed(store)
    owner = publication.OWNER
    first = stage(store.conn)
    owner.publish(store.conn, stage=first)
    store.tracks.upsert_track('missing', 'Arrived', 'Artist', None, 10)
    assert owner.selected(store.conn) is None
    second = stage(store.conn)
    owner.publish(store.conn, stage=second)
    header = owner.capture(store.conn, second, identity)
    assert store.conn.execute('SELECT entry_id FROM '+preparation.ITEMS+
        " WHERE recipe_id=? AND generation=? AND selection_state='picked' AND position=2",
        (identity, header['generation'])).fetchone()[0] is not None
    store.road_trip.delete_road_trip_draft(identity)
    owner.publish(store.conn, stage=stage(store.conn))
    assert store.road_trip.latest_road_trip_draft() is None
    assert owner.value(store.conn, first, identity) == raw


@pytest.mark.parametrize('installed', [93], indirect=True)
@pytest.mark.parametrize('revision', ['absent', None, '', {'invalid':'retained'}])
def test_unrecorded_revision_kinds_are_never_replaced(installed, revision):
    store, _ = installed
    identity = recipe(store)
    value = {} if revision == 'absent' else {'revision':revision}
    store.road_trip.save_road_trip_draft(identity, value, 1)
    publication.OWNER.publish(store.conn, stage=stage(store.conn))
    assert store.road_trip.get_road_trip_draft(identity) == value


@pytest.mark.parametrize('installed', [93], indirect=True)
def test_full_selection_budget_retains_order_and_repeated_uploads(installed, preparation_costs):
    store, _ = installed
    identity = recipe(store)
    value = {'revision':'large', 'picks':['repeated']*16384, 'banned':['repeated']*16384}
    store.road_trip.save_road_trip_draft(identity, value, 9007199254740993)
    original_source = tuple(publication.source_row(store.conn, identity))
    owner = publication.OWNER
    # This owns the full decision budget. The separate 1,200-decision control
    # owns 128-member checkpoints and genuine mid-batch rollback/retry.
    selected = stage(store.conn, worker_size=512)
    owner.publish(store.conn, stage=selected)
    header = owner.capture(store.conn, selected, identity)
    assert header['item_count'] == 32768
    for state in ('picked', 'excluded'):
        members = list(map(tuple, store.conn.execute(
            f'SELECT position,video_id FROM {preparation.ITEMS} '
            'WHERE recipe_id=? AND generation=? AND selection_state=? ORDER BY position',
            (identity, header['generation'], state))))
        assert members == [(position, 'repeated') for position in range(16384)]
    assert json.loads(owner.value(store.conn, selected, identity)) == value
    assert store.road_trip.get_road_trip_draft(identity) == value
    assert tuple(publication.source_row(store.conn, identity)) == original_source
    assert header['updated_at'] == original_source[2]
    assert not store.conn.in_transaction
    assert not store.conn.execute('PRAGMA foreign_key_check').fetchall()


@pytest.mark.parametrize('installed', [93], indirect=True)
def test_missing_files_and_failed_preparation_do_not_block_regular_ddl(installed):
    store, _ = installed
    identity, raw = seed(store)
    owner = publication.OWNER
    selected = stage(store.conn)
    owner.publish(store.conn, stage=selected)
    Path(store.conn.execute('SELECT filename FROM '+preparation.PARTS+' LIMIT 1').fetchone()[0]).unlink()
    store.init_schema(defer_metadata=True)
    regular.migrate(store.conn)
    publication.repair(store.conn, identity)
    owner.publish(store.conn, stage=selected)
    assert owner.value(store.conn, selected, identity) == raw
    with pytest.raises(sqlite3.IntegrityError, match='retained'):
        with store.conn:
            store.conn.execute('DELETE FROM '+preparation.ITEMS)
    def fail(*args):
        raise OSError('controlled draft preparation failure')
    store.road_trip.save_road_trip_draft(identity, json.loads(raw), 12)
    with pytest.raises(OSError):
        replace(owner.worker, prepare=fail).step(store.conn, retry=True)
    failed = owner.worker.progress(store.conn)
    assert failed.status == 'failed'
    regular.migrate(store.conn)
    assert owner.worker.progress(store.conn) == failed
