"""Attach each prepared display's exact manifest to a ready file receipt."""
import json

from .. import publication_files as files
from ..recommendation_custody import HOME_RETIREMENT_GUARD
from ..schema_contract import MigrationError
from ..schema_inspection import definitions
from . import group_context_owners
from .prepared_display_settings import GUARDS
from .recommendation_custody import HOME_GUARD

NAME = 'keep_prepared_displays_on_their_files'


def predecessor_objects(conn):
    return list(definitions(conn, (files.RECEIPTS, *GUARDS)).values())


def install(conn, _context):
    conn.execute(f'ALTER TABLE {files.RECEIPTS} ADD COLUMN display_key TEXT')
    conn.execute(f'''ALTER TABLE {files.RECEIPTS} ADD COLUMN display_manifest TEXT CHECK(
        (display_key IS NULL AND display_manifest IS NULL) OR
        (display_key IS NOT NULL AND display_manifest IS NOT NULL AND file_status='ready'
         AND kind IN ('recommendation-output','prepared-display') AND json_type(display_manifest)='object'))''')
    conn.execute(f'CREATE UNIQUE INDEX {files.INDEX} ON {files.RECEIPTS}(display_key) WHERE display_key IS NOT NULL')
    conn.execute(f'''CREATE VIEW {files.MANIFESTS}(key,value) AS
        SELECT display_key,display_manifest FROM {files.RECEIPTS} WHERE display_key IS NOT NULL''')

    for prefix in files.PREFIXES.values():
        rows = conn.execute('SELECT key,value FROM settings WHERE key GLOB ? ORDER BY key', (prefix + '*',)).fetchall()
        for key, raw in rows:
            files.save(conn, key, json.loads(raw))
        conn.execute('DELETE FROM settings WHERE key GLOB ?', (prefix + '*',))

    for name, sql in definitions(conn, GUARDS).items():
        updated = sql.replace('FROM settings p,', f'FROM {files.MANIFESTS} p,')
        if sql == updated:
            raise MigrationError(f'Unknown prepared display retention guard {name}')
        conn.execute(f'DROP TRIGGER {name}')
        conn.execute(updated)


def validate(conn):
    guards = set(GUARDS)
    if definitions(conn, (HOME_RETIREMENT_GUARD,)):
        guards.remove(HOME_GUARD)
        guards.add(HOME_RETIREMENT_GUARD)
    if definitions(conn, (group_context_owners.GENERATION_GUARD,)):
        guards.remove(group_context_owners.HOME_GUARD)
        guards.update((group_context_owners.GENERATION_GUARD, group_context_owners.VERSION_GUARD))
    names = {files.INDEX, files.MANIFESTS, *guards}
    actual = definitions(conn, names)
    if set(actual) != names or any(files.MANIFESTS not in actual[name] for name in guards):
        raise MigrationError('Incomplete prepared display file cutover')
