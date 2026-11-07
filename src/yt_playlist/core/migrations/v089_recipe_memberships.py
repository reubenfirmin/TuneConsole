"""Normalize ordered recipe preferences without guessing artist identities."""
import json
from pathlib import Path
import re
import sqlite3

from yt_playlist.core import recipe_memberships
from .runner import MigrationError


def transform(sql):
    if re.match(r'CREATE TABLE\s+"?road_trip_recipes"?\s*\(',sql.lstrip(),re.I):
        sql = re.sub(r'\b(?:artists|genres|blacklist_genres)\s+TEXT\s+NOT NULL\s*,(?:[ \t]*--[^\n]*)?', '', sql)
    return sql


def statements():
    pending = ''
    for line in Path(__file__).with_name('schema_recipe_memberships.sql').read_text().splitlines(True):
        pending += line
        if sqlite3.complete_statement(pending):
            yield pending
            pending = ''
    if pending.strip():
        raise MigrationError('Incomplete recipe membership schema')


def apply(conn, context):
    original = list(conn.execute('SELECT id,artists,genres,blacklist_genres FROM road_trip_recipes'))
    for sql in statements():
        conn.execute(sql)
    for recipe_id, artist_labels, include, exclude in original:
        try:
            recipe_memberships.replace(conn,recipe_id,json.loads(artist_labels),json.loads(include),json.loads(exclude),legacy=True)
        except (ValueError,TypeError) as exc:
            raise MigrationError(f'Invalid recipe {recipe_id}; original retained: {exc}') from exc
    for column in ('artists','genres','blacklist_genres'):
        conn.execute(f'ALTER TABLE road_trip_recipes DROP COLUMN {column}')
