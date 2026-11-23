"""Keep Discovery checkpoints with their owners and retire import-only tables."""
import json

from .. import legacy_journals
from ..schema_contract import MigrationError, tokens
from ..schema_inspection import definitions
from .runner import PreparationChanged


NAME = 'fold_discovery_checkpoints'
RETIRED = (
    'discovery_artist_image_state', 'starter_song_choices',
    'discovery_identity_evidence', 'unresolved_discovery_impressions',
)
COLUMNS = (
    ('artists', 'discovery_image_retry_at',
     "REAL CHECK(discovery_image_retry_at IS NULL OR "
     "(typeof(discovery_image_retry_at) IN ('integer','real') AND abs(discovery_image_retry_at)<=1.7976931348623157e308))"),
    ('track_sources', 'starter_selected_at',
     "REAL CHECK(starter_selected_at IS NULL OR "
     "(typeof(starter_selected_at) IN ('integer','real') AND abs(starter_selected_at)<=1.7976931348623157e308))"),
)
MARKER = 'ix_track_sources_starter_selected'
INDEX = f'''CREATE INDEX {MARKER}
    ON track_sources(starter_selected_at,video_id) WHERE starter_selected_at IS NOT NULL'''


def predecessor_objects(conn):
    placeholders = ','.join('?' for table in RETIRED)
    return [sql for sql, in conn.execute(
        f'SELECT sql FROM sqlite_schema WHERE sql IS NOT NULL AND '
        f'((type=? AND name IN (?,?)) OR tbl_name IN ({placeholders})) ORDER BY type,name',
        ('table', 'artists', 'track_sources', *RETIRED))]


def _import_only_rows(conn):
    for view, kind, key, shown, reason in conn.execute(
            'SELECT * FROM unresolved_discovery_impressions ORDER BY view_id,original_kind,original_key'):
        identity = json.dumps([view, kind, key], separators=(',', ':'))
        yield ('unresolved_impression', identity, json.dumps(
            dict(view_id=view, original_kind=kind, original_key=key, shown_at=shown)), reason)
    for video, selected in conn.execute(
            'SELECT s.video_id,s.selected_at FROM starter_song_choices s '
            'WHERE NOT EXISTS(SELECT 1 FROM track_sources t WHERE t.video_id=s.video_id) ORDER BY s.video_id'):
        yield ('unmatched_starter_song', video, json.dumps(
            dict(video_id=video, selected_at=selected)), 'missing exact upload')


def _applied(conn, version):
    return conn.execute('SELECT 1 FROM schema_changes WHERE version=?', (version,)).fetchone() is not None


def prepare(conn, version):
    while True:
        conn.execute('BEGIN IMMEDIATE')
        with conn:
            if _applied(conn, version):
                return
            if not conn.execute('SELECT 1 FROM discovery_identity_evidence LIMIT 1').fetchone():
                break
            legacy_journals.prepare_batch(conn, 'discovery')
    with conn:
        conn.execute('BEGIN')
        if _applied(conn, version):
            return
        rows = list(_import_only_rows(conn))
    for offset in range(0, len(rows), 128):
        conn.execute('BEGIN IMMEDIATE')
        with conn:
            if _applied(conn, version):
                return
            legacy_journals.retain_many(conn, 'discovery', rows[offset:offset + 128])
    return rows


def install(conn, context):
    if (conn.execute('SELECT 1 FROM discovery_identity_evidence LIMIT 1').fetchone()
            or list(_import_only_rows(conn)) != context.prepared):
        raise PreparationChanged('Discovery imports changed before installation')
    for table, column, declaration in COLUMNS:
        conn.execute(f'ALTER TABLE {table} ADD COLUMN {column} {declaration}')
    conn.execute('UPDATE artists SET discovery_image_retry_at=s.retry_at '
                 'FROM discovery_artist_image_state s WHERE artists.id=s.artist_id')
    conn.execute('UPDATE track_sources SET starter_selected_at=s.selected_at '
                 'FROM starter_song_choices s WHERE track_sources.video_id=s.video_id')
    conn.execute(INDEX)
    for table in RETIRED:
        conn.execute('DROP TABLE ' + table)
    legacy_journals._refresh_file_references(conn)


def validate(conn):
    actual = definitions(conn, (*RETIRED, MARKER))
    if actual.keys() & set(RETIRED) or tokens(actual.get(MARKER)) != tokens(INDEX):
        raise MigrationError('Discovery checkpoint storage was not folded')
