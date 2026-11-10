"""Retain user demand and listening-window identity outside disposable output."""
import json
from pathlib import Path
import sqlite3

from yt_playlist.core import recommendation_requests
from .runner import MigrationError

OBJECTS = frozenset({'listening_choice_windows', 'album_menu_refresh_state', 'album_menu_refresh_exclusions',
                     'home_genre_requests', 'home_genre_request_items', 'home_genre_request_item_owner',
                     'album_menu_exclusion_owner', 'home_genre_request_item_update_owner', 'home_genre_request_list_owner',
                     'album_note_request_work_revision', 'retired_recommendation_request_insert',
                     'retired_recommendation_request_update'})


def statements():
    pending = ''
    for line in Path(__file__).with_name('schema_recommendation_requests.sql').read_text().splitlines(True):
        pending += line
        if sqlite3.complete_statement(pending):
            yield pending
            pending = ''
    if pending.strip():
        raise MigrationError('Incomplete recommendation request schema')


def apply(conn, context):
    sources = []
    for surface in recommendation_requests.SURFACES:
        row = conn.execute('SELECT payload,built_at FROM rec_proposals WHERE surface=?', (surface,)).fetchone()
        if row:
            try:
                value = json.loads(row[0])
                recommendation_requests.parse(surface, value, row[1])
            except (ValueError, KeyError, TypeError, OverflowError) as exc:
                raise MigrationError(f'Invalid {surface}; original demand retained: {exc}') from exc
            sources.append((surface, value, row[1]))
    for sql in statements():
        conn.execute(sql)
    conn.execute('UPDATE album_note_requests SET revision=revision+1,work_revision=revision')
    for surface, value, at in sources:
        recommendation_requests.write(conn, surface, value, at)
        conn.execute('DELETE FROM rec_proposals WHERE surface=?', (surface,))
