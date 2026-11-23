"""Exact uploads and unresolved imports share one stable source ID space."""
import re

from . import metadata_values
from .schema_contract import MigrationError


FIELDS = metadata_values.ITEM_FIELDS
RAW_COLUMNS = {field: metadata_values.column_type(field) for field in FIELDS}
SOURCE_COLUMNS = tuple('effective_' + field for field in FIELDS if field not in ('title', 'artist'))
PLAYBACK_PREFERENCE = 'playback_preferred INTEGER NOT NULL DEFAULT 0 CHECK(playback_preferred IN (0,1))'
TABLE = '''CREATE TABLE track_sources (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    recording_id INTEGER NOT NULL REFERENCES recordings(id),
    video_id TEXT UNIQUE CHECK(video_id IS NULL OR length(trim(video_id))>0),
    source_ref TEXT UNIQUE,
    library_origin TEXT,
    library_added_at REAL,
    ''' + ',\n    '.join(f'{field} {kind}' for field, kind in RAW_COLUMNS.items()) + ''',
    ''' + ',\n    '.join(f'{field} {RAW_COLUMNS[field.removeprefix("effective_")]}' for field in SOURCE_COLUMNS) + ''',
    UNIQUE(video_id,recording_id),
    CHECK((video_id IS NOT NULL)<>(source_ref IS NOT NULL)),
    CHECK(library_origin IS NOT NULL OR library_added_at IS NULL),
    CHECK(available IS NULL OR (typeof(available)='integer' AND available IN (0,1)))
)'''


def folded(conn):
    return conn.execute("SELECT 1 FROM main.sqlite_schema WHERE type='table' AND name='track_sources'").fetchone() is not None


def pending(conn):
    return conn.execute("SELECT 1 FROM main.sqlite_schema WHERE type='view' AND name='track_sources'").fetchone() is not None


def install(conn, _context):
    conn.execute('CREATE VIEW track_sources AS SELECT * FROM catalog_entries')


def installed_projection(*, namespace='', columns=('e.*',)):
    fields = ','.join(f'CASE WHEN e.video_id IS NOT NULL THEN y.{field} ELSE u.{field} END AS {field}'
                      for field in FIELDS)
    return (f'SELECT {",".join(columns)},{fields},m.origin AS library_origin,m.added_at AS library_added_at '
            f'FROM {namespace}catalog_entries e '
            f'LEFT JOIN {namespace}youtube_items y ON y.video_id=e.video_id '
            f'LEFT JOIN {namespace}unresolved_items u ON u.entry_id=e.id '
            f'LEFT JOIN {namespace}library_memberships m ON m.entry_id=e.id')


def save_to_library(conn, entries, added_at, *, origin='observed'):
    conn.executemany('UPDATE track_sources SET library_origin=?,library_added_at=? '
                     'WHERE id=? AND library_origin IS NULL',
                     ((origin, added_at, entry) for entry in entries))


def remove_from_library(conn, video_id):
    conn.execute('UPDATE track_sources SET library_origin=NULL,library_added_at=NULL '
                 'WHERE video_id=? AND library_origin IS NOT NULL', (video_id,))


def validate(conn):
    if folded(conn):
        if conn.execute("SELECT 1 FROM main.sqlite_schema WHERE name IN ("
                        "'catalog_entries','catalog_metadata_state','youtube_items',"
                        "'library_memberships','tracks','playable_library_tracks','catalog_enrichment_tracks') "
                        "OR (type='table' AND name='unresolved_items')").fetchone():
            raise MigrationError('Retired track source storage remains')
    elif not pending(conn):
        raise MigrationError('Missing track source storage')


def create(conn, recording, video, ref, title, artist, *, retain_original=True):
    original = (title, artist) if retain_original else (None, None)
    return conn.execute('INSERT INTO track_sources(recording_id,video_id,source_ref,title,artist,orig_title,orig_artist) '
                        'VALUES (?,?,?,?,?,?,?)', (recording, video, ref, title, artist, *original)).lastrowid


def reassign(conn, video, recording):
    conn.execute('UPDATE track_sources SET recording_id=?,playback_preferred=0 WHERE video_id=? AND recording_id<>?',
                 (recording, video, recording))


def prefer_playback(conn, recording, video):
    conn.execute(
        'UPDATE track_sources SET playback_preferred=(video_id=?) '
        'WHERE recording_id=? AND video_id IS NOT NULL AND playback_preferred IS NOT (video_id=?)',
        (video, recording, video))


def write_field(conn, entry, field, value):
    conn.execute(f'UPDATE track_sources SET {field}=? WHERE id=? AND {field} IS NOT ?', (value, entry, value))


def projections():
    fields = ','.join(FIELDS)
    yield 'CREATE VIEW unresolved_items AS SELECT id AS entry_id,' + fields + ' FROM track_sources WHERE video_id IS NULL'
    values = ','.join(f'{field}=NEW.{field}' for field in FIELDS)
    yield ('CREATE TRIGGER write_unresolved_items INSTEAD OF UPDATE ON unresolved_items BEGIN '
           f'UPDATE track_sources SET {values} WHERE id=OLD.entry_id; END')


def folded_sql(sql):
    return re.sub(r'\b(?:catalog_entries|youtube_items)\b', 'track_sources', sql)


def folded_foreign_keys(sql):
    return re.sub(r'\bREFERENCES\s+"?(catalog_entries|youtube_items)"?\s*\(',
                  'REFERENCES track_sources(', sql, flags=re.I)

