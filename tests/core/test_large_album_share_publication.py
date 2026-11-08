"""A full ordered share is prepared once before real continuing writer checks."""
import json
import sqlite3
from types import SimpleNamespace

import pytest

from tests.core.test_album_share_publication import complete_source, stage
from tests.core.test_album_owner_preparation import finish
from tests.store_fixture_support import reuse_reporting_reader
from yt_playlist.core import schema_inspection
from yt_playlist.core.preparation import album_shares as preparation, album_share_publication as publication


@pytest.fixture(autouse=True)
def disable_periodic_library_sync():
    """Album SQL and file publication do not start the HTTP lifecycle."""


def test_large_share_schema_input_keeps_real_history_and_snapshot_guards(_bounded_installed_schema):
    source = _bounded_installed_schema(93).source
    assert source.conn.execute('SELECT max(version) FROM schema_migrations').fetchone()[0] == 93
    assert source._snapshot_revisions.conn is source.conn
    assert not source.conn.execute('PRAGMA foreign_key_check').fetchone()


@pytest.fixture(scope='module')
def share(_bounded_installed_schema, tmp_path_factory):
    path = tmp_path_factory.mktemp('large-album-share')/'source.db'
    with _bounded_installed_schema(93).open(path) as store, pytest.MonkeyPatch.context() as patch:
        with reuse_reporting_reader(store, patch), schema_inspection.reuse_tables(store.conn):
            _, entry = complete_source(store, count=8192)
            yield SimpleNamespace(store=store, entry=entry)


def test_large_share_source_keeps_all_eight_thousand_repeated_positions(share):
    raw = share.store.conn.execute('SELECT video_ids_json FROM main.album_share_snapshots').fetchone()[0]
    assert json.loads(raw) == ['one'] * 8192
    assert share.store.collection.is_album_tracklist_complete('release')


@pytest.fixture(scope='module')
def prepared_share(share):
    preparation.JOB.install(share.store.conn)
    finish(preparation.JOB, share.store.conn, size=512)
    return share


def test_large_share_preparation_keeps_every_ordered_subject_and_exact_original(prepared_share):
    conn = prepared_share.store.conn
    assert conn.execute('SELECT count(*) FROM prepared_album_share_items').fetchone()[0] == 8192
    original = preparation.read_original(conn, preparation.head(conn, 'release'))
    assert json.loads(original['video_ids_json']) == ['one'] * 8192


@pytest.fixture(scope='module')
def mapped_share(prepared_share):
    for _ in range(100):
        result = publication.OWNER.step(prepared_share.store.conn, batch_size=128)
        if result.complete:
            prepared_share.selected = result.id
            return prepared_share
    pytest.fail('Large album share map did not finish bounded fixture')


def test_large_share_map_remains_unselected_until_actual_publication(mapped_share):
    assert publication.OWNER.selected(mapped_share.store.conn) is None


@pytest.fixture(scope='module')
def published_share(mapped_share):
    publication.OWNER.publish(mapped_share.store.conn, stage=mapped_share.selected)
    return mapped_share


def test_large_share_publication_selects_the_complete_real_map(published_share):
    assert publication.OWNER.selected(published_share.store.conn) == published_share.selected
    assert len(published_share.store.collection.cached_album_share('release')['video_ids']) == 8192


def test_real_cache_writer_fences_snapshot_and_preserves_the_full_archived_order(published_share):
    store = published_share.store
    key = store.conn.execute('SELECT r.identity_key FROM main.catalog_entries e JOIN main.recordings r '
        'ON r.id=e.recording_id WHERE e.id=?', (published_share.entry,)).fetchone()[0]
    with store.read_snapshot() as snapshot:
        assert len(snapshot.store.collection.cached_album_share('release')['video_ids']) == 8192
        store.collection.cache_album_tracklist('release', 'Changed', 'Artist', None, [key]*3,
            entries=[{'entry_id':published_share.entry}]*3, video_ids=['one','one','one'])
        assert not snapshot.publish(lambda:None)
    assert publication.OWNER.selected(store.conn) is None
    assert store.collection.cached_album_share('release') == {'title':'Changed', 'video_ids':['one']*3}
    publication.OWNER.publish(store.conn, stage=stage(store.conn))
    assert store.collection.cached_album_share('release') == {'title':'Changed', 'video_ids':['one']*3}
    assert len(json.loads(publication.OWNER.value(store.conn, published_share.selected, 'release')['video_ids_json'])) == 8192
    with pytest.raises(sqlite3.IntegrityError, match='retained'):
        with store.conn:
            store.conn.execute('DELETE FROM prepared_album_share_items')
