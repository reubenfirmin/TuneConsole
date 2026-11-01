"""Preserve release-note demand outside disposable recommendation output."""
import json
from pathlib import Path
import sqlite3

from yt_playlist.core import album_note_outputs, album_note_requests
from .runner import MigrationError

OBJECTS = frozenset({'album_note_requests', 'album_note_request_budget', 'album_note_request_identity', 'album_note_request_revision',
                     'retired_album_note_request_insert', 'retired_album_note_request_update',
                     'album_note_outputs', 'album_note_output_budget', 'album_note_output_immutable',
                     'retired_album_note_output_insert', 'retired_album_note_output_update'})


def statements():
    pending = ''
    for line in Path(__file__).with_name('schema_album_note_requests.sql').read_text().splitlines(keepends=True):
        pending += line
        if sqlite3.complete_statement(pending):
            yield pending
            pending = ''
    if pending.strip():
        raise MigrationError('Incomplete album note request schema')


def apply(conn, context):
    row = conn.execute("SELECT payload,built_at FROM rec_proposals WHERE surface='album_note_requests'").fetchone()
    notes = conn.execute("SELECT payload,built_at FROM rec_proposals WHERE surface='album_notes'").fetchone()
    if row is not None:
        try:
            value = json.loads(row[0])
            album_note_requests.parse(value, row[1])
        except (ValueError, TypeError, OverflowError) as exc:
            raise MigrationError(f'Invalid album note demand; original retained: {exc}') from exc
    if notes is not None:
        try:
            note_value = json.loads(notes[0])
            # All files must be validated and durable before deleting the source.
            note_files = album_note_outputs.prepare(conn, note_value, notes[1])
        except (ValueError, TypeError, OverflowError) as exc:
            raise MigrationError(f'Invalid album note output; original retained: {exc}') from exc
    for sql in statements():
        conn.execute(sql)
    if row is not None:
        album_note_requests.replace(conn, value, row[1], historical=True)
        conn.execute("DELETE FROM rec_proposals WHERE surface='album_note_requests'")
    if notes is not None:
        for browse, filename, digest, size in note_files:
            album_note_outputs._insert(conn, browse, filename, digest, size, notes[1])
        conn.execute("DELETE FROM rec_proposals WHERE surface='album_notes'")


def validate(conn):
    from .current import _tokens
    for sql in statements():
        name = sql.split()[2]
        actual = conn.execute('SELECT sql FROM sqlite_schema WHERE name=?', (name,)).fetchone()
        if actual is None or _tokens(actual[0]) != _tokens(sql):
            raise MigrationError('Incompatible album note request owner: ' + name)
    if conn.execute("SELECT 1 FROM rec_proposals WHERE surface IN ('album_note_requests','album_notes')").fetchone():
        raise MigrationError('Retired album note demand remains in rec_proposals')
