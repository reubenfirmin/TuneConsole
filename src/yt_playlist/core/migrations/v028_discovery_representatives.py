"""Repair discovery representatives after partial upload reassociation, retaining evidence."""
import json

from .runner import MigrationError


SQL = tuple(
    f"CREATE TRIGGER discovery_membership_{operation.lower()} BEFORE {operation} ON discovery_memberships WHEN NOT EXISTS(SELECT 1 FROM catalog_entries e WHERE e.id=NEW.entry_id AND e.recording_id=NEW.recording_id) BEGIN SELECT RAISE(ABORT,'Discovery representative belongs to another recording'); END"
    for operation in ('INSERT','UPDATE')
) + (
    "CREATE TRIGGER discovery_entry_reassociation BEFORE UPDATE OF recording_id ON catalog_entries WHEN EXISTS(SELECT 1 FROM discovery_memberships d WHERE d.entry_id=OLD.id AND d.recording_id<>NEW.recording_id) BEGIN SELECT RAISE(ABORT,'Replace discovery representative before reassociation'); END",
)


def _archive(conn,table,row):
    conn.execute('INSERT OR IGNORE INTO identity_state_archive VALUES (?,?,?,?)',
                 ('discovery_repair_v28',json.dumps([table,row['recording_id'],row['source_token'] if table=='discovery_sources' else None]),
                  json.dumps(dict(row)),'retained before repairing discovery recording/representative consistency'))


def _merge_member(conn,target,row):
    conn.execute(
        'INSERT INTO discovery_memberships VALUES (?,?,?,?,?,?) ON CONFLICT(recording_id) DO UPDATE SET '
        'found_at=MIN(COALESCE(found_at,excluded.found_at),COALESCE(excluded.found_at,found_at)), '
        'offered_count=offered_count+excluded.offered_count, '
        'first_shown=MIN(COALESCE(first_shown,excluded.first_shown),COALESCE(excluded.first_shown,first_shown)), '
        'last_shown=MAX(COALESCE(last_shown,excluded.last_shown),COALESCE(excluded.last_shown,last_shown))',
        (target,row['entry_id'],row['found_at'],row['first_shown'],row['last_shown'],row['offered_count']))


def apply(conn,context):
    members = {r[0] for r in conn.execute('SELECT recording_id FROM discovery_memberships')}
    moved = {}
    rows = conn.execute('SELECT d.* FROM discovery_memberships d JOIN catalog_entries e ON e.id=d.entry_id '
                        'WHERE d.recording_id<>e.recording_id ORDER BY d.recording_id').fetchall()
    # First repair representatives where the original recording still has entries.
    # Only then merge memberships for recordings with no remaining entries.
    for row in rows:
        _archive(conn,'discovery_memberships',row)
        entry = conn.execute('SELECT MIN(id) FROM catalog_entries WHERE recording_id=?',(row['recording_id'],)).fetchone()[0]
        if entry is not None:
            conn.execute('UPDATE discovery_memberships SET entry_id=? WHERE recording_id=?',(entry,row['recording_id']))
        else:
            moved[row['recording_id']] = conn.execute('SELECT recording_id FROM catalog_entries WHERE id=?',(row['entry_id'],)).fetchone()[0]
    for previous,target in moved.items():
        row = conn.execute('SELECT * FROM discovery_memberships WHERE recording_id=?',(previous,)).fetchone()
        existing = conn.execute('SELECT * FROM discovery_memberships WHERE recording_id=?',(target,)).fetchone()
        if existing:
            _archive(conn,'discovery_memberships',existing)
        _merge_member(conn,target,row)
        conn.execute('DELETE FROM discovery_memberships WHERE recording_id=?',(previous,))
    source_moves = []
    for row in conn.execute('SELECT * FROM discovery_sources ORDER BY recording_id,source_token').fetchall():
        item = conn.execute('SELECT recording_id FROM youtube_items WHERE video_id=?',(row['input_video_id'],)).fetchone()
        target = item[0] if item else moved.get(row['recording_id'],row['recording_id'])
        if target == row['recording_id']:
            continue
        _archive(conn,'discovery_sources',row)
        existing = conn.execute('SELECT * FROM discovery_sources WHERE recording_id=? AND source_token=?',
                                (target,row['source_token'])).fetchone()
        if existing:
            _archive(conn,'discovery_sources',existing)
        source_moves.append((row,target))
    # Remove all changed keys before inserting, so crossed source associations
    # cannot consume one another's original evidence during collision aggregation.
    for row,target in source_moves:
        conn.execute('DELETE FROM discovery_sources WHERE recording_id=? AND source_token=?',(row['recording_id'],row['source_token']))
    for row,target in source_moves:
        conn.execute(
            'INSERT INTO discovery_sources VALUES (?,?,?,?,?,?,?) ON CONFLICT(recording_id,source_token) DO UPDATE SET '
            'found_at=MIN(COALESCE(found_at,excluded.found_at),COALESCE(excluded.found_at,found_at)), '
            'last_found_at=MAX(COALESCE(last_found_at,excluded.last_found_at),COALESCE(excluded.last_found_at,last_found_at))',
            (target,row['source_token'],row['input_video_id'],row['found_at'],row['source_type'],row['source_id'],row['last_found_at']))
        if row['recording_id'] in members:
            entry = conn.execute('SELECT MIN(id) FROM catalog_entries WHERE recording_id=?',(target,)).fetchone()[0]
            conn.execute('INSERT INTO discovery_memberships(recording_id,entry_id,found_at) VALUES (?,?,?) '
                         'ON CONFLICT(recording_id) DO NOTHING',(target,entry,row['found_at']))
    for sql in SQL:
        conn.execute(sql)
    validate(conn)


def validate(conn):
    for sql in SQL:
        name = sql.split()[2]
        row = conn.execute('SELECT sql FROM sqlite_master WHERE name=?',(name,)).fetchone()
        if row is None or row[0]!=sql:
            raise MigrationError(f'Incompatible discovery representative structure: {name}')
    if conn.execute('SELECT 1 FROM discovery_memberships d JOIN catalog_entries e ON e.id=d.entry_id '
                    'WHERE d.recording_id<>e.recording_id LIMIT 1').fetchone():
        raise MigrationError('Discovery representative belongs to another recording')
    if conn.execute('PRAGMA foreign_key_check').fetchone():
        raise MigrationError('Invalid discovery representative reference')
