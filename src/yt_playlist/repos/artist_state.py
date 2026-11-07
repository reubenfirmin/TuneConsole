"""Persist reconciled artist evidence in the identity repository's transaction."""
import json

from yt_playlist.core.recommendation_invalidation import artist_reconciled
from yt_playlist.core import legacy_evidence
from yt_playlist.repos.base import Repo, snapshot_read, synchronized


class ArtistStateRepo(Repo):
    @snapshot_read
    def reconciliation_inputs(self, previous, target):
        return {'caches': [dict(row) for row in self.conn.execute(
            'SELECT * FROM rec_artist_similar WHERE artist_id IN (?,?)', (previous, target))],
            'edges': [dict(row) for row in self.conn.execute(
                'SELECT * FROM artist_similarity_targets WHERE source_artist_id=? OR target_artist_id=?', (previous, previous))]}

    @synchronized
    def apply_reconciliation(self, previous, target, winner, edges):
        with self.conn.atomic(join=True):
            self.conn.execute('UPDATE rec_feedback SET artist_id=? WHERE artist_id=?', (target, previous))
            if winner is not None:
                self.conn.execute('DELETE FROM rec_artist_similar WHERE artist_id IN (?,?)', (previous, target))
                self.conn.execute('INSERT INTO rec_artist_similar (' + ','.join(winner) + ') VALUES (' +
                    ','.join('?' for _ in winner) + ')', tuple(winner.values()))
            self.conn.execute('DELETE FROM artist_similarity_targets WHERE source_artist_id=? OR target_artist_id=?',
                              (previous, previous))
            self.conn.executemany('INSERT INTO artist_similarity_targets VALUES (?,?,?) '
                'ON CONFLICT(source_artist_id,target_artist_id) DO UPDATE SET weight=MAX(weight,excluded.weight)', edges)
            artist_reconciled(self.conn)


def reconcile_artist(conn, previous, target, *, source):
    conn.execute('UPDATE rec_feedback SET artist_id=? WHERE artist_id=?',(target,previous))
    rows = [dict(r) for r in conn.execute(
        'SELECT * FROM rec_artist_similar WHERE artist_id IN (?,?)', (previous,target))]
    if rows:
        for row in rows:
            legacy_evidence.retain(conn, 'artist_reconcile:rec_artist_similar',
                 json.dumps([previous,target,row['artist']]),json.dumps(row),source)
        winner = max(rows,key=lambda r:(r['fetched_at'] if r['fetched_at'] is not None else float('-inf'),
                                       r['artist_id']==target,json.dumps(r,sort_keys=True)))
        winner.update(artist_id=target,artist=f'@artist:{target}')
        conn.execute('DELETE FROM rec_artist_similar WHERE artist_id IN (?,?)',(previous,target))
        columns = ','.join(winner)
        conn.execute(f'INSERT INTO rec_artist_similar ({columns}) VALUES ({",".join("?" for _ in winner)})',
                     tuple(winner.values()))
    edges = conn.execute('SELECT * FROM artist_similarity_targets WHERE source_artist_id=? OR target_artist_id=?',
                         (previous,previous)).fetchall()
    conn.execute('DELETE FROM artist_similarity_targets WHERE source_artist_id=? OR target_artist_id=?',
                 (previous,previous))
    for edge in edges:
        conn.execute('INSERT INTO artist_similarity_targets VALUES (?,?,?) '
            'ON CONFLICT(source_artist_id,target_artist_id) DO UPDATE SET weight=MAX(weight,excluded.weight)',
            (target if edge['source_artist_id']==previous else edge['source_artist_id'],
             target if edge['target_artist_id']==previous else edge['target_artist_id'],edge['weight']))
    artist_reconciled(conn)
