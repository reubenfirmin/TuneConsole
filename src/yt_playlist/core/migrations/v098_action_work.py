"""Give durable action history explicit targets, progress and backup ownership."""
from pathlib import Path
import re
import sqlite3

from yt_playlist.core import action_work, workflow_settings, append_work, collection_recovery
from .runner import MigrationError


def transform(sql):
    if (re.match(r'CREATE TABLE\s+"?actions"?\s*\(',sql.lstrip(),re.I)
            or re.match(r'CREATE INDEX ix_actions_delete_pending\b',sql.lstrip(),re.I)
            or re.match(r'CREATE TRIGGER playlist_revision_actions_(insert|update|delete)\b',sql.lstrip(),re.I)):
        return ''
    return sql


def fences():
    yield ("CREATE TRIGGER action_identity BEFORE UPDATE ON actions WHEN NEW.id<>OLD.id OR NEW.kind<>OLD.kind "
        'OR NEW.idempotency_key<>OLD.idempotency_key OR NEW.intent_digest<>OLD.intent_digest OR '
        'NEW.parent_action_id IS NOT OLD.parent_action_id OR NEW.undo_of_action_id IS NOT OLD.undo_of_action_id OR '
        'NEW.created_at IS NOT OLD.created_at OR NEW.execution_mode IS NOT OLD.execution_mode OR '
        '(OLD.reviewed_draft_id IS NOT NULL AND (NEW.reviewed_draft_id IS NOT OLD.reviewed_draft_id OR '
        'NEW.reviewed_draft_revision IS NOT OLD.reviewed_draft_revision OR NEW.reviewed_draft_signature IS NOT OLD.reviewed_draft_signature)) '
        "BEGIN SELECT RAISE(ABORT,'Committed action identity and reviewed revision cannot change'); END")
    transitions = {
        'draft':('cancelled',), 'planned':('executing','executed','delete_pending','delete_failed','failed','cancelled'),
        'pending':('executing','executed','failed','cancelled'),
        'executing':('executed','delete_pending','delete_failed','failed'),
        'executed':('undoing','delete_pending','delete_failed'),
        'delete_pending':('executed','delete_failed'),
        'delete_failed':('delete_pending','executed','undoing'),
        'undoing':('undo_failed','undone'), 'undo_failed':('undoing',),
        'failed':('executing','cancelled'), 'cancelled':(), 'undone':(),
    }
    allowed = ' OR '.join("(OLD.status='"+old+"' AND NEW.status IN ("+','.join("'"+state+"'" for state in states)+'))'
                          for old,states in transitions.items() if states)
    yield ('CREATE TRIGGER action_status_transition BEFORE UPDATE ON actions WHEN NEW.status<>OLD.status AND NOT ('+allowed+
        ") BEGIN SELECT RAISE(ABORT,'Invalid action status transition'); END")
    for event in ('INSERT','UPDATE'):
        yield (f'CREATE TRIGGER action_reviewed_draft_{event.lower()} BEFORE {event} ON actions '
            'WHEN NEW.reviewed_draft_id IS NOT NULL '+
            ('AND OLD.reviewed_draft_id IS NULL ' if event=='UPDATE' else '')+
            'AND NOT EXISTS (SELECT 1 FROM action_drafts d WHERE d.id=NEW.reviewed_draft_id '
            'AND d.revision=NEW.reviewed_draft_revision AND d.signature=NEW.reviewed_draft_signature) '
            "BEGIN SELECT RAISE(ABORT,'Reviewed draft revision is no longer current'); END")
        yield (f'CREATE TRIGGER action_draft_owner_{event.lower()} BEFORE {event} ON action_drafts '
            "WHEN NOT EXISTS (SELECT 1 FROM actions a WHERE a.id=NEW.id AND a.kind='merge_draft' AND a.status='draft') "
            "BEGIN SELECT RAISE(ABORT,'Mutable preparation requires its draft action'); END")
    agreement = ("NOT EXISTS (SELECT 1 FROM playlist_scopes s WHERE s.ref=NEW.playlist_scope_id "
        "AND s.provider_id=NEW.provider_playlist_id_snapshot AND "
        "((s.kind='shared' AND s.identity_id IS NULL) OR (s.kind='account' AND s.identity_id=NEW.account_id)))")
    for event in ('INSERT','UPDATE'):
        yield (f'CREATE TRIGGER action_target_agreement_{event.lower()} BEFORE {event} ON action_targets '
               f'WHEN (NEW.playlist_scope_id IS NOT NULL AND ({agreement})) OR '
               '(NEW.membership_ref_id IS NOT NULL AND NOT EXISTS (SELECT 1 FROM playlist_membership_refs m '
               'WHERE m.id=NEW.membership_ref_id AND m.scope_ref IS NEW.playlist_scope_id AND m.identity_id IS NEW.account_id)) '
               "BEGIN SELECT RAISE(ABORT,'Action target disagrees with its account, scope or membership'); END")
    yield ("CREATE TRIGGER action_target_immutable BEFORE UPDATE ON action_targets "
           "BEGIN SELECT RAISE(ABORT,'Committed action targets are immutable'); END")
    yield ("CREATE TRIGGER action_history_delete BEFORE DELETE ON actions WHEN OLD.kind<>'merge_draft' "
           "BEGIN SELECT RAISE(ABORT,'Action history is retained for recovery and links'); END")
    yield ("CREATE TRIGGER action_step_identity BEFORE UPDATE ON action_steps WHEN NEW.action_id<>OLD.action_id "
           "OR NEW.target_id IS NOT OLD.target_id OR NEW.position<>OLD.position OR NEW.operation<>OLD.operation "
           "BEGIN SELECT RAISE(ABORT,'Committed action step identity cannot change'); END")
    yield ("CREATE TRIGGER action_step_completion BEFORE UPDATE ON action_steps WHEN OLD.phase='complete' "
           "AND NEW.phase<>'complete' BEGIN SELECT RAISE(ABORT,'A completed recovery step cannot reopen'); END")
    yield ("CREATE TRIGGER action_step_creation BEFORE UPDATE ON action_steps "
           'WHEN (OLD.creation_marker IS NOT NULL AND NEW.creation_marker IS NOT OLD.creation_marker) OR '
           '(OLD.created_provider_id_snapshot IS NOT NULL AND NEW.created_provider_id_snapshot IS NOT OLD.created_provider_id_snapshot) '
           "BEGIN SELECT RAISE(ABORT,'A committed recovery creation cannot change its marker or destination'); END")
    step_transitions = {
        'prepared':('pending','cancelled'),
        'pending':('uncertain','failed','creating','created','restoring','deleting','remote_complete','complete','cancelled'),
        'uncertain':('failed','remote_complete','complete','restoring'),
        'failed':('pending','uncertain','deleting','restoring','creating','created','remote_complete','complete','cancelled'),
        'creating':('pending','created','failed','remote_complete'),
        'created':('restoring','remote_complete','complete'),
        'restoring':('remote_complete','failed','complete'),
        'deleting':('failed','remote_complete','complete'),
        'remote_complete':('complete',), 'complete':(), 'cancelled':(),
    }
    allowed_steps = ' OR '.join("(OLD.phase='"+old+"' AND NEW.phase IN ("+','.join("'"+state+"'" for state in states)+'))'
                               for old,states in step_transitions.items() if states)
    yield ('CREATE TRIGGER action_step_transition BEFORE UPDATE ON action_steps WHEN NEW.phase<>OLD.phase AND NOT ('+
        allowed_steps+") BEGIN SELECT RAISE(ABORT,'Invalid action step transition'); END")
    yield ("CREATE TRIGGER action_step_backup BEFORE UPDATE ON action_steps WHEN OLD.backup_status='verified' AND "
        '(NEW.backup_status<>OLD.backup_status OR NEW.backup_digest IS NOT OLD.backup_digest OR NEW.backup_bytes IS NOT OLD.backup_bytes) '
        "BEGIN SELECT RAISE(ABORT,'Committed backup content cannot change'); END")
    for event in ('INSERT','UPDATE'):
        yield (f'CREATE TRIGGER action_created_scope_{event.lower()} BEFORE {event} ON action_steps '
            'WHEN NEW.created_playlist_scope_id IS NOT NULL AND NOT EXISTS (SELECT 1 FROM playlist_scopes s '
            'JOIN action_targets t ON t.id=NEW.target_id WHERE s.ref=NEW.created_playlist_scope_id '
            'AND s.provider_id=NEW.created_provider_id_snapshot AND '
            "(s.kind='shared' OR (s.kind='account' AND s.identity_id=t.account_id))) "
            "BEGIN SELECT RAISE(ABORT,'Recovery creation scope disagrees with its target'); END")
        yield (f'CREATE TRIGGER retired_workflow_settings_{event.lower()} BEFORE {event} ON settings '
            "WHEN substr(NEW.key,1,18)='playlist_gc_retry:' OR NEW.key IN "
            "('spotify_library_imported_playlists','spotify_library_imported_albums') OR "
            "substr(NEW.key,1,35)='spotify_library_imported_playlists:' OR "
            "substr(NEW.key,1,32)='spotify_library_imported_albums:' "
            "BEGIN SELECT RAISE(ABORT,'Workflow settings require their explicit owner'); END")
        for table in ('playlist_gc_attempts','spotify_transfer_receipts'):
            yield (f'CREATE TRIGGER {table}_scope_{event.lower()} BEFORE {event} ON {table} '
                'WHEN NEW.playlist_scope_id IS NOT NULL AND NOT EXISTS (SELECT 1 FROM playlist_scopes s '
                'WHERE s.ref=NEW.playlist_scope_id AND s.provider_id=NEW.provider_playlist_id_snapshot '
                "AND (s.kind='shared' OR (s.kind='account' AND s.identity_id=NEW.account_id))) "
                "BEGIN SELECT RAISE(ABORT,'Workflow receipt disagrees with its account and playlist scope'); END")
    yield ("CREATE TRIGGER spotify_transfer_destination BEFORE UPDATE ON spotify_transfer_receipts WHEN "
        'NEW.captured_account_id IS NOT OLD.captured_account_id OR NEW.account_id IS NOT OLD.account_id OR '
        'NEW.source_kind<>OLD.source_kind OR NEW.fingerprint<>OLD.fingerprint OR '
        '(OLD.marker IS NOT NULL AND NEW.marker IS NOT OLD.marker) OR '
        '(OLD.provider_playlist_id_snapshot IS NOT NULL AND NEW.provider_playlist_id_snapshot IS NOT OLD.provider_playlist_id_snapshot) '
        "BEGIN SELECT RAISE(ABORT,'Committed Spotify transfer ownership cannot change'); END")
    yield ("CREATE TRIGGER tv_append_completed BEFORE UPDATE ON tv_append_intents WHEN OLD.state='complete' AND NEW.state<>'complete' "
           "BEGIN SELECT RAISE(ABORT,'Completed TV append cannot reopen'); END")
    for event in ('INSERT','UPDATE'):
        yield (f'CREATE TRIGGER tv_append_recipe_{event.lower()} BEFORE {event} ON tv_append_intents '
            'WHEN NEW.recipe_id IS NOT NULL AND NOT EXISTS(SELECT 1 FROM rec_recipes r WHERE r.id=NEW.recipe_id '
            'AND r.playlist_scope_id=NEW.playlist_scope_id) '
            "BEGIN SELECT RAISE(ABORT,'TV append belongs to another recipe scope'); END")
        yield (f'CREATE TRIGGER radio_operation_revision_{event.lower()} BEFORE {event} ON radio_operations '
            'WHEN NOT EXISTS(SELECT 1 FROM radio_sessions s WHERE s.id=NEW.session_id AND s.revision=NEW.session_revision) '
            "BEGIN SELECT RAISE(ABORT,'Radio operation belongs to another session revision'); END")
    # Pending placement changes invalidate precisely the affected retained
    # membership. Trigger ownership no longer depends on parsing an action plan.
    for event,coordinate in (('INSERT','NEW'),('DELETE','OLD')):
        yield (f'CREATE TRIGGER playlist_revision_action_targets_{event.lower()} AFTER {event} ON action_targets '
               f"WHEN {coordinate}.role='placement' BEGIN INSERT INTO playlist_library_revisions "
               f'SELECT {coordinate}.playlist_scope_id,{coordinate}.account_id,1 FROM actions a '
               f"WHERE a.id={coordinate}.action_id AND a.kind='add_tracks' AND a.status='pending' "
               f'AND {coordinate}.playlist_scope_id IS NOT NULL AND {coordinate}.account_id IS NOT NULL '
               'ON CONFLICT(scope_ref,identity_id) DO UPDATE SET revision=revision+1; END')
    yield ("CREATE TRIGGER playlist_revision_actions_update AFTER UPDATE ON actions "
           "WHEN NEW.kind='add_tracks' AND (OLD.status='pending' OR NEW.status='pending') BEGIN "
           'INSERT INTO playlist_library_revisions SELECT playlist_scope_id,account_id,1 FROM action_targets '
           "WHERE action_id=NEW.id AND role='placement' AND playlist_scope_id IS NOT NULL AND account_id IS NOT NULL "
           'ON CONFLICT(scope_ref,identity_id) DO UPDATE SET revision=revision+1; END')


def statements():
    for name in ('schema_action_work.sql','schema_transfer_work.sql'):
        pending = ''
        for line in Path(__file__).with_name(name).read_text().splitlines(True):
            pending += line
            if sqlite3.complete_statement(pending):
                yield pending
                pending = ''
        if pending.strip():
            raise MigrationError('Incomplete action workflow schema')
    yield from fences()


def apply(conn,context):
    original = list(conn.execute('SELECT * FROM actions ORDER BY id'))
    highwater = conn.execute("SELECT seq FROM sqlite_sequence WHERE name='actions'").fetchone()
    settings = {row[0]:row[1] for row in conn.execute('SELECT key,value FROM settings') if workflow_settings.owns(row[0])}
    try:
        # Decode supported overloaded formats before replacing the SQL owner.
        for row in original:
            draft = row['kind']=='merge_draft'
            action_work.decode(row['params_json'])
            action_work.decode(row['plan_json'],draft=draft)
            action_work.decode(row['undo_json'],draft=draft)
        conn.execute('DROP TABLE actions')
        for sql in statements():
            conn.execute(sql)
        for row in original:
            action_work.insert(conn,row['kind'],row['params_json'],row['plan_json'],row['status'],
                row['undo_json'],row['created_at'],identity=row['id'],executed_at=row['executed_at'],
                idempotency_key='legacy-action:'+str(row['id']),legacy=True)
        if highwater:
            if conn.execute("SELECT 1 FROM sqlite_sequence WHERE name='actions'").fetchone():
                conn.execute("UPDATE sqlite_sequence SET seq=MAX(seq,?) WHERE name='actions'",(highwater[0],))
            else:
                conn.execute("INSERT INTO sqlite_sequence(name,seq) VALUES ('actions',?)",(highwater[0],))
        workflow_settings.put(conn,settings,legacy=True)
        conn.executemany('DELETE FROM settings WHERE key=?',[(key,) for key in settings])
        for row in conn.execute('SELECT * FROM rec_recipes').fetchall():
            state = collection_recovery.read(row['recovery_filename'],row['recovery_digest'],row['recovery_bytes'],kind='playlist-recipe',owner=row['id'])
            append_work.save_tv(conn,row['id'],row['playlist_scope_id'],state)
        for row in conn.execute('SELECT * FROM radio_sessions').fetchall():
            state = collection_recovery.read(row['recovery_filename'],row['recovery_digest'],row['recovery_bytes'],kind='radio-session',owner=row['id'])
            append_work.save_radio(conn,row['id'],row['revision'],state)
    except (ValueError,TypeError,KeyError,sqlite3.IntegrityError) as exc:
        raise MigrationError(f'Action conversion failed; original history retained: {exc}') from exc
