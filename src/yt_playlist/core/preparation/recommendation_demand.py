"""Stage retained Home demand and listening-window identities in bounded batches."""
import json

from .. import album_note_requests, genre_identity, recommendation_requests
from .recommendation_state import original, selected
from .rows import Job, main as run


SURFACES = recommendation_requests.SURFACES
MAX_BYTES = 256 * 1024


def delete(conn, owner, key):
    surface, = json.loads(key)
    if surface not in SURFACES:
        return
    if surface.startswith('choice_window:'):
        conn.execute('DELETE FROM prepared_listening_choice_windows WHERE format=?', (surface.split(':', 1)[1],))
    elif surface == 'album_menu_refresh':
        conn.execute('DELETE FROM prepared_album_menu_exclusions')
        conn.execute('DELETE FROM prepared_album_menu_refresh')
    else:
        conn.execute('DELETE FROM prepared_home_genre_items')
        conn.execute('DELETE FROM prepared_home_genre_requests')
    conn.execute('DELETE FROM prepared_demand_originals WHERE surface=?', (surface,))


def describe(conn, row):
    """Derive ordered request rows without changing their installed owner."""
    row = dict(row)
    raw = row['payload'].encode() if isinstance(row['payload'], str) else bytes(row['payload'])
    if len(raw) > MAX_BYTES:
        raise ValueError('Recommendation demand exceeds its preparation byte budget')
    row['payload'] = raw.decode('utf-8')
    surface, at = row['surface'], row['built_at']
    value = recommendation_requests.parse(surface, json.loads(row['payload']), at)
    result = {'prepared_demand_originals': [(surface, row['payload'], at)]}
    if surface.startswith('choice_window:'):
        if value:
            result['prepared_listening_choice_windows'] = [
                (surface.split(':', 1)[1], value['opportunity_id'], value['window'], at)]
    elif surface == 'album_menu_refresh':
        result['prepared_album_menu_refresh'] = [(1, value.get('epoch'), at)]
        items = result['prepared_album_menu_exclusions'] = []
        for position, browse in enumerate(value.get('exclude', [])):
            reference, reason = album_note_requests._reference(conn, browse)
            items.append((1, position, browse, reference, reason))
    else:
        result['prepared_home_genre_requests'] = [
                     (1, value.get('epoch'), int('include_genres' in value), int('avoid_genres' in value),
                      int(value['held_by_vibe']) if 'held_by_vibe' in value else None,
                      int(value['suspended_by_vibe']) if 'suspended_by_vibe' in value else None, at)]
        items = result['prepared_home_genre_items'] = []
        for field, kind in (('include_genres', 'include'), ('avoid_genres', 'avoid')):
            for position, label in enumerate(value.get(field, [])):
                family = conn.execute('SELECT key FROM genre_family WHERE key=?', (label,)).fetchone()
                genre, reason = (None, None) if family else genre_identity.resolve(conn, label.removeprefix('other:'))
                items.append((1, kind, position, label, family[0] if family else None, genre, reason))
    return result


def copy(conn, owner, key, row):
    values = describe(conn, row)
    delete(conn, owner, key)
    for table, rows in values.items():
        if rows:
            conn.executemany('INSERT INTO '+table+' VALUES ('+','.join('?' for _ in rows[0])+')', rows)


JOB = Job('demand', 79, 86, {'rec_proposals': ('surface',)}, (
    original('demand', SURFACES),
    ('prepared_listening_choice_windows', '''CREATE TABLE prepared_listening_choice_windows (
        format TEXT PRIMARY KEY CHECK(format IN ('albums','mixes','radio')),
        opportunity_key TEXT NOT NULL, window_text TEXT NOT NULL, updated_at REAL) WITHOUT ROWID'''),
    ('prepared_album_menu_refresh', '''CREATE TABLE prepared_album_menu_refresh (
        id INTEGER PRIMARY KEY CHECK(id=1), epoch INTEGER, updated_at REAL)'''),
    ('prepared_album_menu_exclusions', '''CREATE TABLE prepared_album_menu_exclusions (
        request_id INTEGER NOT NULL CHECK(request_id=1) REFERENCES prepared_album_menu_refresh(id),
        position INTEGER PRIMARY KEY CHECK(position BETWEEN 0 AND 99),
        browse_id TEXT NOT NULL, album_provider_id TEXT, unresolved_reason TEXT,
        CHECK((album_provider_id IS NULL)=(unresolved_reason IS NOT NULL)))'''),
    ('prepared_home_genre_requests', '''CREATE TABLE prepared_home_genre_requests (
        id INTEGER PRIMARY KEY CHECK(id=1), epoch INTEGER,
        has_includes INTEGER NOT NULL CHECK(has_includes IN (0,1)),
        has_avoids INTEGER NOT NULL CHECK(has_avoids IN (0,1)),
        held_by_vibe INTEGER CHECK(held_by_vibe IN (0,1)),
        suspended_by_vibe INTEGER CHECK(suspended_by_vibe IN (0,1)), updated_at REAL)'''),
    ('prepared_home_genre_items', '''CREATE TABLE prepared_home_genre_items (
        request_id INTEGER NOT NULL CHECK(request_id=1) REFERENCES prepared_home_genre_requests(id),
        kind TEXT NOT NULL CHECK(kind IN ('include','avoid')), position INTEGER NOT NULL CHECK(position BETWEEN 0 AND 99),
        raw_key TEXT NOT NULL, family_key TEXT, genre_key TEXT,
        resolution_reason TEXT CHECK(resolution_reason IN ('unknown_genre','ambiguous_genre')),
        PRIMARY KEY(kind,position),
        CHECK((family_key IS NOT NULL AND genre_key IS NULL AND resolution_reason IS NULL) OR
              (family_key IS NULL AND genre_key IS NOT NULL AND resolution_reason IS NULL) OR
              (family_key IS NULL AND genre_key IS NULL AND resolution_reason IS NOT NULL))) WITHOUT ROWID'''),
), copy, delete, dependencies=('album_provider_ids', 'genre_catalog', 'genre_alias', 'genre_family'),
    filters=selected(SURFACES), projections={'rec_proposals': (
        'surface', f'substr(CAST(payload AS BLOB),1,{MAX_BYTES + 1}) AS payload', 'built_at')})


def main(argv=None):
    return run(JOB, argv)


if __name__ == '__main__':
    raise SystemExit(main())
