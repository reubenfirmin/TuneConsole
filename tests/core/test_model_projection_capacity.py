"""Complete Radio projection ranks, paging and capture use completed catalog inputs."""
from contextlib import closing
import sqlite3

import pytest

from tests.core.test_clicked_rank_migration import clicked_input
from tests.core.test_tv_state_migration import cutover
from tests.prepared_fixture_support import capture_template
from tests.recommendation_support import seed_publication_subjects
from yt_playlist.core import model_projections
from yt_playlist.core.migrations import regular, runner
from yt_playlist.rec.stored_radio import PreparedRadio


@pytest.fixture(autouse=True)
def disable_periodic_library_sync():
    """Projection SQL owners do not start a web application or sync worker."""


def test_projection_input_admits_real_v94_owners_and_revision_guards(clicked_input):
    conn = clicked_input.source.conn
    with closing(sqlite3.connect(clicked_input.source._db_path.as_uri() + '?mode=ro', uri=True)) as reader:
        reader.row_factory = sqlite3.Row
        runner.validate_version(reader, 94)
        regular.validate(reader)
    assert conn.execute('PRAGMA journal_mode').fetchone()[0] == 'wal'
    assert clicked_input.revision_guards


@pytest.fixture(scope='module')
def projection_source(clicked_input, tmp_path_factory):
    with clicked_input.open(tmp_path_factory.mktemp('projection-source') / 'source.db') as store:
        store.rec_ledger.record_serving_policy('original-pool', {'algorithm': 'retained'}, 1)
        run = store.model_selection.start_selection(1, {'kind': 'product'})
        # Metadata ingestion has its own catalog owners. This migration consumes
        # a completed 515-recording catalog, rather than 515 provider deliveries.
        seed_publication_subjects(store, [
            {'key': f'song {index}|artist', 'video_id': f'video{index}',
             'title': f'Song {index}', 'artist': 'Artist', 'duration_s': 100}
            for index in range(515)])
        items = [{'key': f'song {index}|artist', 'score': 1 / (index + 1), 'pool_policy': 'original-pool'}
                 for index in range(515)]
        keys = [item['key'] for item in items]
        modes = [{'mode_id': 1, 'space': '', 'centroid': [1., 0.], 'label': 'Original mode', 'size': 515}]
        value = {'policy_version': 'original-pool', 'context': {}, 'modes': modes, 'encoding_model': None,
                 'bundles': {'1': {'wheelhouse': items}, '_radio': {'space': '', 'generation': 'old',
                             'members': {'1': keys}, 'scores': {key: .3 for key in keys}},
                             '_ppr': {'1': list(reversed(keys))}}}
        surface = f'model_pair:{run}:challenger:radio'
        store.rec_proposals.put_proposals(surface, {'run_id': run, 'data': value}, None)
        yield capture_template(store, {'run': run, 'surface': surface, 'keys': keys, 'modes': modes})


def test_projection_source_keeps_all_515_candidates_and_original_mode_envelope(projection_source):
    store, expected = projection_source.source, projection_source.result
    assert store.conn.execute('SELECT count(*) FROM catalog_entries').fetchone()[0] == 515
    value = store.rec_proposals.get_proposals(expected['surface'])
    assert value['run_id'] == expected['run']
    assert value['data']['modes'] == expected['modes']
    assert value['data']['bundles']['_radio']['members']['1'] == expected['keys']
    assert value['data']['bundles']['_ppr']['1'] == list(reversed(expected['keys']))
    assert not store.conn.execute('PRAGMA foreign_key_check').fetchone()


@pytest.fixture(scope='module')
def projection_native(projection_source, tmp_path_factory):
    with projection_source.open(tmp_path_factory.mktemp('projection-native') / 'native.db') as store:
        cutover(store)
        yield store, projection_source.result


def test_projection_cutover_keeps_complete_typed_members_and_original_modes(projection_native):
    store, expected = projection_native
    surface = expected['surface']
    result = model_projections.read(store.conn, surface)['data']
    assert 'bundles' not in result and result['modes'] == expected['modes']
    generation = result['_ranked_generation']
    assert store.recommendations.pool(surface + ':radio', '1', 'membership', generation=generation)['eligible_count'] == 515
    assert store.conn.execute('SELECT built_at FROM model_pair_publications').fetchone()[0] is None
    assert tuple(store.conn.execute('SELECT mode_ref,resolution_reason FROM model_pair_modes').fetchone()) == (None, 'missing_mode')
    assert not store.conn.execute('PRAGMA foreign_key_check').fetchone()


def radio_for(store, expected):
    generation = model_projections.read(store.conn, expected['surface'])['data']['_ranked_generation']
    return PreparedRadio(store, 10, generation=generation, surface_prefix=expected['surface'] + ':')


def test_projection_positions_retain_first_and_last_ranks_and_page_past_512_rejections(projection_native):
    store, expected = projection_native
    keys = expected['keys']
    prepared = radio_for(store, expected)
    try:
        assert prepared.positions('1', [keys[0], keys[-1]]) == {keys[0]: 514, keys[-1]: 0}
        tracks = prepared.tracks('radio', '1', 'membership', allowed=lambda item: item['key'] == keys[-1], limit=1)
        assert [item['key'] for item in tracks] == [keys[-1]]
    finally:
        prepared.close()


def test_projection_capture_counts_all_members_and_bounds_decoded_radio_inputs(projection_native):
    store, expected = projection_native
    prepared = radio_for(store, expected)
    try:
        with store.read_view() as reader:
            captured = prepared.capture_inputs(reader, {'1': expected['modes'][0]}, '', 'new-current-model', set(), set(), .5)
        inputs = prepared.decode_inputs(captured)
        assert inputs['_radio']['members'] == {'1': 515}
        assert len(inputs['1']['_radio_pool']) <= 96
    finally:
        prepared.close()


def test_projection_keeps_mode_and_selection_run_immutability_and_foreign_keys(projection_native):
    store, expected = projection_native
    with pytest.raises(sqlite3.IntegrityError, match='immutable'), store.conn.atomic():
        store.conn.execute('DELETE FROM model_pair_modes')
    with pytest.raises(sqlite3.IntegrityError), store.conn.atomic():
        store.conn.execute('DELETE FROM rec_model_selection_runs WHERE id=?', (expected['run'],))
    assert not store.conn.execute('PRAGMA foreign_key_check').fetchone()
