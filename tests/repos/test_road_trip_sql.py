import sqlite3

import pytest

from yt_playlist.core import collection_recovery
from yt_playlist.core.store import Store


def recipe(store):
    store.accounts.upsert_account('Main', 'unused.json', None, True)
    return store.road_trip.save_road_trip_recipe(None, 'Trip', 50, [], [], 60, 1)


def test_file_write_is_outside_the_sql_transaction_and_repository_calls_never_read_files(store, monkeypatch):
    identity = recipe(store)
    publish = store.road_trip.recovery.publish
    def outside_sql(*args):
        assert not store.conn.in_transaction
        assert not store._lock.held_by_current_thread
        return publish(*args)
    monkeypatch.setattr(store.road_trip.recovery, 'publish', outside_sql)
    store.road_trip.save_road_trip_draft(identity, {'revision':'first', 'picks':['exact-video']}, 2)
    def forbidden(*args, **kwargs):
        raise AssertionError('A repository called file access')
    monkeypatch.setattr(collection_recovery, 'read', forbidden)
    facts = store.road_trip.repository.draft_facts(identity)
    assert facts['row']['revision'] == 'first'
    assert facts['selections'] == [('picked', 'exact-video')]


def test_worker_losing_its_revision_during_file_publication_cannot_overwrite_newer_decisions(file_backed_store, monkeypatch):
    store = file_backed_store
    identity = recipe(store)
    store.road_trip.save_road_trip_draft(identity, {'revision':'held', 'picks':['old-video']}, 2)
    other = Store(store._db_path)
    other.init_schema()
    publish = store.road_trip.recovery.publish
    def concurrent_user_edit(*args):
        reference = publish(*args)
        other.road_trip.save_road_trip_draft(identity, {'revision':'user-edit', 'picks':['user-video']}, 4)
        return reference
    monkeypatch.setattr(store.road_trip.recovery, 'publish', concurrent_user_edit)
    try:
        assert not store.road_trip.replace_road_trip_draft(identity,
            {'revision':'worker', 'picks':['worker-video']}, 3, expected_revision='held')
        assert store.road_trip.get_road_trip_draft(identity)['revision'] == 'user-edit'
        assert store.road_trip.get_road_trip_draft(identity)['picks'] == ['user-video']
    finally:
        other.conn.close()


def test_failed_file_publication_preserves_the_existing_draft_and_selections(store, monkeypatch):
    identity = recipe(store)
    store.road_trip.save_road_trip_draft(identity, {'revision':'held', 'picks':['old-video']}, 2)
    def fail(*args):
        raise OSError('controlled file failure')
    monkeypatch.setattr(store.road_trip.recovery, 'publish', fail)
    with pytest.raises(OSError, match='controlled file failure'):
        store.road_trip.save_road_trip_draft(identity, {'revision':'next', 'picks':['new-video']}, 3)
    assert store.road_trip.get_road_trip_draft(identity)['revision'] == 'held'
    assert store.road_trip.get_road_trip_draft(identity)['picks'] == ['old-video']


def test_duplicate_genre_rejection_cannot_create_custom_identities_or_replace_recipe(store):
    identity = recipe(store)
    with pytest.raises(ValueError, match='same genre twice'):
        store.road_trip.save_road_trip_recipe(identity, 'Changed', 60, [], ['New genre', 'NEW GENRE'], 90, 2)
    assert store.road_trip.get_road_trip_recipe(identity)['name'] == 'Trip'
    assert store.conn.execute("SELECT 1 FROM genre_catalog WHERE key='new genre'").fetchone() is None


def test_failed_selection_insert_rolls_back_the_draft_reference_and_previous_choices(store):
    identity = recipe(store)
    store.road_trip.save_road_trip_draft(identity, {'revision':'held', 'picks':['old-video']}, 2)
    before = dict(store.conn.execute('SELECT * FROM road_trip_drafts WHERE recipe_id=?', (identity,)).fetchone())
    store.conn.execute("CREATE TEMP TRIGGER reject_new_selection BEFORE INSERT ON road_trip_draft_entries "
        "WHEN NEW.video_id='new-video' BEGIN SELECT RAISE(ABORT,'controlled selection failure'); END")
    with pytest.raises(sqlite3.IntegrityError, match='controlled selection failure'):
        store.road_trip.save_road_trip_draft(identity, {'revision':'next', 'picks':['new-video']}, 3)
    assert dict(store.conn.execute('SELECT * FROM road_trip_drafts WHERE recipe_id=?', (identity,)).fetchone()) == before
    assert store.road_trip.get_road_trip_draft(identity)['picks'] == ['old-video']
