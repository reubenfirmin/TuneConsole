"""Full-size decision planning, bounded copying and publication retain custody."""
from contextlib import closing
from dataclasses import replace
import json
import sqlite3
from unittest.mock import Mock

import pytest

from tests.catalog_migration_support import catalog_reader
from tests.core.test_road_trip_draft_preparation import recipe
from tests.genre_catalog_support import bound_catalog
from tests.store_fixture_support import install_numbered_input
from yt_playlist.core.migrations import runner
from yt_playlist.core.preparation import draft_publication as publication, road_trip_drafts as preparation


@pytest.fixture(autouse=True)
def disable_periodic_library_sync():
    """These SQL/file owners do not construct an HTTP lifespan."""


@pytest.fixture(scope='module', autouse=True)
def isolated_app_home(tmp_path_factory):
    with pytest.MonkeyPatch.context() as patch:
        patch.setenv('YT_PLAYLIST_HOME', str(tmp_path_factory.mktemp('draft-budget-home')))
        patch.setenv('YT_PLAYLIST_AUDIO_VIBES', '0')
        patch.setenv('YT_PLAYLIST_AUDIO_VIBE_MODEL', '')
        patch.delenv('DEEPSEEK_API_KEY', raising=False)
        bound_catalog(patch)
        yield


@pytest.fixture(scope='module')
def installed(tmp_path_factory):
    path = tmp_path_factory.mktemp('draft-selection-budget') / 'installed.db'
    with closing(sqlite3.connect(path)) as conn:
        install_numbered_input(conn, 93)
    with catalog_reader(path, 93, snapshot_guards=True) as store:
        yield dict(store=store, path=path, conn=store.conn)


def test_full_budget_uses_the_real_supported_v93_input(installed):
    with closing(sqlite3.connect(installed['path'].as_uri() + '?mode=ro', uri=True)) as conn:
        conn.row_factory = sqlite3.Row
        runner.validate_version(conn, 93)
    assert installed['conn'].execute('PRAGMA foreign_keys').fetchone()[0] == 1


@pytest.fixture(scope='module')
def full_plan(installed):
    state, store = installed, installed['store']
    identity = recipe(store)
    value = {'revision': 'large', 'picks': ['repeated'] * 16384, 'banned': ['repeated'] * 16384}
    store.road_trip.save_road_trip_draft(identity, value, 9007199254740993)
    row = dict(preparation.JOB.definition._read(store.conn, 'road_trip_drafts', json.dumps([identity])))
    plan = preparation.prepare(store.conn, row)
    state.update(identity=identity, value=value, row=row, plan=plan,
                 original_source=tuple(publication.source_row(store.conn, identity)))
    return state


def test_real_full_selection_plan_publishes_every_original_part_without_losing_repetitions(full_plan):
    state, plan = full_plan, full_plan['plan']
    assert plan.item_count == 32768 and plan.part_count > 1
    assert [unit[2:] for unit in plan.units if unit[0] == 'item' and unit[1] == 'picked'] == [
        (position, 'repeated') for position in range(16384)]
    assert [unit[2:] for unit in plan.units if unit[0] == 'item' and unit[1] == 'excluded'] == [
        (position, 'repeated') for position in range(16384)]
    raw = ''.join(preparation.files.read(*unit[2], surface='road_trip_draft',
        chunk_key=f'{state["identity"]}:{unit[1]}') for unit in plan.units if unit[0] == 'file')
    assert raw.encode() == state['row']['state'] and json.loads(raw) == state['value']


def test_real_bounded_worker_copies_the_exact_prepared_32768_decisions(full_plan):
    state, conn = full_plan, full_plan['conn']

    def planned(connection, row):
        assert connection is conn and row == state['row']
        assert not conn.in_transaction
        return state['plan']

    planner = Mock(side_effect=planned)
    worker = replace(preparation.JOB, prepare=planner)
    owner = replace(publication.OWNER, worker=worker)
    worker.install(conn)
    for _ in range(80):
        before = conn.execute('SELECT count(*) FROM ' + preparation.ITEMS).fetchone()[0]
        progress = worker.step(conn, batch_size=512)
        after = conn.execute('SELECT count(*) FROM ' + preparation.ITEMS).fetchone()[0]
        assert 0 <= after - before <= 512
        if progress.status == 'prepared':
            break
        assert owner.selected(conn) is None
    assert progress.status == 'prepared'
    assert planner.call_count == (len(state['plan'].units) + 511) // 512
    assert conn.execute('SELECT count(*) FROM ' + preparation.ITEMS).fetchone()[0] == 32768
    assert not conn.in_transaction
    state['owner'] = owner


def test_real_map_and_publication_verify_all_full_budget_members_and_originals(full_plan):
    state, conn = full_plan, full_plan['conn']
    owner = state['owner']
    for _ in range(3):
        stage = owner.step(conn)
        if stage.complete:
            break
    assert stage.complete
    owner.publish(conn, stage=stage.id)
    header = owner.capture(conn, stage.id, state['identity'])
    assert header['item_count'] == 32768
    assert header['updated_at'] == state['original_source'][2]
    assert header['account_id'] is None and header['legacy_author_unknown'] == 1
    state.update(selected=stage.id, header=dict(header))


def test_completed_full_selection_keeps_order_repeated_uploads_and_unchanged_source(full_plan):
    state, conn = full_plan, full_plan['conn']
    for kind in ('picked', 'excluded'):
        members = list(map(tuple, conn.execute(
            f'SELECT position,video_id FROM {preparation.ITEMS} '
            'WHERE recipe_id=? AND generation=? AND selection_state=? ORDER BY position',
            (state['identity'], state['header']['generation'], kind))))
        assert members == [(position, 'repeated') for position in range(16384)]
    assert json.loads(state['owner'].value(conn, state['selected'], state['identity'])) == state['value']
    assert state['store'].road_trip.get_road_trip_draft(state['identity']) == state['value']
    assert tuple(publication.source_row(conn, state['identity'])) == state['original_source']
    assert not conn.in_transaction and not conn.execute('PRAGMA foreign_key_check').fetchone()
