"""Release-note demand survives output expiry and fences superseded workers."""
from contextlib import closing
from concurrent.futures import ThreadPoolExecutor
import json
import sqlite3
from threading import Barrier

import pytest

from tests.store_fixture_support import bounded_consumer_schema, initialize_consumer
from yt_playlist.core import album_note_requests as owner, albums, recommendation_outputs as outputs
from yt_playlist.core.migrations import current, migrate, registry
from yt_playlist.core.migrations.runner import MigrationError
from yt_playlist.core.store import Store

ALBUM = {'browse_id': 'release-one', 'artist': 'Artist', 'title': 'Release',
         'year': '2020', 'thumbnail': None}


@pytest.fixture(scope='module')
def acknowledgement_schema():
    with bounded_consumer_schema(()) as schema:
        yield schema


def test_acknowledgement_input_keeps_empty_demand_and_real_guards(acknowledgement_schema):
    assert acknowledgement_schema.execute('PRAGMA query_only').fetchone()[0] == 1
    assert acknowledgement_schema._revision_script
    assert not acknowledgement_schema.execute('SELECT 1 FROM album_note_requests').fetchone()
    assert not acknowledgement_schema.execute('PRAGMA foreign_key_check').fetchone()


@pytest.fixture
def predecessor(tmp_path, _pristine_installed_schema):
    store = Store(tmp_path / 'notes.db')
    with closing(store.conn):
        _pristine_installed_schema(83).backup(store.conn)
        store._bind_repositories()
        yield store


def seed(store, value):
    with store.conn:
        albums.observe(store.conn, ALBUM['browse_id'], title=ALBUM['title'], artist=ALBUM['artist'])
        store.conn.execute('INSERT INTO rec_proposals VALUES (?,?,?)',
                           ('album_note_requests', json.dumps(value), 1234.5))


@pytest.mark.parametrize('value', [None, [], {'wrong': ALBUM},
    {'release-one': {**ALBUM, 'extra': 'preserve unknown source'}},
    {'release-one': {**ALBUM, 'year': True}},
    {'release-one': {**ALBUM, 'requested_until': float('inf')}},
    {'release-one': {**ALBUM, 'artist': None}},
    {str(i): {**ALBUM, 'browse_id': str(i)} for i in range(25)}])
def test_preflight_failure_keeps_original_and_adds_no_owner(predecessor, value):
    seed(predecessor, value)
    before = list(predecessor.conn.iterdump())
    with pytest.raises(MigrationError, match='original retained'):
        migrate(predecessor.conn, registry=registry.MIGRATIONS)
    assert list(predecessor.conn.iterdump()) == before


def test_newer_visible_request_survives_old_worker_and_re_admission(store):
    store.album_notes.request([ALBUM], 100)
    album, first = store.album_notes.next_request()
    store.album_notes.request([{**ALBUM, 'year': '2021'}], 101, visible=True)
    assert not store.album_notes.publish(album['browse_id'], {'old': True}, 102,
                                                      expected_request=first)
    assert store.rec_proposals.get_proposals('album_notes') is None
    assert store.rec_proposals.get_proposals('album_note_requests')[album['browse_id']]['requested_until'] == 1001
    _, second = store.album_notes.next_request()
    assert store.album_notes.publish(album['browse_id'], {'fresh': True}, 103,
                                                 expected_request=second)
    store.album_notes.request([ALBUM], 104)
    assert not store.album_notes.publish(album['browse_id'], {'stale': True}, 105,
                                                      expected_request=second)
    assert store.rec_proposals.get_proposals('album_notes') == {album['browse_id']: {'fresh': True}}
    assert store.album_notes.next_request()[1][0] != second[0]


def test_note_publication_file_failure_does_not_acknowledge_request_or_erase_last_good(store, monkeypatch):
    store.album_notes.publish(ALBUM['browse_id'], {'label': 'Last good'}, 100)
    store.album_notes.request([ALBUM], 101)
    _, token = store.album_notes.next_request()
    def disk_failure(*args):
        raise OSError('Interrupted durable output write')
    monkeypatch.setattr(outputs, 'publish', disk_failure)
    with pytest.raises(OSError, match='Interrupted'):
        store.album_notes.publish(ALBUM['browse_id'], {'label': 'Replacement'}, 102,
                                               expected_request=token)
    assert store.album_notes.next_request()[1] == token
    assert store.rec_proposals.get_proposals('album_notes') == {ALBUM['browse_id']: {'label': 'Last good'}}


def test_note_cache_eviction_keeps_visible_demand_and_reader_files(store):
    store.album_notes.publish(ALBUM['browse_id'], {'label': 'Original'}, 100)
    store.album_notes.request([ALBUM], 101, visible=True)
    with store.read_view() as reader:
        for number in range(128):
            store.album_notes.publish(f'other-{number}', {'label': str(number)}, 102)
        assert len(store.rec_proposals.get_proposals('album_notes')) == 128
        assert ALBUM['browse_id'] not in store.rec_proposals.get_proposals('album_notes')
        assert outputs.collect_orphans(store.conn) == 0
        assert reader.rec_proposals.get_proposals('album_notes') == {ALBUM['browse_id']: {'label': 'Original'}}
    assert outputs.collect_orphans(store.conn) == 1
    assert ALBUM['browse_id'] in store.rec_proposals.get_proposals('album_note_requests')


@pytest.mark.parametrize('notes', [None, [], {'release-one': 'unclassified'}, {'release-one': {'bad': float('inf')}},
                                {str(i): {} for i in range(129)}])
def test_invalid_note_output_preserves_original_legacy_and_queue(predecessor, notes):
    seed(predecessor, {ALBUM['browse_id']: ALBUM})
    with predecessor.conn:
        predecessor.conn.execute('INSERT INTO rec_proposals VALUES (?,?,?)', ('album_notes', json.dumps(notes), 123))
    before = list(predecessor.conn.iterdump())
    with pytest.raises(MigrationError, match='original retained'):
        migrate(predecessor.conn, registry=registry.MIGRATIONS)
    assert list(predecessor.conn.iterdump()) == before


def test_unchanged_demand_keeps_its_completion_fence_and_joins_rollback(store):
    store.album_notes.request([ALBUM], 100)
    _, token = store.album_notes.next_request()
    store.album_notes.request([ALBUM], 101)
    assert store.album_notes.next_request()[1] == token
    store.conn.execute('BEGIN IMMEDIATE')
    store.album_notes.publish(ALBUM['browse_id'], {'fresh': True}, 102, expected_request=token)
    store.conn.rollback()
    assert store.album_notes.next_request()[1] == token
    assert store.rec_proposals.get_proposals('album_notes') is None


def test_visible_polling_extends_visibility_without_starving_an_inflight_worker(store):
    store.album_notes.request([ALBUM], 100, visible=True)
    _, token = store.album_notes.next_request()
    for now in range(102, 120, 2):
        store.album_notes.request([ALBUM], now, visible=True)
        assert store.album_notes.next_request()[1] == token
    assert store.rec_proposals.get_proposals('album_note_requests')[ALBUM['browse_id']]['requested_until'] == 1018
    assert store.album_notes.publish(ALBUM['browse_id'], {'label': 'Current'}, 120, expected_request=token)


def test_two_connections_can_acknowledge_one_request_only_once(tmp_path, acknowledgement_schema):
    # Admission has its own owners. The race needs two genuine writable
    # connections, repositories and revision guards on one private WAL database.
    path = tmp_path / 'acknowledgement.db'
    stores = [Store(path), Store(path)]
    try:
        for store in stores:
            store.conn.execute('PRAGMA synchronous=NORMAL')
            initialize_consumer(store, acknowledgement_schema)
        stores[0].album_notes.request([ALBUM], 100)
        _, token = stores[0].album_notes.next_request()
        barrier = Barrier(2)

        def finish(index):
            barrier.wait()
            return stores[index].album_notes.publish(ALBUM['browse_id'], {'worker': index}, 101,
                                                                  expected_request=token)

        with ThreadPoolExecutor(2) as pool:
            assert sorted(pool.map(finish, range(2))) == [False, True]
        assert not stores[0].rec_proposals.get_proposals('album_note_requests')
    finally:
        for store in stores:
            store.conn.close()


def test_sql_owner_shape_revision_and_retired_bucket_are_enforced(store):
    store.album_notes.request([ALBUM], 100)
    for sql, args in (
        ('UPDATE album_note_requests SET revision=0', ()),
        ('UPDATE album_note_requests SET title=?', ('Unfenced change',)),
        ('UPDATE album_note_requests SET unresolved_reason=NULL,revision=revision+1', ()),
        ('UPDATE album_note_requests SET requested_until=?,revision=revision+1', (float('inf'),)),
        ("INSERT INTO rec_proposals VALUES ('album_note_requests','{}',1)", ()),
    ):
        with pytest.raises(sqlite3.IntegrityError):
            with store.conn:
                store.conn.execute(sql, args)
    current.validate(store.conn)


@pytest.mark.parametrize('pause_flag', ['activity_paused', '_read_only'])
def test_paused_work_preserves_demand_without_publication(store, pause_flag):
    store.album_notes.request([ALBUM], 100)
    _, token = store.album_notes.next_request()
    setattr(store, pause_flag, True)
    before = store.conn.total_changes
    store.album_notes.request([], 200, current_only=True)
    assert not store.album_notes.publish(ALBUM['browse_id'], {}, 201, expected_request=token)
    assert store.conn.total_changes == before
    assert store.album_notes.next_request()[1] == token
