"""Fold album state into release, provider ID and paid request owners."""
import re

from .. import album_note_requests
from .. import domain_contracts, group_preview_work, legacy_journals, library_scope_changes, recording_library
from ..schema_contract import MigrationError
from ..schema_inspection import catalog_rows, definitions
from . import album_storage_preparation as preparation
from .account_storage import _preserve_sequence, _sequence
from .track_source_cutover import _replace_table


NAME = 'fold_album_storage'
TABLES = (
    'album_discovery_memberships', 'unattributed_album_memberships', 'album_alias_reconciliations',
    'album_alias_checks', 'album_provider_fingerprints', 'album_share_items', 'album_share_snapshots',
    'album_identity_evidence', 'album_note_requests', 'album_note_outputs', 'album_playback_intent_items',
    'album_descriptions', 'description_file_writes',
)
VIEWS = ('album_fetch_state', 'album_primary_provider_ids', 'album_track_links',
         'album_tracklist_keys', 'saved_albums')
FK_OWNERS = (*TABLES, 'albums', 'album_provider_ids', 'album_tracklists', 'album_description_requests',
             'album_playback_intents', 'album_track_occurrences', 'album_occurrence_sources')
SUCCESS_GUARDS = ('album_description_success_insert', 'album_description_success_update')
CHANGED = frozenset((
    'albums', 'album_provider_ids', 'album_tracklists', 'album_description_requests', 'album_playback_intents',
    'album_occurrence_frozen_delete', 'album_source_frozen_delete',
    'discovered_albums', 'discovery_origin_tokens', 'saved_album_memberships', legacy_journals.FILE_REFERENCES,
))
MARKER = library_scope_changes.ALBUM_STORAGE_INDEX
PROVIDER_COLUMNS = (
    'fingerprint_digest TEXT', 'fingerprint_at REAL',
    "share_json TEXT CHECK(share_json IS NULL OR (json_valid(share_json) AND json_type(share_json)='object'))",
    'share_order INTEGER',
    'note_request_order INTEGER',
    "note_work_revision INTEGER NOT NULL DEFAULT 0 CHECK(typeof(note_work_revision)='integer' AND note_work_revision>=0)",
    'note_pending INTEGER NOT NULL DEFAULT 0 CHECK(note_pending IN (0,1) AND '
    '(note_pending=0 OR (note_request_order IS NOT NULL AND note_work_revision>0)))',
    "note_requested_until REAL CHECK(note_requested_until IS NULL OR (note_pending=1 "
    "AND typeof(note_requested_until) IN ('real','integer') AND abs(note_requested_until)<=1.7976931348623157e308))",
    'note_numeric_year INTEGER NOT NULL DEFAULT 0 CHECK(note_numeric_year IN (0,1))',
    "note_json TEXT CHECK(note_json IS NULL OR (length(CAST(note_json AS BLOB))<=1048576 "
    "AND json_valid(note_json) AND json_type(note_json)='object'))",
    'note_output_order INTEGER',
)
DISCOVERED = '''
CREATE VIEW discovered_albums AS
SELECT p.provider_id browse_id,a.artist,a.title,a.year,a.thumbnail,a.discovery_found_at found_at,
    (SELECT max(shown_at) FROM discovery_impressions WHERE album_id=a.id) last_shown,
    COALESCE((SELECT g.name FROM genre_catalog g WHERE g.key=a.genre_key),a.unresolved_genre) genre,
    (SELECT min(shown_at) FROM discovery_impressions WHERE album_id=a.id) first_shown,
    (SELECT count(*) FROM discovery_impressions WHERE album_id=a.id) offered_count,
    a.earlier_offer_count legacy_offered_count,a.earlier_first_shown legacy_first_shown,
    a.earlier_last_shown legacy_last_shown,
    (SELECT artist_id FROM album_artists aa WHERE aa.album_id=a.id ORDER BY position LIMIT 1) artist_id
FROM albums a JOIN album_provider_ids p ON p.album_id=a.id AND p.provider='youtube' AND p.is_primary=1
WHERE a.discovery_found_at IS NOT NULL
'''


def installed(conn):
    return MARKER in definitions(conn, (MARKER,))


def predecessor_objects(conn):
    """Capture just the definitions changed or retired by this migration."""
    names = CHANGED | set(TABLES) | set(VIEWS)
    owners = set(TABLES) | {'discovered_albums', 'albums', 'album_provider_ids', 'album_tracklists',
                            'album_description_requests', 'album_playback_intents'}
    return [sql for sql, in conn.execute(
        "SELECT sql FROM sqlite_schema WHERE sql IS NOT NULL AND "
        f"(name IN ({','.join('?' for _ in names)}) OR tbl_name IN ({','.join('?' for _ in owners)})) "
        "ORDER BY type,name", (*sorted(names), *sorted(owners)))]


def _rebuild(conn, table, sql, *, expressions=None, omit=(), guards=None):
    owned = conn.execute("SELECT name,sql FROM sqlite_schema WHERE tbl_name=? "
                         "AND type IN ('index','trigger') AND sql IS NOT NULL ORDER BY type,name",
                         (table,)).fetchall()
    previous = _sequence(conn, table)
    _replace_table(conn, table, sql, expressions=expressions)
    _preserve_sequence(conn, table, previous)
    guards = guards or {}
    for name, definition in owned:
        if name not in omit:
            conn.execute(guards.get(name, definition))


def _fold_albums(conn, sql):
    additions = (
        f'discovery_found_at REAL CHECK({domain_contracts.finite("discovery_found_at")})',
        'unattributed_saved_source TEXT',
        f'unattributed_saved_at REAL CHECK({domain_contracts.finite("unattributed_saved_at")}) '
        'CHECK(unattributed_saved_at IS NULL OR unattributed_saved_source IS NOT NULL)',
        'canonical_album_id INTEGER REFERENCES albums(id) CHECK(canonical_album_id IS NULL OR canonical_album_id<>id)',
        'merge_digest TEXT',
        'merged_at REAL',
    )
    sql = sql.replace('legacy_key TEXT UNIQUE,', ','.join(additions) + ',', 1)
    _rebuild(conn, 'albums', sql, omit=('group_preview_changed_albums_update',), expressions={
        'discovery_found_at': '(SELECT COALESCE(found_at,first_shown,last_shown,0) '
                              'FROM album_discovery_memberships WHERE album_id=original.id)',
        'unattributed_saved_source': '(SELECT source FROM unattributed_album_memberships WHERE album_id=original.id)',
        'unattributed_saved_at': '(SELECT saved_at FROM unattributed_album_memberships WHERE album_id=original.id)',
        'canonical_album_id': '(SELECT album_id FROM album_alias_reconciliations WHERE retired_id=original.id)',
        'merge_digest': '(SELECT comparison_digest FROM album_alias_reconciliations WHERE retired_id=original.id)',
        'merged_at': '(SELECT verified_at FROM album_alias_reconciliations WHERE retired_id=original.id)',
    })
    conn.execute(f'CREATE INDEX {MARKER} ON albums(canonical_album_id) WHERE canonical_album_id IS NOT NULL')
    conn.execute('CREATE INDEX ix_album_unattributed ON albums(id) WHERE unattributed_saved_source IS NOT NULL')


def _fold_provider_state(conn, sql):
    # A single replacement avoids reparsing the installed schema for each
    # added column. Every pre-existing provider identifier is retained.
    sql = sql.replace(',PRIMARY KEY(provider,provider_id)', ', ' + ', '.join(PROVIDER_COLUMNS)
                      + ',PRIMARY KEY(provider,provider_id)', 1)
    expressions = {column.split()[0]: 'NULL' for column in PROVIDER_COLUMNS}
    expressions.update(note_work_revision='0', note_pending='0', note_numeric_year='0')
    _rebuild(conn, 'album_provider_ids', sql, expressions=expressions)
    conn.execute('''
        UPDATE album_provider_ids AS p SET (fingerprint_digest,fingerprint_at)=
        (f.digest,f.observed_at) FROM album_provider_fingerprints f
        WHERE f.provider=p.provider AND f.provider_id=p.provider_id
    ''')
    if conn.execute('''
        SELECT 1 FROM album_share_snapshots s LEFT JOIN album_provider_ids p
        ON p.provider='youtube' AND p.provider_id=s.captured_provider_id AND p.album_id=s.album_id
        WHERE p.provider_id IS NULL LIMIT 1
    ''').fetchone():
        raise MigrationError('Album share has no matching provider ID')
    conn.execute('''
        WITH latest AS (SELECT max(id) id FROM album_share_snapshots GROUP BY album_id,captured_provider_id)
        UPDATE album_provider_ids AS p SET share_order=s.id,
            share_json=json_object('title',s.title,'video_ids',json((
                SELECT json_group_array(coalesce(i.raw_source_ref,t.video_id) ORDER BY i.position)
                FROM album_share_items i LEFT JOIN track_sources t ON t.id=i.entry_id WHERE i.snapshot_id=s.id
            )))
        FROM latest JOIN album_share_snapshots s ON s.id=latest.id
        WHERE p.provider='youtube' AND p.provider_id=s.captured_provider_id AND p.album_id=s.album_id
    ''')
    for identity, revision, *fields in conn.execute(
            'SELECT id,work_revision,browse_id,artist,title,year_text,year_number,thumbnail,requested_until '
            'FROM album_note_requests ORDER BY id').fetchall():
        album_note_requests.store(conn, fields)
        conn.execute("UPDATE album_provider_ids SET note_request_order=?,note_work_revision=? "
                     "WHERE provider='youtube' AND provider_id=?", (identity, revision, fields[0]))
    for browse, in conn.execute(f'''
        SELECT r.browse_id FROM album_note_outputs r JOIN settings s ON s.key=({preparation.NOTE_KEY})
        LEFT JOIN album_provider_ids p ON p.provider='youtube' AND p.provider_id=r.browse_id
        WHERE s.value IS NOT NULL AND p.provider_id IS NULL ORDER BY r.id
    ''').fetchall():
        album_note_requests.admit(conn, browse)
    conn.execute(f'''
        WITH notes AS (
            SELECT r.browse_id,s.value,row_number() OVER (ORDER BY r.id) output_order
            FROM album_note_outputs r JOIN settings s ON s.key=({preparation.NOTE_KEY}) WHERE s.value IS NOT NULL
        )
        UPDATE album_provider_ids AS p SET note_json=notes.value,note_output_order=notes.output_order
        FROM notes WHERE p.provider='youtube' AND p.provider_id=notes.browse_id
    ''')
    for name, column in (('ix_album_fingerprint', 'provider,fingerprint_digest'),
                         ('ix_album_recent_share', 'share_order'),
                         ('ix_album_pending_note', 'note_request_order'),
                         ('ix_album_recent_note', 'note_output_order')):
        if name == 'ix_album_fingerprint':
            conn.execute('DROP INDEX ix_album_fingerprint')
        conn.execute(f'CREATE INDEX {name} ON album_provider_ids({column}) '
                     f'WHERE {column.split(",")[-1]} IS NOT NULL')
    conn.execute("CREATE TRIGGER album_note_inputs_changed AFTER UPDATE OF title,artist,year,thumbnail ON albums "
                 "WHEN OLD.title IS NOT NEW.title OR OLD.artist IS NOT NEW.artist OR OLD.year IS NOT NEW.year "
                 "OR OLD.thumbnail IS NOT NEW.thumbnail BEGIN UPDATE album_provider_ids "
                 "SET note_work_revision=note_work_revision+1 WHERE album_id=NEW.id AND note_pending=1; END")
    conn.execute("CREATE TRIGGER album_note_subject_changed AFTER UPDATE OF album_id ON album_provider_ids "
                 "WHEN OLD.album_id IS NOT NEW.album_id AND OLD.note_pending=1 BEGIN UPDATE album_provider_ids "
                 "SET note_work_revision=note_work_revision+1 WHERE provider=NEW.provider "
                 "AND provider_id=NEW.provider_id; END")


def _fold_shares(conn, sql):
    for table in ('album_share_items', 'album_share_snapshots'):
        conn.execute('DROP TABLE ' + table)
    for name in ('album_occurrence_frozen_delete', 'album_source_frozen_delete'):
        original = sql[name]
        conn.execute('DROP TRIGGER ' + name)
        conn.execute(re.sub(r"capture_kind<>'provider' OR ", "capture_kind='legacy_full' OR ",
                            re.sub(r' OR EXISTS\(SELECT 1 FROM album_share_snapshots s WHERE s.tracklist_id=\w+.id\)',
                                   '', original)))
    obsolete = "SELECT id FROM album_tracklists WHERE capture_kind IN ('legacy_share','share')"
    conn.execute(f'DELETE FROM album_occurrence_sources WHERE occurrence_id IN (SELECT id FROM '
                 f'album_track_occurrences WHERE tracklist_id IN ({obsolete}))')
    conn.execute(f'DELETE FROM album_track_occurrences WHERE tracklist_id IN ({obsolete})')
    conn.execute(f'DELETE FROM album_tracklists WHERE id IN ({obsolete})')
    ddl = sql['album_tracklists'].replace("'provider','legacy_full','legacy_share','share'", "'provider','legacy_full'")
    ddl = ddl.replace("'building','partial','complete','snapshot'", "'building','partial','complete'")
    ddl = re.sub(r"    CHECK\(\(status<>'snapshot'.*?\n", '', ddl)
    _rebuild(conn, 'album_tracklists', ddl, guards={
        'album_tracklist_finish': sql['album_tracklist_finish'].replace("IN ('complete','snapshot')", "='complete'")
            .replace("(NEW.status='complete' AND NEW.expected_count=0)", "NEW.expected_count=0"),
    })


def _fold_playback(conn, sql):
    sql = sql.replace('has_videos INTEGER NOT NULL CHECK(has_videos IN (0,1))',
                      "video_ids TEXT CHECK(video_ids IS NULL OR (json_valid(video_ids) AND json_type(video_ids)='array'))")
    _rebuild(conn, 'album_playback_intents', sql, expressions={
        'video_ids': 'CASE WHEN original.has_videos=1 THEN (SELECT json_group_array(video_id ORDER BY position) '
                     'FROM album_playback_intent_items WHERE intent_id=original.singleton) END',
    })
    conn.execute('DROP TABLE album_playback_intent_items')


def _fold_paid(conn, sql):
    start, end = sql.index('outcome_filename TEXT'), sql.index('CHECK((resolution_status=')
    replacement = (
        "outcome_json TEXT CHECK(outcome_json IS NULL OR (length(CAST(outcome_json AS BLOB))<=262144 "
        "AND json_valid(outcome_json) AND json_type(outcome_json)='object')),"
        "result_created_at REAL,result_order INTEGER,"
        "CHECK(status<>'ready' OR (outcome_json IS NOT NULL AND result_created_at IS NOT NULL "
        "AND result_order IS NOT NULL)),"
    )
    retained = ("CREATE TRIGGER album_description_success_retained BEFORE UPDATE ON album_description_requests "
                "WHEN OLD.status='ready' AND (NEW.status IS NOT OLD.status OR NEW.outcome_json IS NOT OLD.outcome_json "
                "OR NEW.result_created_at IS NOT OLD.result_created_at "
                "OR NEW.result_order IS NOT OLD.result_order) "
                "BEGIN SELECT RAISE(ABORT,'Successful album description cannot be replaced'); END")
    outcome = f'(SELECT value FROM settings WHERE key=({preparation.PAID_KEY}))'
    # Keep completed attempts closed to paid retries even when their body was lost.
    response = f"CASE WHEN original.status='ready' THEN COALESCE({outcome},json_object('status','unavailable')) ELSE {outcome} END"
    _rebuild(conn, 'album_description_requests', sql[:start] + replacement + sql[end:],
             omit=SUCCESS_GUARDS, guards={'album_description_success_retained': retained}, expressions={
                 'outcome_json': response,
                 'result_created_at': "CASE WHEN original.status='ready' THEN (SELECT created_at "
                                      'FROM album_descriptions WHERE request_id=original.id) END',
                 'result_order': "CASE WHEN original.status='ready' THEN (SELECT id "
                                 'FROM album_descriptions WHERE request_id=original.id) END',
             })
    conn.execute('DROP TABLE description_file_writes')
    conn.execute('DROP TABLE album_descriptions')


def _membership_guards(conn):
    for name, statement in library_scope_changes.triggers(folded=True, album_folded=True):
        if name == 'library_scope_album_unattributed':
            conn.execute(statement)
    for name, statement in recording_library.triggers(album_folded=True):
        if name == 'recording_library_album_unattributed':
            conn.execute(statement)
    for name, body in (
        ('rec_capture_album_unattributed', "INSERT INTO rec_change_queue(kind,item) VALUES ('refresh','') "
         "ON CONFLICT(kind,item) DO UPDATE SET revision=revision+1;"),
        ('rec_evidence_album_unattributed', 'UPDATE rec_preparation SET evidence_changes=evidence_changes+1 WHERE id=1;'),
    ):
        conn.execute(f'CREATE TRIGGER {name} AFTER UPDATE OF unattributed_saved_source ON albums '
                     f'WHEN {library_scope_changes.UNATTRIBUTED_CHANGED} BEGIN {body} END')
    group_preview_work.install_guards(conn, sources={'albums', 'album_provider_ids'})
    conn.execute('UPDATE group_preview_work SET invalid=1 WHERE singleton=1 AND invalid=0')


prepare = preparation.prepare


def install(conn, _context):
    preparation.require_current(conn)
    sql = {name: statement for name, _kind, statement in catalog_rows(conn)}
    _fold_albums(conn, sql['albums'])
    _fold_provider_state(conn, sql['album_provider_ids'])
    _fold_shares(conn, sql)
    _fold_playback(conn, sql['album_playback_intents'])
    _fold_paid(conn, sql['album_description_requests'])
    for name in (*VIEWS, 'discovered_albums', 'discovery_origin_tokens', 'saved_album_memberships'):
        conn.execute('DROP VIEW ' + name)
    already_dropped = {'album_share_items', 'album_share_snapshots', 'album_playback_intent_items',
                       'album_descriptions', 'description_file_writes'}
    # Album identity receipts describe old inputs. The rebuilt albums and
    # their original relationships already retain the live data.
    for table in TABLES:
        if table not in already_dropped:
            conn.execute('DROP TABLE ' + table)
    conn.execute('CREATE INDEX ix_discovery_album_impressions ON discovery_impressions(album_id,shown_at) '
                 'WHERE album_id IS NOT NULL')
    conn.execute(DISCOVERED)
    conn.execute("CREATE TRIGGER discovered_albums_delete INSTEAD OF DELETE ON discovered_albums BEGIN "
                 "UPDATE albums SET discovery_found_at=NULL WHERE id=(SELECT album_id FROM album_provider_ids "
                 "WHERE provider='youtube' AND provider_id=OLD.browse_id); END")
    conn.execute(sql['discovery_origin_tokens'].replace('FROM album_primary_provider_ids WHERE',
                                                        'FROM album_provider_ids WHERE is_primary=1 AND'))
    conn.execute('CREATE VIEW saved_album_memberships AS SELECT account_id,album_id,source,saved_at FROM account_albums '
                 'UNION ALL SELECT NULL,id,unattributed_saved_source,unattributed_saved_at FROM albums '
                 'WHERE unattributed_saved_source IS NOT NULL')
    legacy_journals._refresh_file_references(conn)
    _membership_guards(conn)
    preparation.clear(conn)


def validate(conn):
    if not installed(conn) or definitions(conn, (*TABLES, *VIEWS, *SUCCESS_GUARDS)):
        raise MigrationError('Album storage was not folded')
