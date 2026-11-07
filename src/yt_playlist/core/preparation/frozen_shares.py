"""Guard future frozen choices independently of auditing installed membership."""
from dataclasses import asdict, dataclass
import hashlib
import json
from pathlib import Path
import sqlite3

from ..evidence_ledger import canonical
from ..schema_contract import MigrationError
from ..frozen_share_contract import fences
from .rows import Job


MAX_ROW_BYTES = 128 * 1024
ORIGINALS = 'prepared_frozen_share_originals'
SUMMARIES = 'prepared_frozen_share_summaries'
MEMBERS = 'prepared_frozen_share_members'


@dataclass(frozen=True)
class ShareJob(Job):
    def definitions(self, conn=None):
        yield from super().definitions(conn)
        for sql in fences():
            name = 'prepare_frozen_share_membership_insert'
            yield name, sql.replace('CREATE TRIGGER album_share_membership_insert ', 'CREATE TRIGGER ' + name + ' ', 1)


def clipped(column):
    return f'CAST(substr(CAST({column} AS BLOB),1,{MAX_ROW_BYTES+1}) AS TEXT) AS {column}'


def copy(conn, owner, key, row):
    raw = canonical(row)
    if len(raw) > MAX_ROW_BYTES:
        raise ValueError('Frozen share row exceeds its preparation byte budget')
    digest = hashlib.sha256(raw).hexdigest()
    conn.execute(f'INSERT INTO {ORIGINALS} VALUES (?,?,?,?) ON CONFLICT(owner,source_key,digest) DO NOTHING',
                 (owner,key,digest,raw.decode()))
    if owner == 'album_share_snapshots':
        capture = conn.execute('SELECT status,order_status,expected_count FROM main.album_tracklists WHERE id=?',
                               (row['tracklist_id'],)).fetchone()
        valid = capture is not None and capture['status'] in ('complete','snapshot') and capture['order_status'] == 'known'
        conn.execute(f'INSERT INTO {SUMMARIES}(snapshot_id,source_digest,tracklist_id,expected_count,capture_valid) VALUES (?,?,?,?,?) '
            'ON CONFLICT(snapshot_id) DO UPDATE SET source_digest=excluded.source_digest,tracklist_id=excluded.tracklist_id,'
            'expected_count=excluded.expected_count,capture_valid=excluded.capture_valid',
            (row['id'],digest,row['tracklist_id'],capture['expected_count'] if capture else None,int(valid)))
        return
    valid = conn.execute('SELECT 1 FROM main.album_share_snapshots s '
        'JOIN main.album_tracklists t ON t.id=s.tracklist_id '
        'JOIN main.album_track_occurrences o ON o.tracklist_id=t.id AND o.id=? '
        "WHERE s.id=? AND t.id=? AND t.status IN ('complete','snapshot') AND t.order_status='known' "
        'AND ?>=0 AND ?<t.expected_count AND o.position=?',
        (row['occurrence_id'],row['snapshot_id'],row['tracklist_id'],row['position'],row['position'],row['position'])).fetchone()
    existing = conn.execute(f'SELECT 1 FROM {MEMBERS} WHERE snapshot_id=? AND position=?',
                            (row['snapshot_id'],row['position'])).fetchone()
    conn.execute(f'INSERT INTO {MEMBERS} VALUES (?,?,?,?,?,?) ON CONFLICT(snapshot_id,position) DO UPDATE SET '
        'source_digest=excluded.source_digest,tracklist_id=excluded.tracklist_id,occurrence_id=excluded.occurrence_id,invalid=excluded.invalid',
        (row['snapshot_id'],row['position'],digest,row['tracklist_id'],row['occurrence_id'],int(valid is None)))
    if existing is None:
        conn.execute(f'INSERT INTO {SUMMARIES}(snapshot_id,member_count) VALUES (?,1) '
                     'ON CONFLICT(snapshot_id) DO UPDATE SET member_count=member_count+1',(row['snapshot_id'],))


def delete(conn, owner, key):
    identity = json.loads(key)
    if owner == 'album_share_snapshots':
        conn.execute(f'UPDATE {SUMMARIES} SET source_digest=NULL,tracklist_id=NULL,expected_count=NULL,capture_valid=0 WHERE snapshot_id=?',identity)
    else:
        removed = conn.execute(f'DELETE FROM {MEMBERS} WHERE snapshot_id=? AND position=?',identity).rowcount
        if removed:
            conn.execute(f'UPDATE {SUMMARIES} SET member_count=member_count-1 WHERE snapshot_id=?',(identity[0],))


def original(conn, owner, key, digest):
    row = conn.execute(f'SELECT snapshot FROM {ORIGINALS} WHERE owner=? AND source_key=? AND digest=?',(owner,key,digest)).fetchone()
    if row is None or hashlib.sha256(row[0].encode()).hexdigest() != digest:
        raise MigrationError('Frozen share original differs from its captured bytes')
    return json.loads(row[0])


DESTINATIONS = (
    (ORIGINALS, f'''CREATE TABLE {ORIGINALS} (
        owner TEXT NOT NULL CHECK(owner IN ('album_share_snapshots','album_share_items')),source_key TEXT NOT NULL CHECK(json_valid(source_key)),
        digest TEXT NOT NULL CHECK(length(digest)=64),snapshot TEXT NOT NULL CHECK(json_valid(snapshot)),
        PRIMARY KEY(owner,source_key,digest)) WITHOUT ROWID'''),
    (SUMMARIES, f'''CREATE TABLE {SUMMARIES} (
        snapshot_id INTEGER PRIMARY KEY,source_digest TEXT CHECK(source_digest IS NULL OR length(source_digest)=64),
        tracklist_id INTEGER,expected_count INTEGER,capture_valid INTEGER NOT NULL DEFAULT 0 CHECK(capture_valid IN (0,1)),
        member_count INTEGER NOT NULL DEFAULT 0 CHECK(member_count>=0),
        blocked INTEGER GENERATED ALWAYS AS
            (source_digest IS NULL OR capture_valid=0 OR expected_count IS NULL OR member_count<>expected_count) STORED)'''),
    (SUMMARIES+'_blocked',f'CREATE INDEX {SUMMARIES}_blocked ON {SUMMARIES}(snapshot_id) WHERE blocked=1'),
    (MEMBERS, f'''CREATE TABLE {MEMBERS} (
        snapshot_id INTEGER NOT NULL,position INTEGER NOT NULL,source_digest TEXT NOT NULL CHECK(length(source_digest)=64),
        tracklist_id INTEGER NOT NULL,occurrence_id INTEGER NOT NULL,invalid INTEGER NOT NULL CHECK(invalid IN (0,1)),
        PRIMARY KEY(snapshot_id,position)) WITHOUT ROWID'''),
    (MEMBERS+'_invalid',f'CREATE INDEX {MEMBERS}_invalid ON {MEMBERS}(snapshot_id,position) WHERE invalid=1'),
)
JOB = ShareJob('frozen_shares',99,107,
    {'album_share_snapshots':('id',),'album_share_items':('snapshot_id','position')},DESTINATIONS,copy,delete,
    dependencies=('album_tracklists','album_track_occurrences'),batch_limit=32,refresh_highwater=False,
    projections={'album_share_snapshots':('id','album_id','tracklist_id',clipped('captured_provider_id'),clipped('title'),'created_at'),
                 'album_share_items':('snapshot_id','position','tracklist_id','occurrence_id','entry_id',clipped('raw_source_ref'))})


def verify(conn):
    """Use reconciled counters and indexed blockers, without a historical scan."""
    if conn.in_transaction:
        raise MigrationError('Frozen share verification requires an idle connection')
    try:
        conn.execute('BEGIN')
        JOB._version(conn)
        JOB.validate(conn)
        progress = JOB.progress(conn)
        if (progress is None or progress.status != 'prepared' or progress.prepared_revision != progress.input_revision
                or conn.execute(f'SELECT 1 FROM {JOB.changes} LIMIT 1').fetchone()):
            raise MigrationError('Frozen shares require their reconciled preparation revision')
        blocked = conn.execute(f'SELECT snapshot_id FROM {SUMMARIES} WHERE blocked=1 ORDER BY snapshot_id LIMIT 1').fetchone()
        invalid = conn.execute(f'SELECT snapshot_id,position FROM {MEMBERS} WHERE invalid=1 ORDER BY snapshot_id,position LIMIT 1').fetchone()
        if blocked or invalid:
            identity = blocked[0] if blocked else invalid[0]
            raise MigrationError(f'Frozen album share {identity} has changed membership; original rows retained')
        return progress
    finally:
        conn.rollback()


def main(argv=None):
    import argparse
    from contextlib import closing
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('database',type=Path)
    parser.add_argument('--status',action='store_true')
    parser.add_argument('--verify',action='store_true')
    parser.add_argument('--batches',type=int,default=1)
    parser.add_argument('--batch-size',type=int,default=32)
    parser.add_argument('--retry',action='store_true')
    args = parser.parse_args(argv)
    if not 1 <= args.batches <= 512 or not 1 <= args.batch_size <= 512:
        parser.error('--batches and --batch-size must be from 1 through 512')
    try:
        mode = 'ro' if args.status or args.verify else 'rw'
        from ..db_handles import connect
        with closing(connect(args.database.resolve().as_uri()+'?mode='+mode,uri=True,timeout=.1)) as conn:
            conn.row_factory = sqlite3.Row
            conn.execute('PRAGMA foreign_keys=ON')
            if args.verify:
                result = verify(conn)
            elif args.status:
                result = JOB.progress(conn) if conn.execute('SELECT 1 FROM main.sqlite_schema WHERE name=?',(JOB.state,)).fetchone() else None
            else:
                result = JOB.install(conn)
                for _ in range(args.batches):
                    result = JOB.step(conn,batch_size=args.batch_size,retry=args.retry)
                    if result.status == 'prepared':
                        break
            print(json.dumps(asdict(result) if result else {'status':'uninitialized'},separators=(',',':')))
    except (MigrationError,sqlite3.Error,ValueError) as exc:
        parser.exit(1,f'Frozen share preparation failed: {exc}\n')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
