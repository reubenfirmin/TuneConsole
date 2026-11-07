"""Retain the current offer deduplication window outside display caches."""
import json
from pathlib import Path
import sqlite3

from yt_playlist.core import home_offers
from .runner import MigrationError

OBJECTS = frozenset({'home_offer_dedup', 'home_offer_signatures', 'home_offer_signature_budget',
                     'retired_home_offer_insert', 'retired_home_offer_update'})


def statements():
    pending = ''
    for line in Path(__file__).with_name('schema_home_offers.sql').read_text().splitlines(keepends=True):
        pending += line
        if sqlite3.complete_statement(pending):
            yield pending
            pending = ''
    if pending.strip():
        raise MigrationError('Incomplete Home offer schema')


def apply(conn, context):
    row = conn.execute("SELECT payload,built_at FROM rec_proposals WHERE surface='home_shown'").fetchone()
    if row is not None:
        try:
            value = json.loads(row[0])
            home_offers.parse(value, row[1])
        except (ValueError, TypeError, OverflowError) as exc:
            raise MigrationError(f'Invalid Home offer window; original retained: {exc}') from exc
    for sql in statements():
        conn.execute(sql)
    if row is not None:
        home_offers.replace(conn, value, row[1])
        conn.execute("DELETE FROM rec_proposals WHERE surface='home_shown'")


def validate(conn):
    from .current import _tokens
    for sql in statements():
        name = sql.split()[2]
        actual = conn.execute('SELECT sql FROM sqlite_schema WHERE name=?', (name,)).fetchone()
        if actual is None or _tokens(actual[0]) != _tokens(sql):
            raise MigrationError('Incompatible Home offer owner: ' + name)
    if conn.execute("SELECT 1 FROM rec_proposals WHERE surface='home_shown'").fetchone():
        raise MigrationError('Retired Home offer deduplication remains in rec_proposals')
