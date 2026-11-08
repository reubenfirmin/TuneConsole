"""Full-size receipt preparation and both atomic cutover outcomes stay real."""
import json
import sqlite3
from dataclasses import replace
from types import SimpleNamespace

import pytest

from tests.core.test_tv_state_preparation import finish
from tests.test_tv_stream import video
from yt_playlist.core import schema_inspection
from yt_playlist.core.preparation import tv_publication as publication, tv_state as preparation


@pytest.fixture(autouse=True)
def disable_periodic_library_sync():
    """Receipt SQL and files do not start the HTTP lifecycle."""


def test_receipt_schema_input_keeps_real_installed_history_and_snapshot_guards(_bounded_installed_schema):
    source = _bounded_installed_schema(93).source
    assert source.conn.execute('SELECT max(version) FROM schema_migrations').fetchone()[0] == 93
    assert source._snapshot_revisions.conn is source.conn
    assert not source.conn.execute('PRAGMA foreign_key_check').fetchone()


@pytest.fixture(scope='module')
def receipt_source(_bounded_installed_schema, tmp_path_factory):
    path = tmp_path_factory.mktemp('tv-receipt-budget')/'source.db'
    with _bounded_installed_schema(93).open(path) as store, schema_inspection.reuse_tables(store.conn):
        store.conn.execute('PRAGMA synchronous=NORMAL')
        value = {f'{number+1}:stream:request:{number}': {'playlist_id': number+101, 'stream': 'next',
            'items': [video(index) for index in range(60)]} for number in range(60)}
        raw = json.dumps(value, indent=2)
        with store.conn:
            store.conn.execute('INSERT INTO rec_proposals VALUES (?,?,?)', ('tv_continuations', raw, 9007199254740993))
        original = tuple(store.conn.execute('SELECT * FROM rec_proposals WHERE surface=?', ('tv_continuations',)).fetchone())
        yield SimpleNamespace(store=store, value=value, raw=raw, original=original,
                              owner=publication.OWNERS['continuations'])


def test_full_budget_source_keeps_sixty_receipts_and_exact_stored_timestamp(receipt_source):
    assert len(receipt_source.value) == 60
    assert sum(len(receipt['items']) for receipt in receipt_source.value.values()) == 3600
    assert receipt_source.original[1] == receipt_source.raw
    assert receipt_source.original[2] == float(9007199254740993)
    assert receipt_source.owner.selected(receipt_source.store.conn) is None


@pytest.fixture(scope='module')
def checkpointed_receipts(receipt_source):
    receipt_source.owner.worker.install(receipt_source.store.conn)

    def capture_plan(conn, row):
        receipt_source.plan = preparation.prepare(conn, row)
        return receipt_source.plan

    worker = replace(receipt_source.owner.worker, prepare=capture_plan)
    receipt_source.checkpoint = worker.step(receipt_source.store.conn, batch_size=512)
    return receipt_source


def test_full_budget_first_slice_commits_a_bounded_prefix_without_selecting_output(checkpointed_receipts):
    state = checkpointed_receipts
    assert state.checkpoint.processed == 512
    assert state.checkpoint.status != 'prepared'
    assert state.owner.selected(state.store.conn) is None
    assert not state.store.conn.in_transaction
    assert state.plan.row_count == 3660
    assert state.plan.part_count > 0


@pytest.fixture(scope='module')
def receipts(checkpointed_receipts):
    # The first slice computed the genuine full-size Plan and published all
    # originals. Reuse that exact lower input while the real 512-unit batches
    # copy every row and advance their SQL checkpoints.
    state = checkpointed_receipts

    def completed_plan(conn, row):
        assert bytes(row['payload']) == state.raw.encode()
        assert row['built_at'] == state.original[2]
        return state.plan

    worker = replace(state.owner.worker, prepare=completed_plan)
    state.revision = finish(worker, state.store.conn, size=512).prepared_revision
    state.owner.install(state.store.conn)
    return state


def test_full_budget_preparation_retains_sixty_receipts_and_all_three_thousand_six_hundred_items(receipts):
    conn = receipts.store.conn
    assert len(receipts.value) == 60
    assert conn.execute('SELECT count(*) FROM '+preparation.relation('tv_continuation_receipts')+
        ' WHERE preparation_revision=?', (receipts.revision,)).fetchone()[0] == 60
    assert conn.execute('SELECT count(*) FROM '+preparation.relation('tv_continuation_items')).fetchone()[0] == 3600
    assert receipts.store.rec_proposals._proposal_json(receipts.owner.source) == receipts.raw == receipts.original[1]
    # Source timestamp affinity is REAL; preserve its actual stored value.
    assert receipts.original[2] == float(9007199254740993)
    assert receipts.owner.selected(conn) is None
    assert receipts.owner.worker.progress(conn).prepared_revision == receipts.revision


@pytest.fixture(scope='module')
def interrupted_receipts(receipts):
    conn = receipts.store.conn
    with conn:
        conn.execute('CREATE TRIGGER controlled_tv_interrupt BEFORE INSERT ON '+receipts.owner.tables[1]+
            " BEGIN SELECT RAISE(ABORT,'controlled TV interruption'); END")
    with pytest.raises(sqlite3.IntegrityError, match='controlled TV interruption'):
        receipts.owner.publish(conn, revision=receipts.revision)
    return receipts


def test_interrupted_full_budget_cutover_leaves_no_partial_generation_or_selection(interrupted_receipts):
    state = interrupted_receipts
    assert not state.store.conn.execute('SELECT 1 FROM '+state.owner.tables[0]).fetchone()
    assert state.owner.selected(state.store.conn) is None
    assert not state.store.conn.in_transaction


def test_explicit_retry_selects_exact_full_budget_without_changing_source_affinity(interrupted_receipts):
    state = interrupted_receipts
    conn = state.store.conn
    with conn:
        conn.execute('DROP TRIGGER controlled_tv_interrupt')
    state.owner.publish(conn, revision=state.revision)
    assert state.store.rec_proposals._proposal_json(state.owner.source) == state.raw
    assert state.store.rec_proposals.get_proposals(state.owner.source) == state.value
    assert tuple(conn.execute('SELECT * FROM rec_proposals WHERE surface=?', (state.owner.source,)).fetchone()) == state.original
    assert not conn.in_transaction and not conn.execute('PRAGMA foreign_key_check').fetchone()
