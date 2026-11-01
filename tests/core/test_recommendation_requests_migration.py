"""Demand has explicit ownership and survives disposable output replacement."""
import json
import sqlite3

import pytest

from yt_playlist.core import albums, recommendation_requests as owner
from yt_playlist.core.migrations import current, migrate, registry
from yt_playlist.core.migrations.runner import MigrationError
from yt_playlist.core.store import Store


@pytest.fixture
def predecessor(tmp_path):
    store = Store(tmp_path / 'demand.db')
    migrate(store.conn, registry=registry.MIGRATIONS[:86])
    store._bind_repositories()
    yield store
    store.conn.close()


def seed(store, values):
    with store.conn:
        albums.observe(store.conn, 'known-release', artist='Artist', title='Album')
        store.conn.execute("INSERT OR IGNORE INTO genre_family VALUES ('jazz','Jazz')")
        store.conn.executemany('INSERT INTO rec_proposals VALUES (?,?,?)',
                               [(surface, json.dumps(value), 123.5) for surface, value in values.items()])


def test_registered_cutover_retains_every_demand_field_and_unknown_reference(predecessor):
    values = {'choice_window:mixes': {'opportunity_id': 'external-offer', 'window': '07'},
              'album_menu_refresh': {'epoch': 2, 'exclude': ['known-release', 'missing-release', 'known-release']},
              'home_genre_request': {'epoch': 3, 'include_genres': ['jazz', 'other:unregistered style'],
                                     'avoid_genres': [], 'held_by_vibe': False, 'suspended_by_vibe': True}}
    seed(predecessor, values)
    migrate(predecessor.conn, registry=registry.MIGRATIONS)
    predecessor._bind_repositories()
    for surface, value in values.items():
        assert predecessor.rec_proposals.get_proposals(surface) == value
        assert not predecessor.conn.execute('SELECT 1 FROM rec_proposals WHERE surface=?', (surface,)).fetchone()
    assert [tuple(row) for row in predecessor.conn.execute(
        'SELECT album_provider_id,unresolved_reason FROM album_menu_refresh_exclusions ORDER BY position')] == [
            ('known-release', None), (None, 'provider_album_not_admitted'), ('known-release', None)]
    assert [tuple(row) for row in predecessor.conn.execute(
        'SELECT family_key,genre_key,resolution_reason FROM home_genre_request_items ORDER BY position')] == [
            ('jazz', None, None), (None, None, 'unknown_genre')]
    with predecessor.conn:
        predecessor.conn.execute('DELETE FROM rec_proposals')
    reopened = Store(predecessor._db_path)
    try:
        reopened.init_schema()
        assert {surface: reopened.rec_proposals.get_proposals(surface) for surface in values} == values
        assert not reopened.conn.execute('PRAGMA foreign_key_check').fetchall()
    finally:
        reopened.conn.close()


@pytest.mark.parametrize('surface,value', [
    ('home_genre_request', {'epoch': True, 'include_genres': ['jazz']}),
    ('home_genre_request', {'epoch': 1, 'held_by_vibe': 1}),
    ('home_genre_request', {'epoch': 1, 'unclassified': []}),
    ('home_genre_request', {'epoch': 1, 'avoid_genres': ['jazz'] * 101}),
    ('album_menu_refresh', {'epoch': -1, 'exclude': []}),
    ('album_menu_refresh', {'epoch': 0, 'exclude': [None]}),
    ('choice_window:albums', {'opportunity_id': 'offer', 'window': 1}),
    ('choice_window:radio', {'opportunity_id': 'offer', 'window': '1', 'extra': 'retain source'}),
])
def test_invalid_legacy_demand_stops_without_changing_source_or_schema(predecessor, surface, value):
    seed(predecessor, {surface: value})
    before = list(predecessor.conn.iterdump())
    with pytest.raises(MigrationError, match='original demand retained'):
        migrate(predecessor.conn, registry=registry.MIGRATIONS)
    assert list(predecessor.conn.iterdump()) == before


def test_demand_publication_participates_in_an_enclosing_transaction(store):
    store.rec_proposals.put_proposals('home_genre_request', {'epoch': 1, 'include_genres': ['unknown']}, 10)
    store.conn.execute('BEGIN IMMEDIATE')
    store.rec_proposals.publish_prepared_home(json.dumps({'epoch': 2, 'avoid_genres': ['jazz']}), 11,
                                               surface='home_genre_request')
    store.conn.rollback()
    assert store.rec_proposals.get_proposals('home_genre_request') == {'epoch': 1, 'include_genres': ['unknown']}
    store.rec_proposals.put_proposals('home_genre_request', {}, 12)
    assert store.rec_proposals.get_proposals('home_genre_request') == {}
    current.validate(store.conn)


def test_sql_rejects_retired_keys_invalid_references_and_orphan_demand_lists(store):
    store.rec_proposals.put_proposals('home_genre_request', {'epoch': 1, 'include_genres': ['jazz']}, 10)
    for sql in (
        "INSERT INTO rec_proposals VALUES ('home_genre_request','{}',1)",
        "UPDATE home_genre_request_items SET family_key='missing',genre_key=NULL,resolution_reason=NULL",
        'UPDATE home_genre_requests SET has_includes=0',
        "UPDATE home_genre_request_items SET kind='avoid'",
        "INSERT INTO listening_choice_windows VALUES ('unknown','offer','1',1)",
    ):
        with pytest.raises(sqlite3.IntegrityError), store.conn:
            store.conn.execute(sql)
