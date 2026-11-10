"""Prepare finite album controls and model-selection pointers independently.

Installed settings remain authoritative. Exact originals and unknown references
are retained; these jobs neither activate destination owners nor advance v94.
"""
import hashlib
import json
import math

from .. import genre_identity, model_selection_heads, recommendation_outputs
from .rows import Job, main as run


MAX_BYTES = 64 * 1024
ALBUM_KEY = 'album_menu_tuning'
ALBUM = 'prepared_album_controls'
GENRES = 'prepared_album_control_genres'
RUNS = 'prepared_model_selection_pointers'


def originals(name):
    return 'prepared_' + name + '_originals'


def retain(conn, name, row):
    raw = bytes(row['value']).decode('utf-8') if row['value'] is not None else None
    if raw is not None and len(raw.encode()) > MAX_BYTES:
        raise ValueError('Collection control exceeds its preparation byte budget')
    digest = hashlib.sha256(json.dumps(raw, ensure_ascii=False).encode()).hexdigest()
    conn.execute(f'INSERT INTO {originals(name)} VALUES (?,?,?) '
                 'ON CONFLICT(source,digest) DO NOTHING', (row['key'], digest, raw))
    return raw, digest


def finite(value):
    return (type(value) in (int, float) and -1.7976931348623157e308 <= value <= 1.7976931348623157e308
            and math.isfinite(value))


def album_values(conn, raw):
    """Return the finite domain fields used by preparation and publication."""
    kind = 'null' if raw is None else 'blank' if raw == '' else 'controls'
    until, novelty, style, labels = None, None, None, []
    if kind == 'controls':
        value = recommendation_outputs.decode_json(raw)
        if not isinstance(value, dict) or set(value) != {'until', 'values', 'genres'}:
            raise ValueError('Unsupported temporary album controls; original retained')
        controls, labels, until = value['values'], value['genres'], value['until']
        if not isinstance(controls, dict):
            raise ValueError('Album controls require recorded selections')
        style = ('novelty' if set(controls) == {'novelty'} else 'legacy' if
                 set(controls) == {'discovery', 'stretch', 'variety', 'recency'} else None)
        novelty = controls.get('discovery' if style == 'legacy' else 'novelty')
        if (style is None or not finite(until) or not finite(novelty) or not 0 <= novelty <= 1
                or not isinstance(labels, list) or len(labels) > 100
                or any(not isinstance(label, str) or not 0 < len(label) <= 100 for label in labels)):
            raise ValueError('Unsupported temporary album control selections; original retained')
        until, novelty = float(until), float(novelty)
    genres = []
    for position, label in enumerate(labels):
        genre, reason = genre_identity.resolve(conn, label)
        genres.append((position, label, genre, reason))
    return (kind, until, novelty, style), genres


def album(conn, owner, key, row):
    raw, digest = retain(conn, 'album_controls', row)
    fields, genres = album_values(conn, raw)
    conn.execute(f'DELETE FROM {GENRES} WHERE source=?', (row['key'],))
    conn.execute(f'INSERT INTO {ALBUM} VALUES (?,?,?,?,?,?) ON CONFLICT(source) DO UPDATE SET '
        'source_kind=excluded.source_kind,expires_at=excluded.expires_at,novelty=excluded.novelty,'
        'control_style=excluded.control_style,source_digest=excluded.source_digest',
        (row['key'], *fields, digest))
    for genre in genres:
        conn.execute(f'INSERT INTO {GENRES} VALUES (?,?,?,?,?)', (row['key'], *genre))


def pointer_values(conn, raw):
    kind = 'null' if raw is None else 'blank' if raw == '' else 'recorded'
    run, known = None, None
    if kind == 'recorded':
        if not raw.isdecimal() or not 0 < int(raw) < 2**63:
            raise ValueError('Model selection requires an exact run ID; original retained')
        run = int(raw)
        known = conn.execute('SELECT id FROM rec_model_selection_runs WHERE id=?', (run,)).fetchone()
    return kind, run, known[0] if known else None, 'missing_run' if run is not None and not known else None


def pointer(conn, owner, key, row):
    raw, digest = retain(conn, 'model_pointers', row)
    conn.execute(f'INSERT INTO {RUNS} VALUES (?,?,?,?,?,?) ON CONFLICT(source) DO UPDATE SET '
        'source_kind=excluded.source_kind,recorded_run_id=excluded.recorded_run_id,run_id=excluded.run_id,'
        'unresolved_reason=excluded.unresolved_reason,source_digest=excluded.source_digest',
        (row['key'], *pointer_values(conn, raw), digest))


def definitions(name, sources, table, columns):
    archive = originals(name)
    allowed = ','.join(repr(key) for key in sources)
    return (
        (archive, f'''CREATE TABLE {archive} (
            source TEXT NOT NULL CHECK(source IN ({allowed})),
            digest TEXT NOT NULL CHECK(length(digest)=64),
            raw_value TEXT CHECK(raw_value IS NULL OR length(CAST(raw_value AS BLOB))<={MAX_BYTES}),
            PRIMARY KEY(source,digest)) WITHOUT ROWID'''),
        (table, f'''CREATE TABLE {table} (
            source TEXT PRIMARY KEY CHECK(source IN ({allowed})), {columns},
            FOREIGN KEY(source,source_digest) REFERENCES {archive}(source,digest)) WITHOUT ROWID'''),
    )


def job(name, sources, destinations, copy, delete, dependencies):
    return Job(name, 79, 93, {'settings': ('key',)}, destinations, copy, delete,
        dependencies=dependencies, filters={'settings': '{prefix}key IN (' + ','.join(repr(key) for key in sources) + ')'},
        projections={'settings': ('key', f'CASE WHEN value IS NULL THEN NULL ELSE '
            f"COALESCE(substr(CAST(value AS BLOB),1,{MAX_BYTES + 1}),X'') END AS value")}, batch_limit=4)


def delete_album(conn, owner, key):
    source, = json.loads(key)
    if source == ALBUM_KEY:
        conn.execute(f'DELETE FROM {GENRES} WHERE source=?', (source,))
        conn.execute(f'DELETE FROM {ALBUM} WHERE source=?', (source,))


def delete_pointer(conn, owner, key):
    source, = json.loads(key)
    if source in model_selection_heads.KEYS:
        conn.execute(f'DELETE FROM {RUNS} WHERE source=?', (source,))


JOBS = {
    'album-controls': job('album_controls', (ALBUM_KEY,), definitions('album_controls', (ALBUM_KEY,), ALBUM, '''
        source_kind TEXT NOT NULL CHECK(source_kind IN ('null','blank','controls')),
        expires_at REAL, novelty REAL, control_style TEXT, source_digest TEXT NOT NULL,
        CHECK((source_kind='controls' AND expires_at IS NOT NULL AND abs(expires_at)<=1.7976931348623157e308
                 AND novelty IS NOT NULL AND novelty BETWEEN 0 AND 1
                 AND control_style IS NOT NULL AND control_style IN ('novelty','legacy'))
           OR (source_kind<>'controls' AND expires_at IS NULL AND novelty IS NULL AND control_style IS NULL))''') + (
        (GENRES, f'''CREATE TABLE {GENRES} (
            source TEXT NOT NULL, position INTEGER NOT NULL CHECK(position BETWEEN 0 AND 99),
            raw_label TEXT NOT NULL CHECK(length(raw_label) BETWEEN 1 AND 100), genre_key TEXT, unresolved_reason TEXT,
            PRIMARY KEY(source,position), FOREIGN KEY(source) REFERENCES {ALBUM}(source),
            CHECK((genre_key IS NOT NULL AND unresolved_reason IS NULL) OR
                (genre_key IS NULL AND unresolved_reason IS NOT NULL AND unresolved_reason IN ('unknown_genre','ambiguous_genre')))
        ) WITHOUT ROWID'''),
    ), album, delete_album, ('genre_catalog', 'genre_alias')),
    'model-pointers': job('model_pointers', tuple(model_selection_heads.KEYS),
        definitions('model_pointers', tuple(model_selection_heads.KEYS), RUNS, '''
        source_kind TEXT NOT NULL CHECK(source_kind IN ('null','blank','recorded')),
        recorded_run_id INTEGER, run_id INTEGER, unresolved_reason TEXT, source_digest TEXT NOT NULL,
        CHECK((source_kind='recorded' AND recorded_run_id IS NOT NULL AND recorded_run_id>0 AND
                  ((run_id IS NOT NULL AND run_id=recorded_run_id AND unresolved_reason IS NULL) OR
                   (run_id IS NULL AND unresolved_reason IS NOT NULL AND unresolved_reason='missing_run')))
           OR (source_kind<>'recorded' AND recorded_run_id IS NULL AND run_id IS NULL AND unresolved_reason IS NULL))'''),
        pointer, delete_pointer, ('rec_model_selection_runs',)),
}


def main(argv=None):
    import argparse
    parser = argparse.ArgumentParser(description=__doc__, add_help=False)
    parser.add_argument('job', choices=tuple(JOBS))
    args, remaining = parser.parse_known_args(argv)
    return run(JOBS[args.job], remaining)


if __name__ == '__main__':
    raise SystemExit(main())
