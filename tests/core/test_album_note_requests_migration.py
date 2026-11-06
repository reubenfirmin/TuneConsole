"""Release-note demand survives output expiry and fences superseded workers."""
from contextlib import closing
from concurrent.futures import ThreadPoolExecutor
import json
import sqlite3
from threading import Barrier

import pytest

from yt_playlist.core import album_note_requests as owner, albums, recommendation_outputs as outputs
from yt_playlist.core.migrations import current, migrate, registry
from yt_playlist.core.migrations.runner import MigrationError
from yt_playlist.core.store import Store

ALBUM = {'browse_id': 'release-one', 'artist': 'Artist', 'title': 'Release',
         'year': '2020', 'thumbnail': None}


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


def test_registered_upgrade_retains_order_values_and_exact_provider_owner(predecessor):
    second = {**ALBUM, 'browse_id': 'unadmitted', 'year': 1999, 'requested_until': 1800}
    value = {ALBUM['browse_id']: ALBUM, second['browse_id']: second}
    seed(predecessor, value)
    migrate(predecessor.conn, registry=registry.MIGRATIONS)
    predecessor._bind_repositories()
    assert predecessor.rec_proposals.get_proposals('album_note_requests') == value
    rows = list(predecessor.conn.execute('SELECT * FROM album_note_requests ORDER BY id'))
    assert rows[0]['album_provider_id'] == ALBUM['browse_id']
    assert rows[0]['unresolved_reason'] is None
    assert rows[1]['album_provider_id'] is None
    assert rows[1]['unresolved_reason'] == 'provider_album_not_admitted'
    assert all(row['requested_at'] is None and row['updated_at'] == 1234.5 for row in rows)
    assert not predecessor.conn.execute("SELECT 1 FROM rec_proposals WHERE surface='album_note_requests'").fetchone()
    with predecessor.conn:
        predecessor.conn.execute('DELETE FROM rec_proposals')
    reopened = Store(predecessor._db_path)
    try:
        reopened.init_schema()
        assert reopened.rec_proposals.get_proposals('album_note_requests') == value
        assert not reopened.conn.execute('PRAGMA foreign_key_check').fetchall()
    finally:
        reopened.conn.close()


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
    store.rec_proposals.request_album_notes([ALBUM], 100)
    album, first = store.rec_proposals.next_album_note_request()
    store.rec_proposals.request_album_notes([{**ALBUM, 'year': '2021'}], 101, visible=True)
    assert not store.rec_proposals.publish_album_note(album['browse_id'], {'old': True}, 102,
                                                      expected_request=first)
    assert store.rec_proposals.get_proposals('album_notes') is None
    assert store.rec_proposals.get_proposals('album_note_requests')[album['browse_id']]['requested_until'] == 1001
    _, second = store.rec_proposals.next_album_note_request()
    assert store.rec_proposals.publish_album_note(album['browse_id'], {'fresh': True}, 103,
                                                 expected_request=second)
    store.rec_proposals.request_album_notes([ALBUM], 104)
    assert not store.rec_proposals.publish_album_note(album['browse_id'], {'stale': True}, 105,
                                                      expected_request=second)
    assert store.rec_proposals.get_proposals('album_notes') == {album['browse_id']: {'fresh': True}}
    assert store.rec_proposals.next_album_note_request()[1][0] != second[0]


def test_note_file_cutover_backup_and_restart_preserve_last_good_and_demand(predecessor, tmp_path):
    value = {ALBUM['browse_id']: {'label': 'Label', 'description': None, 'at': 100,
                                'links': [{'url': 'https://example.com/release', 'label': 'Release'}]}}
    seed(predecessor, {ALBUM['browse_id']: ALBUM})
    with predecessor.conn:
        predecessor.conn.execute('INSERT INTO rec_proposals VALUES (?,?,?)', ('album_notes', json.dumps(value), 99.5))
    migrate(predecessor.conn, registry=registry.MIGRATIONS)
    predecessor._bind_repositories()
    assert predecessor.rec_proposals.get_proposals('album_notes') == value
    row = predecessor.conn.execute('SELECT * FROM album_note_outputs').fetchone()
    assert row['album_provider_id'] == ALBUM['browse_id'] and row['built_at'] == 99.5
    with predecessor.read_view() as reader:
        manifest = outputs.export(reader.conn, tmp_path / 'backup')
    original = row['filename']
    from pathlib import Path
    Path(original).unlink()
    outputs.restore(predecessor.conn, manifest)
    assert predecessor.rec_proposals.get_proposals('album_notes') == value
    with predecessor.conn:
        predecessor.conn.execute('DELETE FROM rec_proposals')
    reopened = Store(predecessor._db_path)
    try:
        reopened.init_schema()
        assert reopened.rec_proposals.get_proposals('album_notes') == value
        assert reopened.rec_proposals.get_proposals('album_note_requests') == {ALBUM['browse_id']: ALBUM}
        assert outputs.collect_orphans(reopened.conn) == 0
    finally:
        reopened.conn.close()


def test_note_publication_file_failure_does_not_acknowledge_request_or_erase_last_good(store, monkeypatch):
    store.rec_proposals.publish_album_note(ALBUM['browse_id'], {'label': 'Last good'}, 100)
    store.rec_proposals.request_album_notes([ALBUM], 101)
    _, token = store.rec_proposals.next_album_note_request()
    def disk_failure(*args):
        raise OSError('Interrupted durable output write')
    monkeypatch.setattr(outputs, 'publish', disk_failure)
    with pytest.raises(OSError, match='Interrupted'):
        store.rec_proposals.publish_album_note(ALBUM['browse_id'], {'label': 'Replacement'}, 102,
                                               expected_request=token)
    assert store.rec_proposals.next_album_note_request()[1] == token
    assert store.rec_proposals.get_proposals('album_notes') == {ALBUM['browse_id']: {'label': 'Last good'}}


def test_note_cache_eviction_keeps_visible_demand_and_reader_files(store):
    store.rec_proposals.publish_album_note(ALBUM['browse_id'], {'label': 'Original'}, 100)
    store.rec_proposals.request_album_notes([ALBUM], 101, visible=True)
    with store.read_view() as reader:
        for number in range(128):
            store.rec_proposals.publish_album_note(f'other-{number}', {'label': str(number)}, 102)
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
    store.rec_proposals.request_album_notes([ALBUM], 100)
    _, token = store.rec_proposals.next_album_note_request()
    store.rec_proposals.request_album_notes([ALBUM], 101)
    assert store.rec_proposals.next_album_note_request()[1] == token
    store.conn.execute('BEGIN IMMEDIATE')
    store.rec_proposals.publish_album_note(ALBUM['browse_id'], {'fresh': True}, 102, expected_request=token)
    store.conn.rollback()
    assert store.rec_proposals.next_album_note_request()[1] == token
    assert store.rec_proposals.get_proposals('album_notes') is None


def test_visible_polling_extends_visibility_without_starving_an_inflight_worker(store):
    store.rec_proposals.request_album_notes([ALBUM], 100, visible=True)
    _, token = store.rec_proposals.next_album_note_request()
    for now in range(102, 120, 2):
        store.rec_proposals.request_album_notes([ALBUM], now, visible=True)
        assert store.rec_proposals.next_album_note_request()[1] == token
    assert store.rec_proposals.get_proposals('album_note_requests')[ALBUM['browse_id']]['requested_until'] == 1018
    assert store.rec_proposals.publish_album_note(ALBUM['browse_id'], {'label': 'Current'}, 120, expected_request=token)


def test_two_connections_can_acknowledge_one_request_only_once(tmp_path):
    path = tmp_path / 'shared.db'
    stores = [Store(path), Store(path)]
    try:
        for store in stores:
            store.init_schema()
        stores[0].rec_proposals.request_album_notes([ALBUM], 100)
        _, token = stores[0].rec_proposals.next_album_note_request()
        barrier = Barrier(2)

        def finish(index):
            barrier.wait()
            return stores[index].rec_proposals.publish_album_note(ALBUM['browse_id'], {'worker': index}, 101,
                                                                  expected_request=token)

        with ThreadPoolExecutor(2) as pool:
            assert sorted(pool.map(finish, range(2))) == [False, True]
        assert not stores[0].rec_proposals.get_proposals('album_note_requests')
    finally:
        for store in stores:
            store.conn.close()


def test_sql_owner_shape_revision_and_retired_bucket_are_enforced(store):
    store.rec_proposals.request_album_notes([ALBUM], 100)
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
    store.rec_proposals.request_album_notes([ALBUM], 100)
    _, token = store.rec_proposals.next_album_note_request()
    setattr(store, pause_flag, True)
    before = store.conn.total_changes
    store.rec_proposals.request_album_notes([], 200, current_only=True)
    assert not store.rec_proposals.publish_album_note(ALBUM['browse_id'], {}, 201, expected_request=token)
    assert store.conn.total_changes == before
    assert store.rec_proposals.next_album_note_request()[1] == token
