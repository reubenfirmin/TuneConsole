"""Store the Discovery candidate and its exact source on each recording."""
from .. import domain_contracts, group_preview_work
from ..discovery_candidates import TRACK_INDEX
from ..recording_library import _DISCOVERY_TRIGGERS
from ..recommendation_invalidation import REQUEST_REFRESH
from ..schema_contract import MigrationError, tokens
from ..schema_inspection import definitions
from . import preparation_checkpoint_storage, recommendation_refresh_storage


NAME = 'fold_discovery_recording_candidates'
MEMBERSHIPS = 'discovery_memberships'
COLUMNS = (
    ('discovery_entry_id', 'INTEGER REFERENCES track_sources(id)'),
    ('discovery_found_at', 'REAL'),
    ('discovery_first_shown', 'REAL'),
    ('discovery_last_shown', 'REAL'),
    ('discovery_offered_count', 'INTEGER NOT NULL DEFAULT 0'),
)
VIEW = '''CREATE VIEW discovered_tracks AS
    SELECT c.*,c.mb_year year,d.discovery_found_at found_at,d.discovery_first_shown first_shown,
           d.discovery_last_shown last_shown,d.discovery_offered_count offered_count,
           c.last_enriched_at last_enriched,
           (SELECT o.source_token FROM discovery_sources s JOIN discovery_origin_tokens o ON o.id=s.origin_id
            WHERE s.recording_id=d.id ORDER BY s.first_found_at,o.source_token,s.id LIMIT 1) source_browse_id
    FROM recordings d JOIN catalog_tracks c ON c.id=d.discovery_entry_id AND c.recording_id=d.id'''
SQL = {
    TRACK_INDEX: f'CREATE INDEX {TRACK_INDEX} ON recordings(discovery_entry_id) WHERE discovery_entry_id IS NOT NULL',
    'discovered_tracks': VIEW,
    'discovery_entry_reassociation': '''CREATE TRIGGER discovery_entry_reassociation
        BEFORE UPDATE OF recording_id ON track_sources
        WHEN EXISTS(SELECT 1 FROM recordings WHERE discovery_entry_id=OLD.id AND id<>NEW.recording_id)
        BEGIN SELECT RAISE(ABORT,'Replace discovery representative before reassociation'); END''',
    'discovery_candidate_source': '''CREATE TRIGGER discovery_candidate_source
        BEFORE UPDATE OF discovery_entry_id,id ON recordings WHEN NEW.discovery_entry_id IS NOT NULL
        AND NOT EXISTS(SELECT 1 FROM track_sources WHERE id=NEW.discovery_entry_id AND recording_id=NEW.id)
        BEGIN SELECT RAISE(ABORT,'Discovery representative belongs to another recording'); END''',
    'discovery_candidate_source_insert': '''CREATE TRIGGER discovery_candidate_source_insert
        BEFORE INSERT ON recordings WHEN NEW.discovery_entry_id IS NOT NULL
        AND NOT EXISTS(SELECT 1 FROM track_sources WHERE id=NEW.discovery_entry_id AND recording_id=NEW.id)
        BEGIN SELECT RAISE(ABORT,'Discovery representative belongs to another recording'); END''',
    'recording_discovery_candidate': '''CREATE TRIGGER recording_discovery_candidate
        AFTER UPDATE OF discovery_entry_id,has_saved_source,in_collection_album ON recordings
        WHEN NEW.is_discovery<>(NEW.discovery_entry_id IS NOT NULL AND NOT (NEW.has_saved_source OR NEW.in_collection_album))
        BEGIN UPDATE recordings SET is_discovery=(NEW.discovery_entry_id IS NOT NULL AND
            NOT (NEW.has_saved_source OR NEW.in_collection_album)) WHERE id=NEW.id; END''',
    'rec_capture_discovery_candidate': '''CREATE TRIGGER rec_capture_discovery_candidate
        AFTER UPDATE OF discovery_entry_id ON recordings WHEN OLD.discovery_entry_id IS NOT NEW.discovery_entry_id
        BEGIN INSERT INTO rec_change_queue(kind,item) VALUES ('refresh','')
        ON CONFLICT(kind,item) DO UPDATE SET revision=revision+1; END''',
    'rec_content_discovery_candidate': '''CREATE TRIGGER rec_content_discovery_candidate
        AFTER UPDATE OF discovery_entry_id ON recordings WHEN OLD.discovery_entry_id IS NOT NEW.discovery_entry_id
        BEGIN INSERT INTO rec_content_pending(identity_key) VALUES (NEW.identity_key)
        ON CONFLICT(identity_key) DO UPDATE SET revision=revision+1; END''',
}


def predecessor_objects(conn):
    return [sql for sql, in conn.execute('''
        SELECT sql FROM sqlite_schema WHERE sql IS NOT NULL AND (
            tbl_name='discovery_memberships'
            OR name IN ('recordings','discovered_tracks','discovery_entry_reassociation','recording_discovery_collection')
            OR name GLOB 'group_preview_changed_recordings_*')
        ORDER BY type,name
    ''')]


def install(conn, _context):
    for column, declaration in COLUMNS:
        check = ''
        if column != 'discovery_entry_id':
            name = column.removeprefix('discovery_')
            check = ' CHECK(' + domain_contracts.CONTRACTS[MEMBERSHIPS][name].replace(name, column) + ')'
        conn.execute(f'ALTER TABLE recordings ADD COLUMN {column} {declaration}{check}')
    conn.execute('''
        UPDATE recordings SET discovery_entry_id=d.entry_id,discovery_found_at=d.found_at,
            discovery_first_shown=d.first_shown,discovery_last_shown=d.last_shown,discovery_offered_count=d.offered_count,
            is_discovery=NOT (has_saved_source OR in_collection_album)
        FROM discovery_memberships d WHERE recordings.id=d.recording_id
    ''')
    conn.execute('DROP VIEW discovered_tracks')
    conn.execute('DROP TRIGGER discovery_entry_reassociation')
    conn.execute('DROP TRIGGER recording_discovery_collection')
    conn.execute('DROP TABLE ' + MEMBERSHIPS)
    for sql in SQL.values():
        conn.execute(sql)
    group_preview_work.install_guards(conn, sources={'recordings', 'playlist_scopes'})
    conn.execute("INSERT OR IGNORE INTO group_preview_dependencies(table_name,record_key) "
                 "SELECT 'recordings','' FROM group_preview_dependencies "
                 "WHERE table_name='discovery_memberships' LIMIT 1")
    conn.execute("DELETE FROM group_preview_dependencies WHERE table_name='discovery_memberships'")


def validate(conn):
    from .. import content_work

    marker = recommendation_refresh_storage.MARKER
    checkpoint = preparation_checkpoint_storage.INDEX
    actual = definitions(conn, (*SQL, *_DISCOVERY_TRIGGERS, MEMBERSHIPS, marker, checkpoint))
    expected = SQL
    if marker in actual:
        name = 'rec_capture_discovery_candidate'
        sql = recommendation_refresh_storage.refresh_trigger(SQL[name])[0]
        if checkpoint in actual:
            sql = sql.replace(recommendation_refresh_storage.REQUEST_REFRESH, REQUEST_REFRESH)
        expected = {**SQL, name: sql}
    if definitions(conn, (content_work.INDEX,)):
        expected = {name: content_work.upgrade_enqueue_sql(sql) for name, sql in expected.items()}
    if definitions(conn, (content_work.RECORDING_INDEX,)):
        expected = {name: content_work.upgrade_recording_enqueue_sql(sql) for name, sql in expected.items()}
    if (actual.keys() & {*_DISCOVERY_TRIGGERS, MEMBERSHIPS}
            or any(tokens(actual.get(name)) != tokens(sql) for name, sql in expected.items())):
        raise MigrationError('Recording Discovery candidate storage was not folded')
