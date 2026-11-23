"""Keep playback registrations with the judgment revisions for their run."""
from ..schema_contract import MigrationError, tokens
from ..schema_inspection import definitions
from . import annotation_storage
from .account_storage import _preserve_sequence, _sequence


NAME = 'own_audio_playback_registrations'
TABLE = 'audio_annotations'
RETIRED = 'audio_vibe_runs'
INDEX = 'ix_audio_annotation_playback'
TABLE_SQL = '''CREATE TABLE audio_annotations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_key TEXT NOT NULL, kind TEXT NOT NULL CHECK(kind IN ('feedback','development','session','playback')),
    taxonomy_version TEXT, revision INTEGER CHECK(revision>0),
    state TEXT CHECK(state IN ('labeled','empty','withdrawn')), created_at REAL NOT NULL,
    supersedes_id INTEGER UNIQUE REFERENCES audio_annotations(id),
    sample_1_id TEXT REFERENCES sample_attributes(id),
    sample_2_id TEXT REFERENCES sample_attributes(id),
    sample_3_id TEXT REFERENCES sample_attributes(id),
    labels_json TEXT CHECK(json_valid(labels_json) AND json_type(labels_json)='array' OR labels_json IS NULL),
    archive_json TEXT CHECK(json_valid(archive_json) AND json_type(archive_json)='object' OR archive_json IS NULL),
    unresolved_sample_count INTEGER NOT NULL DEFAULT 0 CHECK(unresolved_sample_count>=0),
    video_id TEXT, title TEXT, artist TEXT,
    UNIQUE(run_key,kind,taxonomy_version,revision),
    CHECK((state='labeled')=(json_array_length(labels_json)>0)),
    CHECK(kind<>'feedback' OR json_array_length(labels_json)<=1),
    CHECK((kind='playback' AND video_id IS NOT NULL AND title IS NOT NULL AND artist IS NOT NULL
        AND taxonomy_version IS NULL AND revision IS NULL AND state IS NULL AND supersedes_id IS NULL
        AND sample_1_id IS NULL AND sample_2_id IS NULL AND sample_3_id IS NULL
        AND labels_json IS NULL AND archive_json IS NULL AND unresolved_sample_count=0)
      OR (kind<>'playback' AND video_id IS NULL AND title IS NULL AND artist IS NULL
        AND taxonomy_version IS NOT NULL AND revision IS NOT NULL AND state IS NOT NULL
        AND labels_json IS NOT NULL AND archive_json IS NOT NULL)))'''
INDEX_SQL = f"CREATE UNIQUE INDEX {INDEX} ON audio_annotations(run_key) WHERE kind='playback'"
VIDEO_INDEX = '''CREATE INDEX ix_audio_annotation_playback_video ON audio_annotations(video_id,run_key)
    WHERE kind='playback' '''
JUDGMENTS = ('id,run_key,kind,taxonomy_version,revision,state,created_at,supersedes_id,'
             'sample_1_id,sample_2_id,sample_3_id,labels_json,archive_json,unresolved_sample_count')


def install(conn, _context):
    sequence = _sequence(conn, TABLE)
    conn.execute('ALTER TABLE audio_annotations RENAME TO previous_audio_annotations')
    conn.execute(TABLE_SQL)
    conn.execute(f'INSERT INTO audio_annotations({JUDGMENTS}) SELECT {JUDGMENTS} FROM previous_audio_annotations')
    conn.execute('DROP TABLE previous_audio_annotations')
    _preserve_sequence(conn, TABLE, sequence)
    conn.execute("INSERT INTO audio_annotations(run_key,kind,video_id,title,artist,created_at) "
                 "SELECT run_id,'playback',video_id,title,artist,created_at FROM audio_vibe_runs ORDER BY run_id")
    conn.execute('DROP TABLE audio_vibe_runs')
    conn.execute(INDEX_SQL)
    conn.execute(VIDEO_INDEX.strip())
    for name, sql in annotation_storage.SQL.items():
        if name != TABLE:
            conn.execute(sql)


def validate(conn):
    schema = definitions(conn, (TABLE, INDEX, RETIRED))
    if (RETIRED in schema or tokens(schema.get(TABLE)) != tokens(TABLE_SQL)
            or tokens(schema.get(INDEX)) != tokens(INDEX_SQL)):
        raise MigrationError('Incompatible audio playback registration storage')
