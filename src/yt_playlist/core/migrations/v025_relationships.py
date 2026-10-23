"""Enforce membership order and dependent references without deleting historical evidence."""
import sqlite3
import json
import math
from collections import defaultdict

from .runner import MigrationError
from .v008_recordings import _archive


REPLACED = {'enrichment_log','enrichment_conflict','rec_mode_impressions',
            'rec_mode_picks','road_trip_drafts'}
TABLES = (
    'CREATE TABLE playlist_membership_refs (id INTEGER PRIMARY KEY AUTOINCREMENT,scope_ref TEXT REFERENCES playlist_scopes(ref),identity_id INTEGER REFERENCES identities(id),CHECK((scope_ref IS NULL)=(identity_id IS NULL)))',
    'CREATE TABLE enrichment_log (id INTEGER PRIMARY KEY,track_id INTEGER NOT NULL REFERENCES catalog_entries(id),run_id TEXT NOT NULL,provider TEXT NOT NULL,field TEXT NOT NULL,value TEXT,created_at REAL,recording_id INTEGER REFERENCES recordings(id),input_video_id TEXT REFERENCES youtube_items(video_id),field_scope TEXT NOT NULL DEFAULT \'legacy_item\')',
    'CREATE TABLE enrichment_conflict (id INTEGER PRIMARY KEY,track_id INTEGER NOT NULL REFERENCES catalog_entries(id),field TEXT NOT NULL,candidates TEXT NOT NULL,resolved INTEGER NOT NULL DEFAULT 0,resolved_value TEXT,updated_at REAL,recording_id INTEGER REFERENCES recordings(id),input_video_id TEXT REFERENCES youtube_items(video_id),field_scope TEXT NOT NULL DEFAULT \'legacy_item\',UNIQUE(track_id,field))',
    'CREATE TABLE rec_mode_impressions (epoch INTEGER NOT NULL,lane TEXT NOT NULL,mode_id INTEGER REFERENCES taste_modes(mode_id),ranker TEXT,created_at REAL NOT NULL,legacy_mode_id INTEGER,CHECK((mode_id IS NOT NULL)<>(legacy_mode_id IS NOT NULL)),PRIMARY KEY(epoch,lane))',
    'CREATE TABLE rec_mode_picks (playlist_id INTEGER PRIMARY KEY NOT NULL REFERENCES playlist_membership_refs(id),mode_id INTEGER REFERENCES taste_modes(mode_id),ranker TEXT,created_at REAL NOT NULL,legacy_mode_id INTEGER,CHECK((mode_id IS NOT NULL)<>(legacy_mode_id IS NOT NULL)))',
    'CREATE TABLE road_trip_drafts (recipe_id INTEGER PRIMARY KEY REFERENCES road_trip_recipes(id) ON DELETE CASCADE,state TEXT NOT NULL,updated_at REAL NOT NULL)',
)
INDEXES = (
    'CREATE INDEX idx_enrichment_log_track ON enrichment_log(track_id)',
    'CREATE UNIQUE INDEX ix_playlist_item_position ON playlist_items(playlist_id,position)',
    'CREATE UNIQUE INDEX ix_unresolved_playlist_position ON unresolved_playlist_items(playlist_id,position)',
    'CREATE INDEX ix_unresolved_playlist_entry ON unresolved_playlist_items(entry_id,playlist_id)',
    'CREATE INDEX ix_history_snapshot_account_time ON history_snapshots(identity_id,taken_at)',
    'CREATE INDEX ix_history_item_key_snapshot ON history_items(identity_key,snapshot_id)',
    'CREATE INDEX ix_history_item_recording_snapshot ON history_items(recording_id,snapshot_id)',
    'CREATE INDEX ix_play_event_recording_time ON play_events(recording_id,played_at)',
    'CREATE INDEX ix_play_event_account_time ON play_events(identity_id,played_at DESC,id DESC)',
    'CREATE INDEX ix_play_event_account_key_time ON play_events(identity_id,identity_key,played_at)',
    'CREATE INDEX ix_play_event_account_playlist_time ON play_events(identity_id,playlist_ytm_id,played_at)',
    'CREATE INDEX ix_player_event_account_playlist_time ON player_events(identity_id,playlist_ytm_id,at)',
    'CREATE INDEX ix_mode_pick_mode_time ON rec_mode_picks(mode_id,created_at)',
)
SCOPE_VALID = "EXISTS(SELECT 1 FROM playlist_scopes s WHERE s.ref=NEW.scope_ref AND s.provider_id=NEW.ytm_playlist_id AND ((s.kind='account' AND s.identity_id=NEW.identity_id AND s.provider_id IN ('LM','SE')) OR (s.kind='shared' AND s.identity_id IS NULL AND s.provider_id NOT IN ('LM','SE'))))"
TRIGGERS = tuple(
    f"CREATE TRIGGER {table}_position_{operation.lower()} BEFORE {operation} ON {table} WHEN typeof(NEW.position)<>'integer' OR NEW.position<0 OR EXISTS(SELECT 1 FROM {other} WHERE playlist_id=NEW.playlist_id AND position=NEW.position) BEGIN SELECT RAISE(ABORT,'Invalid or occupied playlist position'); END"
    for table,other in (('playlist_items','unresolved_playlist_items'),('unresolved_playlist_items','playlist_items'))
    for operation in ('INSERT','UPDATE')
) + tuple(
    f"CREATE TRIGGER playlists_scope_{operation.lower()} BEFORE {operation} ON playlists WHEN NOT {SCOPE_VALID} BEGIN SELECT RAISE(ABORT,'Playlist scope does not match its account and provider'); END"
    for operation in ('INSERT','UPDATE')
) + (
    "CREATE TRIGGER playlists_membership_insert BEFORE INSERT ON playlists WHEN EXISTS(SELECT 1 FROM playlist_membership_refs m WHERE m.id=NEW.id AND (m.scope_ref IS NOT NEW.scope_ref OR m.identity_id IS NOT NEW.identity_id)) BEGIN SELECT RAISE(ABORT,'Playlist membership ID already belongs to retained history'); END",
    "CREATE TRIGGER playlists_membership_update BEFORE UPDATE ON playlists WHEN NEW.id IS NOT OLD.id OR NEW.scope_ref IS NOT OLD.scope_ref OR NEW.identity_id IS NOT OLD.identity_id BEGIN SELECT RAISE(ABORT,'Playlist membership identity is immutable'); END",
    "CREATE TRIGGER playlists_membership_record AFTER INSERT ON playlists BEGIN INSERT OR IGNORE INTO playlist_membership_refs(id,scope_ref,identity_id) VALUES (NEW.id,NEW.scope_ref,NEW.identity_id); SELECT CASE WHEN EXISTS(SELECT 1 FROM playlist_membership_refs m WHERE m.id=NEW.id AND (m.scope_ref IS NOT NEW.scope_ref OR m.identity_id IS NOT NEW.identity_id)) THEN RAISE(ABORT,'Playlist membership ID already belongs to retained history') END; END",
    "CREATE TRIGGER playlist_membership_refs_update BEFORE UPDATE ON playlist_membership_refs WHEN NEW.id IS NOT OLD.id OR NEW.scope_ref IS NOT OLD.scope_ref OR NEW.identity_id IS NOT OLD.identity_id BEGIN SELECT RAISE(ABORT,'Retained playlist membership identity is immutable'); END",
    "CREATE TRIGGER playlist_membership_refs_delete BEFORE DELETE ON playlist_membership_refs WHEN EXISTS(SELECT 1 FROM playlists WHERE id=OLD.id) BEGIN SELECT RAISE(ABORT,'Playlist still references this membership'); END",
)


def _save(conn,table,row):
    fields = ','.join(row)
    conn.execute(f'INSERT INTO {table}({fields}) VALUES ({",".join("?" for _ in row)})',tuple(row.values()))


def apply(conn,context):
    previous = conn.row_factory
    conn.row_factory = sqlite3.Row
    try:
        _apply(conn)
    finally:
        conn.row_factory = previous


def _apply(conn):
    old = {t:[dict(r) for r in conn.execute(f'SELECT * FROM {t} ORDER BY rowid')] for t in REPLACED}
    # Membership keys already deduplicate exact uploads and unresolved entries. Repair only
    # ambiguous order; a stable tie-break preserves every independent membership.
    members = defaultdict(list)
    for table,key in (('playlist_items','video_id'),('unresolved_playlist_items','entry_id')):
        for row in conn.execute(f'SELECT * FROM {table}'):
            members[row['playlist_id']].append((table,key,dict(row)))
    for pid,items in members.items():
        positions = [r['position'] for _,_,r in items]
        if all(type(p) is int and p>=0 for p in positions) and len(set(positions))==len(positions):
            continue
        def order(item):
            table,key,row = item
            value = row['position']
            numeric = isinstance(value,(int,float)) and math.isfinite(value)
            return (0,value,table,str(row[key])) if numeric else (1,str(value),table,str(row[key]))
        for position,(table,key,row) in enumerate(sorted(items,key=order)):
            if row['position']!=position:
                _archive(conn,'constraint_legacy:'+table,f'{pid}:{row[key]}',row,'stable reconciliation of invalid or colliding positions')
                conn.execute(f'UPDATE {table} SET position=? WHERE playlist_id=? AND {key}=?',(position,pid,row[key]))
    # A scope is durable; correcting a membership never moves its curation to another account.
    for row in conn.execute('SELECT * FROM playlists').fetchall():
        remote,owner = row['ytm_playlist_id'],row['identity_id']
        account = remote in {'LM','SE'}
        ref = f'@account:{owner}:{remote}' if account else remote
        if ref!=row['scope_ref']:
            _archive(conn,'constraint_legacy:playlists',row['id'],row,'membership scope corrected; curation retained on its original scope')
            conn.execute('INSERT OR IGNORE INTO playlist_scopes VALUES (?,?,?,?)',
                         (ref,remote,owner if account else None,'account' if account else 'shared'))
            conn.execute('UPDATE playlists SET scope_ref=? WHERE id=?',(ref,row['id']))
    for table in REPLACED:
        conn.execute(f'DROP TABLE {table}')
    for sql in TABLES:
        conn.execute(sql)
    conn.execute('INSERT INTO playlist_membership_refs(id,scope_ref,identity_id) SELECT id,scope_ref,identity_id FROM playlists')
    # Deleted legacy memberships retain their exact local handle with unknown owner/scope.
    conn.executemany('INSERT OR IGNORE INTO playlist_membership_refs(id) VALUES (?)',
                     [(r['playlist_id'],) for r in old['rec_mode_picks']])
    for table in ('enrichment_log','enrichment_conflict'):
        for source in old[table]:
            row = dict(source)
            target = conn.execute('SELECT id FROM catalog_entries WHERE id=?',(row['track_id'],)).fetchone()
            if target is None:
                target = conn.execute('SELECT entry_id FROM legacy_track_refs WHERE legacy_id=?',(row['track_id'],)).fetchone()
            if target is None:
                _archive(conn,'constraint_legacy:'+table,row['id'],source,'unresolvable enrichment target; original finding retained')
                continue
            row['track_id'] = target[0]
            for field,parent,key in (('recording_id','recordings','id'),('input_video_id','youtube_items','video_id')):
                if row[field] is not None and not conn.execute(f'SELECT 1 FROM {parent} WHERE {key}=?',(row[field],)).fetchone():
                    row[field] = None
            if row!=source:
                _archive(conn,'constraint_legacy:'+table,row['id'],source,'canonical reference repaired; original context retained')
            conflict = conn.execute('SELECT * FROM enrichment_conflict WHERE track_id=? AND field=?',(row['track_id'],row['field'])).fetchone() if table=='enrichment_conflict' else None
            if conflict:
                _archive(conn,'constraint_legacy:'+table,source['id'],source,'multiple legacy references target the same conflict')
                prior = dict(conflict)
                _archive(conn,'constraint_legacy:'+table,prior['id'],prior,'multiple legacy references target the same conflict')
                candidates = {}
                for evidence in (prior,row):
                    try:
                        values = json.loads(evidence['candidates'])
                    except (ValueError,TypeError):
                        values = []
                    for value in values if isinstance(values,list) else []:
                        candidates[json.dumps(value,sort_keys=True)] = value
                resolved = bool(prior['resolved'] and row['resolved'] and prior['resolved_value']==row['resolved_value'])
                conn.execute('UPDATE enrichment_conflict SET candidates=?,resolved=?,resolved_value=?,updated_at=?,recording_id=?,input_video_id=? WHERE id=?',
                    (json.dumps(list(candidates.values())),int(resolved),prior['resolved_value'] if resolved else None,
                     max((v for v in (prior['updated_at'],row['updated_at']) if isinstance(v,(int,float))),default=None),
                     prior['recording_id'] if prior['recording_id']==row['recording_id'] else None,
                     prior['input_video_id'] if prior['input_video_id']==row['input_video_id'] else None,prior['id']))
                continue
            _save(conn,table,row)
    for table in ('rec_mode_impressions','rec_mode_picks'):
        for source in old[table]:
            row = dict(source)
            if not conn.execute('SELECT 1 FROM taste_modes WHERE mode_id=?',(row['mode_id'],)).fetchone():
                row['legacy_mode_id'],row['mode_id'] = row['mode_id'],None
            _save(conn,table,row)
    for row in old['road_trip_drafts']:
        if conn.execute('SELECT 1 FROM road_trip_recipes WHERE id=?',(row['recipe_id'],)).fetchone():
            _save(conn,'road_trip_drafts',row)
        else:
            _archive(conn,'constraint_legacy:road_trip_drafts',row['recipe_id'],row,'draft has no owning recipe')
    for sql in (*INDEXES,*TRIGGERS):
        conn.execute(sql)


def validate(conn):
    for sql in (*TABLES,*INDEXES,*TRIGGERS):
        parts = sql.split()
        name = parts[3] if parts[1]=='UNIQUE' else parts[2]
        actual = conn.execute('SELECT sql FROM sqlite_master WHERE name=?',(name,)).fetchone()
        if not actual or actual[0]!=sql:
            raise MigrationError(f'Incompatible relationship constraint: {name}')
    if conn.execute('PRAGMA foreign_key_check').fetchone():
        raise MigrationError('Invalid constrained relationship')
    if conn.execute('SELECT 1 FROM playlist_tracks GROUP BY playlist_id,position HAVING COUNT(*)>1 LIMIT 1').fetchone():
        raise MigrationError('Colliding playlist positions')
    if conn.execute("SELECT 1 FROM playlist_tracks WHERE typeof(position)<>'integer' OR position<0 LIMIT 1").fetchone():
        raise MigrationError('Invalid playlist position')
    predicate = SCOPE_VALID.replace('NEW.','p.')
    if conn.execute(f'SELECT 1 FROM playlists p WHERE NOT {predicate} LIMIT 1').fetchone():
        raise MigrationError('Invalid playlist scope ownership')
    if conn.execute('SELECT 1 FROM playlists p LEFT JOIN playlist_membership_refs m ON m.id=p.id WHERE m.id IS NULL OR m.scope_ref IS NOT p.scope_ref OR m.identity_id IS NOT p.identity_id LIMIT 1').fetchone():
        raise MigrationError('Invalid retained playlist membership')
