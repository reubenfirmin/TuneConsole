"""Demand has explicit ownership and survives disposable output replacement."""
import json
import sqlite3

import pytest

from yt_playlist.core import albums, recommendation_requests as owner
from yt_playlist.core.migrations import current, migrate, registry
from yt_playlist.core.migrations.runner import MigrationError
from yt_playlist.core.store import Store


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
