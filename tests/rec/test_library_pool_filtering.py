"""Clicked temporal pools consume cut points and recheck current feedback."""
import pytest

from tests.conftest import seed_catalog_track
from tests.store_fixture_support import bounded_consumer_schema, file_consumer_store as store
from yt_playlist.core.db_writes import atomic_write
from yt_playlist.rec import library_pool, mode_surfaces
from yt_playlist.util.matching import identity_key


@pytest.fixture(autouse=True)
def disable_periodic_library_sync():
    """SQL/file filtering has no HTTP lifecycle."""


@pytest.fixture(scope='module')
def _pristine_current_schema():
    with bounded_consumer_schema((('jazz', 'Jazz'),)) as schema:
        yield schema


def test_temporal_pool_input_keeps_real_schema_and_private_snapshot_guards(_pristine_current_schema):
    assert _pristine_current_schema.execute('SELECT count(*) FROM schema_migrations').fetchone()[0] > 0
    assert _pristine_current_schema._revision_script
    assert not _pristine_current_schema.execute('PRAGMA foreign_key_check').fetchone()


def test_temporal_pool_keeps_its_date_band_and_live_feedback(store, monkeypatch):
    # Cut points are completed lower-model input. The real pool publication,
    # catalog metadata, feedback write and post-publication filter stay real.
    monkeypatch.setattr(mode_surfaces, 'bundle_metadata', lambda _: {'year_cuts': [1990, 2010]})
    keys = []
    with atomic_write(store):
        for i, year in enumerate([1960, 1980, 1995, 2005, 2010, 2020]):
            title, artist = f'Song {year}', f'Artist {i}'
            key = identity_key(title, artist)
            seed_catalog_track(store, key, title, artist, video_id=f'v{i}', genre='Jazz', mb_year=str(year))
            keys.append(key)
    parameters = {'lane': 'temporal', 'epoch': 1, 'include_genres': ['jazz'], 'avoid_genres': []}
    library_pool.prepare(store, 1, parameters)
    store.rec_model.record_feedback('for_you', keys[3], 'dismiss', now=2)
    assert [track['year'] for track in library_pool.pick(store, 3, parameters)] == [1995, 2010]
