"""Owned import reports and saved playlist documents, retaining legacy evidence."""
import json
from pathlib import Path
import re
import sqlite3

from yt_playlist.core import playlist_recipe_state, spotify_job_state
from .runner import MigrationError


def transform(sql):
    return '' if re.match(r'CREATE TABLE\s+"?(rec_recipes|cluster_canvas)"?\s*\(',sql.lstrip(),re.I) else sql


def fences():
    yield ("CREATE TRIGGER spotify_import_job_account_update BEFORE UPDATE ON spotify_import_jobs "
           "WHEN NEW.account_id IS NOT OLD.account_id OR NEW.unresolved_account_id IS NOT OLD.unresolved_account_id "
           "OR NEW.account_present IS NOT OLD.account_present "
           "BEGIN SELECT RAISE(ABORT,'Spotify import job account cannot change'); END")
    for event in ('INSERT','UPDATE'):
        yield (f'CREATE TRIGGER spotify_import_settings_{event.lower()} BEFORE {event} ON settings '
               "WHEN NEW.key='spotify_library_import_job' BEGIN "
               "SELECT RAISE(ABORT,'Spotify import state requires its job owner'); END")
    for event in ('INSERT','UPDATE'):
        yield (f'CREATE TRIGGER playlist_recipe_facet_{event.lower()} BEFORE {event} ON playlist_recipe_facets '
               "WHEN NOT EXISTS (SELECT 1 FROM taste_axes a WHERE a.id=NEW.axis_id AND "
               "((NEW.kind='genre' AND a.kind IN ('genre','family','unresolved_genre')) OR "
               "(NEW.kind='era' AND a.kind='other' AND a.value='era:'||NEW.captured_value))) "
               "BEGIN SELECT RAISE(ABORT,'Playlist recipe facet axis disagrees with its kind'); END")


def statements():
    pending = ''
    for line in Path(__file__).with_name('schema_collection_documents.sql').read_text().splitlines(True):
        pending += line
        if sqlite3.complete_statement(pending):
            yield pending
            pending = ''
    if pending.strip():
        raise MigrationError('Incomplete collection document schema')
    yield from fences()


def apply(conn,context):
    try:
        jobs = conn.execute("SELECT value FROM settings WHERE key='spotify_library_import_job'").fetchone()
        job = json.loads(jobs[0]) if jobs and jobs[0] else None
        recipes = [(scope,json.loads(raw),at) for scope,raw,at in conn.execute(
            'SELECT playlist_ytm,recipe,created_at FROM rec_recipes')]
        canvases = list(conn.execute('SELECT playlist_ytm,state,created_at FROM cluster_canvas'))
        conn.execute('DROP TABLE rec_recipes')
        conn.execute('DROP TABLE cluster_canvas')
        for sql in statements():
            conn.execute(sql)
        if job is not None:
            spotify_job_state.save(conn,job,legacy=True)
        for scope,value,at in recipes:
            playlist_recipe_state.save(conn,scope,value,at)
        for scope,state,at in canvases:
            playlist_recipe_state.save_canvas(conn,scope,state,at)
        conn.execute("DELETE FROM settings WHERE key='spotify_library_import_job'")
    except (ValueError,TypeError,KeyError,sqlite3.IntegrityError) as exc:
        raise MigrationError(f'Collection document conversion failed; original retained: {exc}') from exc
