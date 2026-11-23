"""Move the temporary C167 settings entries to the Wikipedia cache file."""
import json

from .. import wiki_cache
from ..schema_contract import MigrationError
from .metadata_conflict_storage import WIKI_CACHE_PREFIX


NAME = 'own_wikipedia_cards_in_cache_file'
KEYS = WIKI_CACHE_PREFIX + '*'


def install(conn, _context):
    cards = {key.removeprefix(WIKI_CACHE_PREFIX): json.loads(value) for key, value in conn.execute(
        'SELECT key,value FROM settings WHERE key GLOB ?', (KEYS,))}
    if not cards:
        return
    database = conn.execute('PRAGMA database_list').fetchone()[2]
    if database:
        path = wiki_cache.path_for(database)
        wiki_cache.write(path, {**wiki_cache.read(path), **cards}, durable=True)
    else:
        conn._wiki_cards = cards
    conn.execute('DELETE FROM settings WHERE key GLOB ?', (KEYS,))


def validate(conn):
    if conn.execute('SELECT 1 FROM settings WHERE key GLOB ? LIMIT 1', (KEYS,)).fetchone():
        raise MigrationError('Retired Wikipedia cache settings remain')
