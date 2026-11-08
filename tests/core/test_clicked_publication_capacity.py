"""Full clicked rank capacity across real preparation, publication and repair."""
from contextlib import closing
import json
from pathlib import Path
import sqlite3

import pytest

from tests.core.test_ranked_state_preparation import seed
from tests.genre_catalog_support import bound_catalog
from tests.prepared_fixture_support import capture_template, prepared_template
from tests.store_fixture_support import install_numbered_input
from yt_playlist.core import library_pool_state, schema_inspection
from yt_playlist.core.migrations import regular, runner
from yt_playlist.core.preparation import clicked_publication as publication, ranked_state as preparation


SURFACE = 'home_pool:wheelhouse'
OWNER = publication.OWNER


@pytest.fixture(autouse=True)
def disable_periodic_library_sync():
    """SQL/file publication owners do not start application workers."""


@pytest.fixture(scope='module', autouse=True)
def isolated_app_home(tmp_path_factory):
    with pytest.MonkeyPatch.context() as patch:
        patch.setenv('YT_PLAYLIST_HOME', str(tmp_path_factory.mktemp('clicked-capacity-home')))
        patch.setenv('YT_PLAYLIST_AUDIO_VIBES', '0')
        patch.setenv('YT_PLAYLIST_AUDIO_VIBE_MODEL', '')
        patch.delenv('DEEPSEEK_API_KEY', raising=False)
        yield


@pytest.fixture(scope='module')
def capacity_input(tmp_path_factory):
    path = tmp_path_factory.mktemp('clicked-capacity-schema') / '93.db'
    with closing(sqlite3.connect(':memory:')) as schema, pytest.MonkeyPatch.context() as patch:
        bound_catalog(patch)
        install_numbered_input(schema, 93, regular_owners=regular.CHANGES)
        with prepared_template(schema, lambda store: None, database=path, defer_metadata=True) as template:
            yield template


def test_capacity_input_admits_numbered_history_wal_and_revision_guards(capacity_input):
    source = capacity_input.source
    with closing(sqlite3.connect(source._db_path.as_uri() + '?mode=ro', uri=True)) as reader:
        reader.row_factory = sqlite3.Row
        runner.validate_version(reader, 93)
        regular.validate(reader)
        assert reader.execute('SELECT COUNT(*) FROM rec_proposals').fetchone()[0] == 0
    assert source.conn.execute('PRAGMA journal_mode').fetchone()[0] == 'wal'
    assert capacity_input.revision_guards


@pytest.fixture(scope='module')
def capacity_catalog(capacity_input, tmp_path_factory):
    with capacity_input.open(tmp_path_factory.mktemp('clicked-capacity-catalog') / 'catalog.db') as store:
        originals, _ = seed(store, lane_counts={'wheelhouse': 8192})
        yield capture_template(store, originals)


def test_capacity_catalog_retains_full_8192_budget_and_three_nonempty_lanes(capacity_catalog):
    rows = dict(capacity_catalog.source.conn.execute(
        "SELECT surface,payload FROM rec_proposals WHERE surface GLOB 'home_pool:*'"))
    assert set(rows) == set(library_pool_state.SURFACES)
    for surface, raw in rows.items():
        tracks = json.loads(raw)['tracks']
        assert len(tracks) == (8192 if surface == SURFACE else 4)
        assert tracks[0]['key'] == 'known|artist'
        assert raw == capacity_catalog.result[surface]


@pytest.fixture(scope='module')
def capacity_short_lanes(capacity_catalog, tmp_path_factory):
    with capacity_catalog.open(tmp_path_factory.mktemp('clicked-capacity-checkpoint') / 'checkpoint.db') as store, \
            schema_inspection.reuse_tables(store.conn):
        OWNER.worker.install(store.conn)
        for _ in range(3):
            OWNER.worker.step(store.conn, batch_size=512)
        yield store


def test_capacity_preparation_keeps_three_nonempty_lanes_before_the_full_pool(capacity_short_lanes):
    store = capacity_short_lanes
    heads = {row[0] for row in store.conn.execute('SELECT surface FROM prepared_clicked_pools_heads')}
    assert heads == set(library_pool_state.SURFACES) - {SURFACE}
    assert store.conn.execute('SELECT COUNT(*) FROM prepared_clicked_pools_candidates').fetchone()[0] == 12
    assert OWNER.selected(store.conn) is None


@pytest.fixture(scope='module')
def capacity_checkpoint(capacity_short_lanes):
    progress = OWNER.worker.step(capacity_short_lanes.conn, batch_size=512)
    cursor = json.loads(progress.cursor)
    assert json.loads(cursor['key']) == [SURFACE] and cursor['offset'] == 512
    return capacity_short_lanes


def test_capacity_preparation_keeps_a_paged_first_checkpoint_without_publication(capacity_checkpoint):
    store = capacity_checkpoint
    progress = OWNER.worker.progress(store.conn)
    cursor = json.loads(progress.cursor)
    assert cursor['offset'] == 512 and cursor['generation'] == progress.input_revision
    assert progress.status != 'prepared' and OWNER.selected(store.conn) is None
    header = store.conn.execute('SELECT * FROM prepared_clicked_pools_generations WHERE surface=?', (SURFACE,)).fetchone()
    plan = preparation.document_plan.load(SURFACE, tuple(header[key] for key in ('plan_filename', 'plan_digest', 'plan_bytes')))
    assert len(plan.units) > 8192
    assert preparation.head(store.conn, SURFACE) is None


@pytest.fixture(scope='module')
def capacity_prepared(capacity_checkpoint, capacity_catalog):
    # Continue the persisted checkpoint before freezing the completed producer
    # once. Publication and damage branches each receive their own private copy.
    store = capacity_checkpoint
    for _ in range(32):
        if OWNER.worker.step(store.conn, batch_size=512).status == 'prepared':
            break
    else:
        pytest.fail('Full ranked pool did not finish bounded continuation')
    return capture_template(store, capacity_catalog.result)


def test_capacity_continuation_keeps_all_8192_ranks_and_foreign_keys(capacity_prepared):
    store = capacity_prepared.source
    header = preparation.head(store.conn, SURFACE)
    rows = store.conn.execute('SELECT rank,recording_id,raw_key,score FROM prepared_clicked_pools_candidates '
        'WHERE surface=? AND preparation_revision=? ORDER BY rank', (SURFACE, header['preparation_revision'])).fetchall()
    expected = json.loads(capacity_prepared.result[SURFACE])['tracks']
    assert len(rows) == len(expected) == 8192
    assert [row['rank'] for row in rows] == list(range(8192))
    assert rows[0]['recording_id'] is not None and rows[0]['raw_key'] is None
    assert [row['raw_key'] for row in rows[1:]] == [item['key'] for item in expected[1:]]
    assert [row['score'] for row in rows] == [item['score'] for item in expected]
    assert OWNER.worker.progress(store.conn).status == 'prepared'
    assert not store.conn.execute('PRAGMA foreign_key_check').fetchall()


@pytest.fixture(scope='module')
def capacity_map(capacity_prepared, tmp_path_factory):
    with capacity_prepared.open(tmp_path_factory.mktemp('clicked-capacity-map') / 'mapped.db') as store, \
            schema_inspection.reuse_tables(store.conn):
        stage = OWNER.step(store.conn)
        assert stage.complete and stage.copied == 4
        yield store, stage.id


def test_capacity_capture_uses_indexed_stage_and_generation_lookups(capacity_map):
    store, stage = capacity_map
    assert OWNER.selected(store.conn) is None
    header = OWNER.capture(store.conn, stage, SURFACE)
    assert header is not None
    query = store.conn.execute('EXPLAIN QUERY PLAN SELECT g.* FROM ' + OWNER.table('map') + ' m JOIN ' + OWNER.header +
        ' g ON g.surface=m.source AND g.preparation_revision=m.generation WHERE m.stage=? AND m.source=?',
        (stage, SURFACE)).fetchall()
    assert query and all('SCAN' not in row[-1] for row in query)


@pytest.fixture(scope='module')
def capacity_published(capacity_map):
    store, stage = capacity_map
    publish_capacity(store, stage)
    return store, stage


def publish_capacity(store, stage):
    # The verifier owns every real file and SQL member. Per-query diagnostic
    # instrumentation has separate tests and need not wrap all 8192 lookups.
    with closing(sqlite3.connect(store._db_path)) as conn, schema_inspection.reuse_tables(conn):
        conn.row_factory = sqlite3.Row
        conn.execute('PRAGMA foreign_keys=ON')
        conn.execute('PRAGMA synchronous=NORMAL')
        OWNER.publish(conn, stage=stage)
        assert not conn.in_transaction


def test_capacity_publication_verifies_every_real_member_before_selecting(capacity_published):
    store, stage = capacity_published
    assert OWNER.selected(store.conn) == stage
    assert store.conn.execute('SELECT COUNT(*) FROM ' + OWNER.table('generations')).fetchone()[0] == 1
    assert not store.conn.in_transaction


def test_capacity_publication_preserves_every_original_byte(capacity_published, capacity_prepared):
    store, stage = capacity_published
    for surface in library_pool_state.SURFACES:
        assert OWNER.value(store.conn, stage, surface) == capacity_prepared.result[surface]


def test_capacity_repository_reads_the_complete_ranked_set(capacity_published, capacity_prepared):
    store, _ = capacity_published
    result = store.rec_proposals.get_proposals(SURFACE)
    assert result == json.loads(capacity_prepared.result[SURFACE])
    assert len(result['tracks']) == 8192


@pytest.fixture(scope='module')
def capacity_damaged(capacity_prepared, tmp_path_factory):
    with capacity_prepared.open(tmp_path_factory.mktemp('clicked-capacity-damage') / 'damaged.db') as store, \
            schema_inspection.reuse_tables(store.conn):
        store.conn.execute('PRAGMA synchronous=NORMAL')
        store.conn.execute('PRAGMA wal_autocheckpoint=0')
        stage = OWNER.step(store.conn).id
        header = OWNER.capture(store.conn, stage, SURFACE)
        reference = tuple(header[key] for key in ('plan_filename', 'plan_digest', 'plan_bytes'))
        plan = preparation.document_plan.load(SURFACE, reference)
        missing = Path(plan.units.indexes[0][1])
        missing.unlink()
        yield store, stage, reference, missing


def test_capacity_plan_damage_is_private_and_leaves_the_full_source_available(capacity_damaged, capacity_prepared):
    store, stage, reference, missing = capacity_damaged
    assert not missing.exists()
    assert preparation.document_plan.load(SURFACE, reference).fields['unit_count'] > 8192
    source_header = preparation.head(capacity_prepared.source.conn, SURFACE)
    source_plan = preparation.document_plan.load(SURFACE, tuple(source_header[key]
        for key in ('plan_filename', 'plan_digest', 'plan_bytes')))
    assert Path(source_plan.units.indexes[0][1]).is_file()
    assert len(json.loads(OWNER.value(store.conn, stage, SURFACE))['tracks']) == 8192


@pytest.fixture(scope='module')
def capacity_rejected(capacity_damaged):
    store, stage, _, _ = capacity_damaged
    with pytest.raises(OSError):
        OWNER.publish(store.conn, stage=stage)
    return capacity_damaged


def test_capacity_publication_rejects_a_missing_plan_index_without_partial_selection(capacity_rejected):
    store, _, _, _ = capacity_rejected
    assert OWNER.selected(store.conn) is None
    assert not store.conn.execute('SELECT 1 FROM ' + OWNER.table('generations')).fetchone()
    assert not store.conn.in_transaction


@pytest.fixture(scope='module')
def capacity_repaired(capacity_rejected):
    store, _, _, _ = capacity_rejected
    regular.migrate(store.conn)
    publication.repair(store.conn, SURFACE)
    return capacity_rejected


def test_capacity_repair_rebuilds_the_exact_manifest_after_regular_ddl(capacity_repaired):
    store, stage, reference, missing = capacity_repaired
    assert missing.is_file()
    header = OWNER.capture(store.conn, stage, SURFACE)
    assert tuple(header[key] for key in ('plan_filename', 'plan_digest', 'plan_bytes')) == reference
    plan = preparation.document_plan.load(SURFACE, reference)
    assert len(plan.units) > 8192 and plan.units[512][0] == 'candidate'
    regular.validate(store.conn)


@pytest.fixture(scope='module')
def capacity_republished(capacity_repaired):
    store, stage, _, _ = capacity_repaired
    publish_capacity(store, stage)
    return store, stage


def test_capacity_repaired_plan_passes_real_publication_again(capacity_republished):
    store, stage = capacity_republished
    assert OWNER.selected(store.conn) == stage
    assert not store.conn.execute('PRAGMA foreign_key_check').fetchall()


def test_capacity_recovery_keeps_every_original_rank_readable(capacity_republished, capacity_prepared):
    store, stage = capacity_republished
    assert OWNER.value(store.conn, stage, SURFACE) == capacity_prepared.result[SURFACE]
    assert store.rec_proposals.get_proposals(SURFACE) == json.loads(capacity_prepared.result[SURFACE])
