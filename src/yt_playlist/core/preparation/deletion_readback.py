"""Prepare readback decisions for retained v98–106 imported deletion journals.

Runtime admission already treats ambiguous imported pending steps as unknown.
This job records the exact installed evidence and decision in bounded batches;
it never replays deletion, rewrites the active ledger or stamps version 107.
"""
import hashlib
import json

from .. import action_work
from .rows import Job, main as run


def copy(conn, owner, key, row):
    action = conn.execute('SELECT id,idempotency_key,status FROM main.actions WHERE id=?',
                          (row['action_id'],)).fetchone()
    if action is None:
        raise ValueError('Deletion step requires its recorded action')
    snapshot = json.dumps({'action': dict(action), 'step': row}, ensure_ascii=False,
                          separators=(',', ':'), sort_keys=True, allow_nan=False)
    digest = hashlib.sha256(snapshot.encode('utf-8')).hexdigest()
    conn.execute('INSERT INTO prepared_deletion_originals VALUES (?,?,?) '
                 'ON CONFLICT(step_id,digest) DO NOTHING', (row['id'], digest, snapshot))
    required = (action['status'] not in ('undone', 'cancelled')
                and (row['outcome'] == 'unknown' or action_work.imported_pending(action, row)))
    conn.execute('INSERT INTO prepared_deletion_readback VALUES (?,?,?,?,?) '
                 'ON CONFLICT(step_id) DO UPDATE SET action_id=excluded.action_id,'
                 'source_digest=excluded.source_digest,requires_readback=excluded.requires_readback,'
                 'effective_phase=excluded.effective_phase',
                 (row['id'], row['action_id'], digest, int(required),
                  'uncertain' if action_work.imported_pending(action, row) else row['phase']))


def delete(conn, owner, key):
    conn.execute('DELETE FROM prepared_deletion_readback WHERE step_id=?', json.loads(key))


JOB = Job('deletion_readback', 98, 106, {'action_steps': ('id',)}, (
    ('prepared_deletion_originals', '''CREATE TABLE prepared_deletion_originals (
        step_id INTEGER NOT NULL, digest TEXT NOT NULL CHECK(length(digest)=64),
        source_snapshot TEXT NOT NULL CHECK(json_valid(source_snapshot)),
        PRIMARY KEY(step_id,digest)) WITHOUT ROWID'''),
    ('prepared_deletion_readback', '''CREATE TABLE prepared_deletion_readback (
        step_id INTEGER PRIMARY KEY, action_id INTEGER NOT NULL,
        source_digest TEXT NOT NULL, requires_readback INTEGER NOT NULL CHECK(requires_readback IN (0,1)),
        effective_phase TEXT NOT NULL,
        FOREIGN KEY(step_id,source_digest) REFERENCES prepared_deletion_originals(step_id,digest))'''),
), copy, delete, dependencies=('actions',),
    filters={'action_steps': "{prefix}operation='delete_playlist'"})


def main(argv=None):
    return run(JOB, argv)


if __name__ == '__main__':
    raise SystemExit(main())
