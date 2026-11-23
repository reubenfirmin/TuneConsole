"""Recording aliases and their stored references, without document I/O."""
from yt_playlist.core import album_tracklists, legacy_mode_payloads, mood, recommendation_impressions
from yt_playlist.core.catalog_keys import resolve_recording
from yt_playlist.repos.base import Repo, synchronized, snapshot_read, lookup_read


class RecordingKeysRepo(Repo):
    @synchronized
    def add_alias(self, alias, recording, source):
        with self.conn.atomic(join=True):
            self.conn.execute('INSERT OR IGNORE INTO recording_aliases VALUES (?,?,?)', (alias, recording, source))
        return [dict(row) for row in self.conn.execute('SELECT r.id,r.identity_key,r.artist FROM recording_aliases a '
            'JOIN recordings r ON r.id=a.recording_id WHERE a.alias=?', (alias,))]

    @snapshot_read
    def rekey_inputs(self, recording):
        return {'feedback': [dict(row) for row in self.conn.execute(
            'SELECT rowid,* FROM rec_feedback WHERE recording_id=?', (recording,))],
            'modes': [dict(row) for row in self.conn.execute(f'SELECT mode_id,rep_keys FROM {legacy_mode_payloads.VIEW}')]}

    @synchronized
    def apply_rekey(self, old_key, new_key, recording, feedback, modes, *, previous=None, association=None):
        subject = recording if previous is None else previous
        with self.conn.atomic(join=True):
            album_tracklists.reassociate(self.conn, subject, recording)
            self.conn.execute('UPDATE rec_served_impressions SET identity_key=?,recording_id=? WHERE recording_id=?',
                              (new_key, recording, subject))
            for table in ('play_events', 'listening_observations'):
                self.conn.execute(f'UPDATE main.{table} SET recording_id=?,unresolved_subject_key=NULL '
                                  'WHERE recording_id=?', (recording, subject))
            if previous is not None:
                self.conn.execute('DELETE FROM rec_feedback WHERE recording_id=?', (previous,))
                mood.move_recording(self.conn, previous, recording)
            for row in feedback:
                self.conn.execute('DELETE FROM rec_feedback WHERE rowid=?', (row['rowid'],))
                self.conn.execute('INSERT INTO rec_feedback(surface,item_key,kind,reason,scope,until,created_at,'
                    'target_type,recording_id) VALUES (?,?,?,?,?,?,?,?,?) ON CONFLICT(surface,item_key,scope) '
                    'DO UPDATE SET kind=excluded.kind,reason=excluded.reason,until=excluded.until,'
                    'created_at=excluded.created_at,target_type=excluded.target_type,recording_id=excluded.recording_id '
                    'WHERE excluded.created_at>=COALESCE(rec_feedback.created_at,0)',
                    (row['surface'], new_key, row['kind'], row['reason'], row['scope'], row['until'],
                     row['created_at'], row['target_type'], recording))
            recommendation_impressions.rekey(self.conn, old_key, new_key)
            self.conn.executemany('UPDATE taste_groups SET mode_rep_keys=? WHERE legacy_mode_id=?', modes)
            if previous is None:
                self.conn.execute('UPDATE recordings SET identity_key=? WHERE id=?', (new_key, recording))
            from yt_playlist.core.association_work import complete
            complete(self.conn, association, 'keys')

    @lookup_read
    def key_candidates(self, key):
        direct = self.conn.execute('SELECT id FROM recordings WHERE identity_key=?', (key,)).fetchone()
        return {'direct': resolve_recording(self.conn, direct[0]) if direct else None,
                'aliases': {resolve_recording(self.conn, row[0]) for row in self.conn.execute(
                    'SELECT recording_id FROM recording_aliases WHERE alias=?', (key,))}}

    @synchronized
    def resolve_mood_seeds(self, key, recording, reason):
        with self.conn.atomic(join=True):
            mood.resolve_key(self.conn, key, recording, reason)

    @lookup_read
    def listening_keys(self, recording):
        return [row[0] for row in self.conn.execute('SELECT alias FROM recording_aliases WHERE recording_id=? '
            'UNION SELECT identity_key FROM recordings WHERE id=?', (recording, recording))]

    @synchronized
    def resolve_listening_keys(self, recording, keys):
        changed = False
        with self.conn.atomic(join=True):
            for key in keys:
                for table in ('play_events', 'listening_observations'):
                    exact = " AND NULLIF(trim(video_id),'') IS NULL" if table == 'play_events' else ''
                    result = self.conn.execute(f'UPDATE main.{table} SET recording_id=?,unresolved_subject_key=NULL '
                        f'WHERE recording_id IS NULL AND raw_identity_key=?{exact}', (recording, key))
                    changed = changed or result.rowcount > 0
        return changed

    @synchronized
    def invalidate_catalog(self):
        from yt_playlist.core import recommendation_invalidation
        with self.conn.atomic(join=True):
            recommendation_invalidation.catalog_changed(self.conn)
