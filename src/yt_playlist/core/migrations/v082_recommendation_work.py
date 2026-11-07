"""Typed daily scheduling and TV preparation state, independent of display caches."""
import json
from pathlib import Path
import sqlite3

from yt_playlist.core.recommendation_work import parse, write
from .runner import MigrationError


OBJECTS = frozenset({'model_selection_schedule', 'tv_preparation_state',
                     'retired_recommendation_work_insert', 'retired_recommendation_work_update'})


def statements():
    pending = ''
    for line in Path(__file__).with_name('schema_recommendation_work.sql').read_text().splitlines(keepends=True):
        pending += line
        if sqlite3.complete_statement(pending):
            yield pending
            pending = ''
    if pending.strip():
        raise MigrationError('Incomplete recommendation work schema')


def apply(conn, context):
    originals = []
    for surface, raw, built_at in conn.execute(
            "SELECT surface,payload,built_at FROM rec_proposals WHERE surface IN ('daily_model_schedule','tv_preparation')"):
        try:
            value = json.loads(raw)
            parse(surface, value, built_at)
        except (ValueError, TypeError, OverflowError) as exc:
            raise MigrationError(f'Invalid {surface}; original work state retained: {exc}') from exc
        originals.append((surface, value, built_at))
    for sql in statements():
        conn.execute(sql)
    for surface, value, built_at in originals:
        write(conn, surface, value, built_at)
    conn.execute("DELETE FROM rec_proposals WHERE surface IN ('daily_model_schedule','tv_preparation')")


def validate(conn):
    from .current import _tokens
    for sql in statements():
        name = sql.split()[2]
        actual = conn.execute('SELECT sql FROM sqlite_schema WHERE name=?', (name,)).fetchone()
        if actual is None or _tokens(actual[0]) != _tokens(sql):
            raise MigrationError('Incompatible recommendation work owner: ' + name)
    if conn.execute("SELECT 1 FROM rec_proposals WHERE surface IN ('daily_model_schedule','tv_preparation')").fetchone():
        raise MigrationError('Retired recommendation work state remains in rec_proposals')
