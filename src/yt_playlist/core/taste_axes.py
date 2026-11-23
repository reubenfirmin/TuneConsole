"""Relational taste axes; encoded tokens exist only at the legacy API boundary."""
from yt_playlist.core import genre_identity, worker_settings
from yt_playlist.util import genre_catalog

GRADUATIONS = 'taste_axis_graduations'
GRADUATION_HISTORY = 'graduation_history'
GRADUATION_SERIAL = 'taste_axis_graduation_high_water'

def identity(conn, token, *, create=False):
    if not isinstance(token, str) or not token:
        raise ValueError('A taste axis requires a nonempty token')
    kind, genre, family, value, reason = 'other', None, None, token, None
    if token.startswith(('genre:', 'family:')):
        label = token.split(':', 1)[1]
        # Legacy genre:<family> bars mean a broad family, even when a specific
        # genre has the same name. Preserve that established interpretation.
        if conn.execute('SELECT 1 FROM genre_family WHERE key=?',(label,)).fetchone():
            kind, family, value = 'family', label, None
        else:
            genre, reason = genre_identity.resolve(conn, label)
            if genre is None and reason == 'unknown_genre' and create:
                records, labels = genre_catalog.catalog()
                verified = labels.get(genre_catalog.normalize(label))
                if verified:
                    record = records[verified[0]]
                    family_key = record['family']
                    if family_key:
                        conn.execute('INSERT INTO genre_family(key,name) VALUES (?,?) ON CONFLICT DO NOTHING',
                                     (family_key, family_key.title()))
                    conn.execute('INSERT INTO genre_catalog(key,name,family_key,source) VALUES (?,?,?,?) '
                                 'ON CONFLICT DO NOTHING',
                                 (verified[0], record['name'], family_key, 'catalog'))
                    genre, reason = genre_identity.resolve(conn, verified[0])
            if genre is not None:
                kind, value = 'genre', None
            else:
                kind, value = 'unresolved_genre', label
    fields = (kind, genre, family, value)
    row = conn.execute('SELECT id FROM taste_axes WHERE kind=? AND genre_key IS ? '
                       'AND family_key IS ? AND value IS ?', fields).fetchone()
    if row:
        return row[0]
    if not create:
        return None
    return conn.execute('INSERT INTO taste_axes(kind,genre_key,family_key,value,resolution_reason) '
                        'VALUES (?,?,?,?,?)', (*fields, reason)).lastrowid


def token_expression():
    # IDs stay stable when display names or aliases change. Existing callers
    # consume stable genre keys and family keys through their token boundary.
    return ("CASE a.kind WHEN 'genre' THEN 'genre:'||a.genre_key "
            "WHEN 'family' THEN 'genre:'||a.family_key "
            "WHEN 'unresolved_genre' THEN 'genre:'||a.value ELSE a.value END")


def graduation_event_sql(identity, source, score, factor, weight, at):
    numbers = ','.join(f"json(printf('%!0.26g',{column}))" for column in (score, factor, weight, at))
    return f'json_array({identity},{source},{numbers})'


def graduation_view():
    return f'''CREATE VIEW {GRADUATIONS} AS
        SELECT event.value->>0 AS id, a.id AS axis_id, {token_expression()} AS axis,
            event.value->>1 AS source, event.value->>2 AS score, event.value->>3 AS factor,
            event.value->>4 AS new_weight, event.value->>5 AS created_at
        FROM taste_axes a CROSS JOIN json_each(a.{GRADUATION_HISTORY}) event
        WHERE a.{GRADUATION_HISTORY}<>'[]' '''


def record_graduation(conn, axis, source, score, factor, weight, at):
    reference = identity(conn, axis, create=True)
    serial = int(worker_settings.read(conn, GRADUATION_SERIAL, '0')) + 1
    event = graduation_event_sql('?', '?', '?', '?', '?', '?')
    conn.execute(f"UPDATE taste_axes SET {GRADUATION_HISTORY}=json_insert({GRADUATION_HISTORY},'$[#]',{event}) "
                 'WHERE id=?', (serial, source, score, factor, weight, at, reference))
    worker_settings.save(conn, GRADUATION_SERIAL, str(serial))
