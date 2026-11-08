"""Real full-size plans and bounded SQL copying have separate owners."""
from contextlib import closing
from dataclasses import replace
import hashlib
import json
import sqlite3
from unittest.mock import Mock

import pytest

from tests.catalog_migration_support import catalog_reader
from tests.genre_catalog_support import bound_catalog
from tests.store_fixture_support import install_numbered_input
from tests.test_tv_stream import video
from yt_playlist.core.migrations import runner
from yt_playlist.core.preparation import tv_state as preparation


SURFACE = 'tv_continuations'
JOB = preparation.JOBS['continuations']


@pytest.fixture(autouse=True)
def disable_periodic_library_sync():
    """These file/SQL owners have no HTTP lifespan."""


@pytest.fixture(scope='module', autouse=True)
def isolated_app_home(tmp_path_factory):
    with pytest.MonkeyPatch.context() as patch:
        patch.setenv('YT_PLAYLIST_HOME', str(tmp_path_factory.mktemp('tv-receipt-home')))
        patch.setenv('YT_PLAYLIST_AUDIO_VIBES', '0')
        patch.setenv('YT_PLAYLIST_AUDIO_VIBE_MODEL', '')
        patch.delenv('DEEPSEEK_API_KEY', raising=False)
        bound_catalog(patch)
        yield


def head(conn):
    generations, current, _ = preparation.tables(SURFACE)
    return conn.execute(f'SELECT g.* FROM {current} h JOIN {generations} g '
                        'USING(source,preparation_revision)').fetchone()


@pytest.fixture(scope='module')
def installed(tmp_path_factory):
    path = tmp_path_factory.mktemp('tv-receipt-budget') / 'installed.db'
    with closing(sqlite3.connect(path)) as conn:
        conn.execute('PRAGMA foreign_keys=ON')
        conn.execute('PRAGMA journal_mode=WAL')
        install_numbered_input(conn, 93)
    with catalog_reader(path, 93, snapshot_guards=True) as store:
        yield {'store': store, 'conn': store.conn}


def test_receipt_budget_uses_the_real_supported_v93_predecessor(installed):
    # Validate main tables without the consumer's account-name TEMP views.
    with closing(sqlite3.connect(installed['store']._db_path.as_uri() + '?mode=ro', uri=True)) as conn:
        conn.row_factory = sqlite3.Row
        runner.validate_version(conn, 93)
    assert installed['conn'].execute('PRAGMA foreign_keys').fetchone()[0] == 1


@pytest.fixture(scope='module')
def prior_generation(installed):
    conn = installed['conn']
    installed['store'].rec_proposals.put_proposals(SURFACE, {}, None)
    JOB.install(conn)
    for _ in range(3):
        if JOB.step(conn, batch_size=128).status == 'prepared':
            break
    assert JOB.progress(conn).status == 'prepared'
    installed['previous'] = dict(head(conn))
    return installed


def test_real_empty_generation_is_complete_before_its_replacement(prior_generation):
    state = prior_generation
    assert state['previous']['row_count'] == 0
    assert preparation.read_original(state['conn'], state['previous']) == '{}'


@pytest.fixture(scope='module')
def full_plan(prior_generation):
    state = prior_generation
    value = {f'{number + 1}::request:{number}': {
        'playlist_id': number + 101, 'stream': f'stream-{number}',
        'items': [video(index) for index in range(60)]} for number in range(60)}
    state['store'].rec_proposals.put_proposals(SURFACE, value, 30)
    # Use the worker's actual bounded source projection, including exact bytes.
    row = dict(JOB.definition._read(state['conn'], 'rec_proposals', '["tv_continuations"]'))
    assert not state['conn'].in_transaction
    plan = preparation.prepare(state['conn'], row)
    assert not state['conn'].in_transaction
    state.update(value=value, row=row, plan=plan)
    return state


def test_real_full_budget_planning_keeps_every_item_and_publishes_all_original_parts(full_plan):
    state, plan = full_plan, full_plan['plan']
    assert plan.row_count == 3660 and plan.part_count > 1
    assert plan.digest == hashlib.sha256(state['row']['payload']).hexdigest()
    assert plan.size == len(state['row']['payload'])
    assert len(plan.units) == plan.part_count + 3660
    assert sum(unit[0] == 'tv_continuation_receipts' for unit in plan.units) == 60
    assert sum(unit[0] == 'tv_continuation_items' for unit in plan.units) == 3600
    raw = ''.join(preparation.files.read(*reference, surface=SURFACE, chunk_key=str(position))
                  for kind, position, reference in plan.units if kind == 'file')
    assert raw.encode() == state['row']['payload']
    assert json.loads(raw) == state['value']


def test_actual_128_unit_slices_copy_full_receipt_budget_without_selecting_partial_state(full_plan):
    state, conn = full_plan, full_plan['conn']

    def planned(connection, row):
        assert connection is conn and row == state['row']
        assert not conn.in_transaction
        return state['plan']

    # Planning/files have their genuine full-size owner above. Exercise the
    # real coordinator, checkpoints and copy_batch with that exact result.
    planner = Mock(side_effect=planned)
    job = replace(JOB, prepare=planner)
    tables = [preparation.relation(name) for name in preparation.RELATIONS[SURFACE]] + [preparation.tables(SURFACE)[2]]
    for _ in range(40):
        before = sum(conn.execute('SELECT count(*) FROM ' + table).fetchone()[0] for table in tables)
        progress = job.step(conn, batch_size=128)
        after = sum(conn.execute('SELECT count(*) FROM ' + table).fetchone()[0] for table in tables)
        assert 0 <= after - before <= 128
        if progress.status == 'prepared':
            break
        assert head(conn)['preparation_revision'] == state['previous']['preparation_revision']
    assert progress.status == 'prepared'
    assert planner.call_count == (len(state['plan'].units) + 127) // 128
    assert dict(head(conn))['row_count'] == 3660
    assert head(conn)['preparation_revision'] != state['previous']['preparation_revision']
    assert conn.execute('SELECT count(*) FROM ' + preparation.relation('tv_continuation_items')).fetchone()[0] == 3600
    assert not conn.in_transaction and not conn.execute('PRAGMA foreign_key_check').fetchone()


def test_completed_full_generation_and_prior_original_remain_exact(full_plan):
    state, conn = full_plan, full_plan['conn']
    current = head(conn)
    assert current['row_count'] == 3660 and current['part_count'] > 1
    assert preparation.read_original(conn, current).encode() == state['row']['payload']
    assert json.loads(preparation.read_original(conn, current)) == state['value']
    assert preparation.read_original(conn, state['previous']) == '{}'
    assert not conn.execute('PRAGMA foreign_key_check').fetchone()
