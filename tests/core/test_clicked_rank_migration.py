"""Clicked cutover retains complete ranks using completed catalog inputs."""
from contextlib import closing
import sqlite3

import pytest

from tests.core.test_tv_state_migration import cutover
from tests.genre_catalog_support import bound_catalog
from tests.prepared_fixture_support import prepared_template
from tests.recommendation_support import seed_publication_subjects
from tests.store_fixture_support import install_numbered_input
from yt_playlist.core.migrations import regular, runner


@pytest.fixture(autouse=True)
def disable_periodic_library_sync():
    """SQL migration owners do not start an application or sync worker."""


@pytest.fixture(scope='module')
def clicked_input(tmp_path_factory):
    path = tmp_path_factory.mktemp('clicked-installed-input') / '94.db'
    with closing(sqlite3.connect(':memory:')) as schema, pytest.MonkeyPatch.context() as patch:
        bound_catalog(patch)
        install_numbered_input(schema, 94, regular_owners=regular.CHANGES)
        with prepared_template(schema, lambda source: None, database=path, defer_metadata=True) as template:
            yield template


def test_clicked_input_admits_numbered_history_and_real_wal_guards(clicked_input):
    source = clicked_input.source
    with closing(sqlite3.connect(source._db_path.as_uri() + '?mode=ro', uri=True)) as reader:
        reader.row_factory = sqlite3.Row
        runner.validate_version(reader, 94)
        regular.validate(reader)
        assert reader.execute('SELECT COUNT(*) FROM recordings').fetchone()[0] == 0
    assert source.conn.execute('PRAGMA journal_mode').fetchone()[0] == 'wal'
    assert clicked_input.revision_guards


def test_clicked_cutover_keeps_complete_ranks_parameters_unknowns_and_observation_time(clicked_input, tmp_path):
    with clicked_input.open(tmp_path / 'clicked.db') as store:
        store.rec_ledger.record_serving_policy('original-pool', {'algorithm': 'retained'}, 1)
        # The cutover consumes an already admitted catalog. Provider ingestion
        # and its metadata deliveries have independent catalog owner tests.
        seed_publication_subjects(store, [
            {'key': f'song {index}|artist', 'video_id': f'video{index}',
             'title': f'Song {index}', 'artist': 'Artist', 'duration_s': 100}
            for index in range(515)])
        assert store.conn.execute('SELECT COUNT(*) FROM catalog_entries').fetchone()[0] == 515
        items = [{'key': f'song {index}|artist', 'score': 1 / (index + 1),
                  'pool_policy': 'original-pool', 'reason': 'Original explanation'}
                 for index in range(515)]
        items.append({'key': 'unknown|artist', 'pool_policy': 'original-pool', 'score': .1})
        parameters = {'lane': 'wheelhouse', 'epoch': 4,
                      'include_genres': ['unknown-historical-genre'], 'avoid_genres': []}
        surface = 'home_pool:wheelhouse'
        store.rec_proposals.put_proposals(surface, {'parameters': parameters, 'tracks': items}, None)
        cutover(store)
        result = store.rec_proposals.get_proposals(surface)
        assert result['parameters'] == parameters
        assert [track['key'] for track in result['tracks']] == [track['key'] for track in items[:-1]]
        assert [track['score'] for track in result['tracks']] == [track['score'] for track in items[:-1]]
        assert len(result['tracks']) == 515
        assert all(track['pool_policy'] == 'original-pool' for track in result['tracks'])
        assert all(track['reason'] == 'Original explanation' for track in result['tracks'])
        assert store.conn.execute('SELECT built_at FROM clicked_library_pools').fetchone()[0] is None
        assert tuple(store.conn.execute('SELECT genre_key,resolution_reason FROM clicked_library_genres').fetchone()) == (
            None, 'unknown_genre')
        assert store.conn.execute('SELECT raw_key FROM unresolved_recommendation_candidates').fetchone()[0] == 'unknown|artist'
        with pytest.raises(sqlite3.IntegrityError, match='immutable'), store.conn.atomic():
            store.conn.execute('UPDATE clicked_library_pools SET epoch=5')
        with pytest.raises(sqlite3.IntegrityError, match='retained'), store.conn.atomic():
            store.conn.execute('DELETE FROM clicked_library_genres')
        assert not store.conn.execute('PRAGMA foreign_key_check').fetchall()
