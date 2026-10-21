"""Adopt supported unversioned layouts. Freeze historical column guards here."""
import re
import sqlite3
from contextlib import closing

from .legacy_schema import SCHEMA
from .runner import MigrationError

# Only these historically optional columns can be added to an existing table.
ADDITIONS = {
    'identities': {'active'},
    'tracks': {'available', 'video_type', 'artist_browse_id', 'album_browse_id', 'thumbnail',
               'genre', 'mb_year', 'bpm', 'energy', 'danceability', 'mb_recording_id',
               'orig_title', 'orig_artist', 'music_key', 'music_scale', 'mood_happy', 'mood_sad',
               'mood_relaxed', 'mood_acoustic', 'instrumental', 'loudness', 'dynamic_complexity',
               'popularity', 'gain', 'label', 'created_at', 'first_enriched_at', 'last_enriched_at'},
    'playlists': {'thumbnail', 'created_at', 'created_at_source'},
    'play_events': {'source', 'is_generated'},
    'rec_weights': {'updated_at'},
    'rec_mode_impressions': {'ranker'}, 'rec_mode_picks': {'ranker'},
    'taste_modes': {'space'},
    'discovered_albums': {'genre', 'first_shown', 'offered_count', 'legacy_offered_count', 'legacy_first_shown', 'legacy_last_shown'},
    'discovered_artists': {'genre', 'first_shown', 'offered_count', 'legacy_offered_count', 'legacy_first_shown', 'legacy_last_shown'},
    'discovered_tracks': {'last_enriched', 'first_shown', 'last_shown', 'offered_count'},
    'road_trip_recipes': {'familiarity_pct'},
}
EXTRA = {
    'tracks': [('created_at', 'REAL'), ('first_enriched_at', 'REAL'), ('last_enriched_at', 'REAL')],
    'road_trip_recipes': [('familiarity_pct', 'INTEGER NOT NULL DEFAULT 50')],
}
for _table in ('discovered_albums', 'discovered_artists', 'discovered_tracks'):
    EXTRA[_table] = [('first_shown', 'REAL'), ('last_shown', 'REAL'), ('offered_count', 'INTEGER NOT NULL DEFAULT 0')]
    if _table != 'discovered_tracks':
        EXTRA[_table] += [('legacy_offered_count', 'INTEGER NOT NULL DEFAULT 0'), ('legacy_first_shown', 'REAL'), ('legacy_last_shown', 'REAL')]


def statements(sql):
    """Use SQLite's statement parser (comments and quoted semicolons are legal)."""
    pending = ''
    for line in sql.splitlines(keepends=True):
        pending += line
        if sqlite3.complete_statement(pending):
            yield pending
            pending = ''
    if pending.strip():
        raise MigrationError('Incomplete baseline SQL')


def expected():
    conn = sqlite3.connect(':memory:')
    for sql in statements(SCHEMA):
        conn.execute(sql)
    for table, fields in EXTRA.items():
        columns = {r[1] for r in conn.execute(f'PRAGMA table_info({table})')}
        for name, declaration in fields:
            if name not in columns:
                conn.execute(f'ALTER TABLE {table} ADD COLUMN {name} {declaration}')
    return conn


def setting(conn, key):
    row = conn.execute('SELECT value FROM settings WHERE key=?', (key,)).fetchone()
    return row[0] if row else None


def stamp(conn, key, value='1'):
    conn.execute('INSERT INTO settings(key,value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value', (key, value))


def _objects(conn):
    return {r[0]: (r[1], r[2]) for r in conn.execute("SELECT name,type,sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'")}


def validate(conn, *, legacy, replaced=()):
    with closing(expected()) as reference:
        objects, wanted = _objects(conn), _objects(reference)
        known = set(objects) & set(wanted)
        app_tables = {name for name, (kind, _) in objects.items() if kind in ('table', 'view') and name != 'schema_migrations'}
        if legacy and app_tables and not known:
            raise MigrationError('Unrecognized database; no supported application tables')
        for table, (kind, sql) in wanted.items():
            if table in replaced:
                continue
            if kind != 'table':
                continue
            if table not in objects:
                if legacy:
                    continue
                raise MigrationError(f'Missing required table: {table}')
            if objects[table][0] != 'table':
                raise MigrationError(f'Expected a table: {table}')
            actual = {r[1]: r for r in conn.execute(f'PRAGMA table_info({table})')}
            required = {r[1]: r for r in reference.execute(f'PRAGMA table_info({table})')}
            for name, info in required.items():
                if name not in actual:
                    if legacy and name in ADDITIONS.get(table, ()):
                        continue
                    raise MigrationError(f'Missing required column: {table}.{name}')
                if actual[name][2].upper() != info[2].upper() or actual[name][5] != info[5]:
                    raise MigrationError(f'Incompatible column/key: {table}.{name}')
            archive = {'legacy_offered_count', 'legacy_first_shown', 'legacy_last_shown'}
            if table in ('discovered_albums', 'discovered_artists') and archive & actual.keys() and not archive <= actual.keys():
                raise MigrationError(f'Incomplete discovery exposure archive: {table}')
        for name, (kind, sql) in wanted.items():
            if name in replaced:
                continue
            if kind != 'index':
                continue
            if name not in objects:
                if legacy:
                    continue
                raise MigrationError(f'Missing required index: {name}')
            def normalized(value):
                return re.sub(r'\s+', '', re.sub(r'IF\s+NOT\s+EXISTS', '', value or '', flags=re.I)).lower()
            if objects[name][0] != 'index' or normalized(objects[name][1]) != normalized(sql):
                raise MigrationError(f'Conflicting index definition: {name}')


def apply(conn, context):
    original = {r[0]: {c[1] for c in conn.execute(f'PRAGMA table_info("{r[0]}")')}
                for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    indexes = []
    for sql in statements(SCHEMA):
        if re.search(r'CREATE\s+(UNIQUE\s+)?INDEX', sql, re.I):
            indexes.append(sql)
        else:
            conn.execute(sql)
    reference = expected()
    try:
        for table in ADDITIONS:
            actual = {r[1] for r in conn.execute(f'PRAGMA table_info({table})')}
            for _, name, kind, notnull, default, pk in reference.execute(f'PRAGMA table_info({table})'):
                if name not in actual:
                    decl = kind + (' NOT NULL' if notnull else '') + (f' DEFAULT {default}' if default is not None else '')
                    conn.execute(f'ALTER TABLE {table} ADD COLUMN {name} {decl}')
    finally:
        reference.close()
    old_tracks = original.get('tracks', set())
    for field in ('title', 'artist'):
        if f'orig_{field}' not in old_tracks:
            conn.execute(f'UPDATE tracks SET orig_{field}={field} WHERE orig_{field} IS NULL')
    if 'first_enriched_at' not in old_tracks and not setting(conn, 'enrich_ts_backfilled'):
        conn.execute('UPDATE tracks SET first_enriched_at=(SELECT MIN(created_at) FROM enrichment_log e WHERE e.track_id=tracks.id), last_enriched_at=(SELECT MAX(created_at) FROM enrichment_log e WHERE e.track_id=tracks.id) WHERE id IN (SELECT track_id FROM enrichment_log)')
        stamp(conn, 'enrich_ts_backfilled')
    if 'source' not in original.get('play_events', set()):
        first = conn.execute('SELECT MIN(played_at) FROM play_events WHERE like_status IS NOT NULL OR playlist_ytm_id IS NOT NULL').fetchone()[0]
        conn.execute("UPDATE play_events SET source=CASE WHEN ? IS NOT NULL AND played_at>=? THEN 'live' ELSE 'takeout' END WHERE source=''", (first, first))
    for table in ('discovered_albums', 'discovered_artists'):
        if 'legacy_offered_count' not in original.get(table, set()):
            conn.execute(f'UPDATE {table} SET legacy_offered_count=offered_count,legacy_first_shown=first_shown,legacy_last_shown=last_shown,offered_count=0,first_shown=NULL,last_shown=NULL')
    for sql in indexes:
        conn.execute(sql)
