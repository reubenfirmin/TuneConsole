"""Recording aliases and their stored references, without document I/O."""
from yt_playlist.core import album_tracklists, listening_summaries
from yt_playlist.core.catalog_keys import resolve_recording
from yt_playlist.repos.base import Repo, synchronized, snapshot_read


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
            'modes': [dict(row) for row in self.conn.execute('SELECT mode_id,rep_keys FROM taste_modes')]}

    @synchronized
    def apply_rekey(self, old_key, new_key, recording, feedback, modes, *, previous=None):
        subject = recording if previous is None else previous
        with self.conn.atomic(join=True):
            if album_tracklists.available(self.conn):
                album_tracklists.reassociate(self.conn, subject, recording)
            else:
                self.conn.execute('UPDATE album_tracks SET recording_id=? WHERE entry_id IS NULL AND recording_id=?',
                                  (recording, subject))
            for table in ('rec_served_impressions', 'rec_lane_impressions'):
                self.conn.execute(f'UPDATE {table} SET identity_key=?,recording_id=? WHERE recording_id=?', (new_key, recording, subject))
            typed = listening_summaries.available(self.conn)
            for table in ('play_events', 'listening_observations'):
                assignment = 'unresolved_subject_key=NULL' if typed else 'identity_key=?'
                parameters = (recording, subject) if typed else (recording, new_key, subject)
                self.conn.execute(f'UPDATE main.{table} SET recording_id=?,{assignment} WHERE recording_id=?', parameters)
            if previous is not None:
                self.conn.execute('DELETE FROM rec_feedback WHERE recording_id=?', (previous,))
                if not self.conn.execute("SELECT 1 FROM main.sqlite_schema WHERE type='table' AND name='rec_mood'").fetchone():
                    self.conn.execute('INSERT INTO mood_event_recordings '
                        'SELECT mood_event_id,?,position FROM mood_event_recordings WHERE recording_id=? '
                        'ON CONFLICT(mood_event_id,recording_id) DO UPDATE SET position=min(position,excluded.position)',
                        (recording, previous))
                    self.conn.execute('DELETE FROM mood_event_recordings WHERE recording_id=?', (previous,))
            for row in feedback:
                self.conn.execute('DELETE FROM rec_feedback WHERE rowid=?', (row['rowid'],))
                self.conn.execute('INSERT INTO rec_feedback(surface,item_key,kind,reason,scope,until,created_at,'
                    'target_type,recording_id) VALUES (?,?,?,?,?,?,?,?,?) ON CONFLICT(surface,item_key,scope) '
                    'DO UPDATE SET kind=excluded.kind,reason=excluded.reason,until=excluded.until,'
                    'created_at=excluded.created_at,target_type=excluded.target_type,recording_id=excluded.recording_id '
                    'WHERE excluded.created_at>=COALESCE(rec_feedback.created_at,0)',
                    (row['surface'], new_key, row['kind'], row['reason'], row['scope'], row['until'],
                     row['created_at'], row['target_type'], recording))
            self.conn.execute('INSERT INTO rec_impressions SELECT surface,?,views,last_shown FROM rec_impressions '
                'WHERE item_key=? ON CONFLICT(surface,item_key) DO UPDATE SET views=views+excluded.views,'
                'last_shown=MAX(COALESCE(last_shown,0),COALESCE(excluded.last_shown,0))', (new_key, old_key))
            self.conn.execute('DELETE FROM rec_impressions WHERE item_key=?', (old_key,))
            self.conn.executemany('UPDATE taste_modes SET rep_keys=? WHERE mode_id=?', modes)
            if previous is None:
                self.conn.execute('UPDATE recordings SET identity_key=? WHERE id=?', (new_key, recording))

    @snapshot_read
    def key_candidates(self, key):
        direct = self.conn.execute('SELECT id FROM recordings WHERE identity_key=?', (key,)).fetchone()
        return {'direct': resolve_recording(self.conn, direct[0]) if direct else None,
                'aliases': {resolve_recording(self.conn, row[0]) for row in self.conn.execute(
                    'SELECT recording_id FROM recording_aliases WHERE alias=?', (key,))}}

    @synchronized
    def resolve_mood_seeds(self, key, recording, reason):
        if self.conn.execute("SELECT 1 FROM main.sqlite_schema WHERE type='table' AND name='rec_mood'").fetchone():
            return
        with self.conn.atomic(join=True):
            if recording is None:
                self.conn.execute('UPDATE mood_event_unresolved_seeds SET reason=? WHERE legacy_key=?', (reason, key))
            else:
                self.conn.execute('INSERT INTO mood_event_recordings '
                    'SELECT mood_event_id,?,position FROM mood_event_unresolved_seeds WHERE legacy_key=? '
                    'ON CONFLICT(mood_event_id,recording_id) DO UPDATE SET position=min(position,excluded.position)',
                    (recording, key))
                self.conn.execute('DELETE FROM mood_event_unresolved_seeds WHERE legacy_key=?', (key,))

    @snapshot_read
    def listening_keys(self, recording):
        if not listening_summaries.available(self.conn):
            return []
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
        from yt_playlist.core.catalog import invalidate_derived
        with self.conn.atomic(join=True):
            invalidate_derived(self.conn)
