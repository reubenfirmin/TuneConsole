"""Bounded collection of disposable superseded predictions, never originals."""
from yt_playlist.core import file_collection
import json

from . import fao, vibe_provenance
from .db_writes import atomic_write
from .evidence_ledger import canonical

RETAIN_SECONDS = 180 * 86400


def maintain(store, now, limit=128):
    if not 1 <= limit <= 256:
        raise ValueError('Invalid vibe history collection budget')
    with store._lock, file_collection.collector(vibe_provenance.directory(store.conn)) as admitted:
        if not admitted:
            return 0
        cursor = vibe_provenance.directory(store.conn)/'.history-collection.json'
        after = json.loads(fao.read(cursor).bytes)['after'] if cursor.exists() else 0
        removed = 0
        with atomic_write(store, wait_seconds=0):
            rows = store.conn.execute('SELECT id,recording_id,vibe,predicted_at FROM predictions '
                'WHERE id>? ORDER BY id LIMIT ?', (after, limit)).fetchall()
            for pid, recording, vibe, at in rows:
                if at is None or at >= now-RETAIN_SECONDS:
                    continue
                if store.conn.execute('SELECT 1 FROM recording_vibes WHERE recording_id=? AND vibe=? '
                                      'AND prediction_id=?', (recording, vibe, pid)).fetchone():
                    continue
                removed += store.conn.execute('DELETE FROM predictions WHERE id=?', (pid,)).rowcount
        # A crash before saving this bookmark safely repeats the committed page.
        fao.write(cursor, canonical({'after': rows[-1][0] if len(rows) == limit else 0}))
        return removed
