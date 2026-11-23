"""SQL checkpoint owners, queue references and revision admission for Radio."""
import json

from yt_playlist.core import playlist_scopes, radio_checkpoint_policy, radio_policy_roots, radio_provenance, radio_storage
from yt_playlist.core.db_revisions import setting_reads
from yt_playlist.repos.base import Repo, lookup_read, snapshot_read, synchronized
from yt_playlist.repos import collection_file_writes


class RadioCheckpointRepo(Repo):
    @lookup_read
    def first_sounds(self):
        row = self.conn.execute('SELECT first_sounds FROM radio_sessions WHERE first_sounds IS NOT NULL').fetchone()
        return json.loads(row[0]) if row else None

    @synchronized
    def save_first_sounds(self, value, *, new=False):
        episode = value['episode_id']
        encoded = json.dumps(value, sort_keys=True)
        with self.conn.atomic():
            if new:
                self.conn.execute('DELETE FROM radio_sessions WHERE first_sounds IS NOT NULL AND id<>?', (episode,))
                self.conn.execute("INSERT INTO radio_sessions(id,status,first_sounds) VALUES (?,'reserved',?)",
                                  (episode, encoded))
            else:
                changed = self.conn.execute('UPDATE radio_sessions SET first_sounds=? WHERE id=? AND first_sounds IS NOT NULL',
                                            (encoded, episode)).rowcount
                if changed != 1:
                    raise ValueError('First sounds checkpoint no longer owns playback')

    @lookup_read
    def launch_receipts(self):
        return dict(self.conn.execute('SELECT launch.value,s.id FROM radio_sessions s,json_each(s.launch_ids) launch'))

    def _record_launch(self, launch_id, episode):
        return self.conn.execute("""
            INSERT INTO radio_sessions(id,status,launch_ids)
            SELECT ?,'reserved',json_array(?) WHERE NOT EXISTS (
                SELECT 1 FROM radio_sessions s,json_each(s.launch_ids) launch WHERE launch.value=?
            )
            ON CONFLICT(id) DO UPDATE SET launch_ids=json_insert(launch_ids,'$[#]',?)
        """, (episode, launch_id, launch_id, launch_id)).rowcount == 1

    @synchronized
    def reserve_launch(self, launch_id, episode, *, predecessor=None, clear_seeded=False, seeded_recovery=None):
        """Record ownership before playlist creation."""
        with self.conn.atomic():
            if predecessor is not None:
                self._record_launch(*predecessor)
            accepted = launch_id is None or self._record_launch(launch_id, episode)
            if accepted and (seeded_recovery is not None or clear_seeded):
                self.conn.execute('UPDATE radio_sessions SET seeded_recovery=NULL WHERE seeded_recovery IS NOT NULL')
                if seeded_recovery is not None:
                    changed = self.conn.execute('UPDATE radio_sessions SET seeded_recovery=? WHERE id=?',
                                                (json.dumps(seeded_recovery), episode)).rowcount
                    if changed != 1:
                        raise ValueError('Seeded recovery requires a launch reservation')
            return accepted

    @lookup_read
    def pending_seeded_launch(self):
        row = self.conn.execute('SELECT seeded_recovery FROM radio_sessions WHERE seeded_recovery IS NOT NULL').fetchone()
        return json.loads(row[0]) if row else None

    @synchronized
    def update_seeded_dispatch(self, launch_id, playlist, *, started, was_started=False):
        """Change stored dispatch ownership only while this launch still owns recovery."""
        with self.conn.atomic():
            return self.conn.execute(
                "UPDATE radio_sessions SET seeded_recovery=json_set(seeded_recovery, '$.playlist', ?, '$.dispatch_started', ?) "
                "WHERE seeded_recovery IS NOT NULL AND json_extract(seeded_recovery, '$.launch_id')=? "
                "AND coalesce(json_extract(seeded_recovery, '$.dispatch_started'), 0)=? "
                "AND coalesce(json_extract(seeded_recovery, '$.playlist'), ?)=?",
                (playlist, int(started), launch_id, int(was_started),
                 playlist, playlist)).rowcount == 1

    @synchronized
    def take_seeded_launch(self, launch_id):
        with self.conn.atomic():
            return self.conn.execute(
                "UPDATE radio_sessions SET seeded_recovery=NULL WHERE seeded_recovery IS NOT NULL "
                "AND json_extract(seeded_recovery,'$.launch_id')=?", (launch_id,)).rowcount == 1

    @synchronized
    def reserve_file(self, owner, integrity):
        with self.conn.atomic(join=True):
            return collection_file_writes.reserve(self.conn, 'radio-session', owner, integrity)

    @snapshot_read
    def checkpoint_facts(self):
        header = self.conn.execute('SELECT s.*,p.id AS local_playlist_id FROM radio_sessions s LEFT JOIN active_playlists p '
            'ON p.account_id=s.account_id AND p.playlist_scope_id=s.playlist_scope_id WHERE s.selected=1').fetchone()
        queue = [dict(row) for row in self.conn.execute('SELECT q.*,r.identity_key FROM radio_queue_items q '
            'JOIN recordings r ON r.id=q.recording_id WHERE q.session_id=? ORDER BY q.position', (header['id'],))] if header else []
        return {'header': dict(header) if header else None, 'queue': queue}

    @snapshot_read
    def save_facts(self, owner, account):
        head = self.conn.execute('SELECT id,revision,recovery_filename,recovery_digest,recovery_bytes '
                                 'FROM radio_sessions WHERE selected=1').fetchone()
        existing = self.conn.execute('SELECT account_id,revision FROM radio_sessions WHERE id=? AND revision IS NOT NULL',
                                     (owner,)).fetchone()
        account_exists = self.conn.execute('SELECT 1 FROM accounts WHERE id=?', (account,)).fetchone() is not None
        return {'head': dict(head) if head else None, 'existing': dict(existing) if existing else None,
                'account_exists': account_exists}

    @snapshot_read
    def queue_entries(self, videos):
        videos, result = list(dict.fromkeys(videos)), {}
        for offset in range(0, len(videos), 500):
            batch = videos[offset:offset + 500]
            rows = self.conn.execute('SELECT video_id,id,recording_id FROM track_sources WHERE video_id IN ('+
                                    ','.join('?' for _ in batch)+') ORDER BY id', batch)
            for row in rows:
                result.setdefault(row['video_id'], (row['recording_id'], row['id']))
        return result

    @synchronized
    def launch_entries(self, videos):
        result = {}
        for offset in range(0, len(videos), 500):
            batch = videos[offset:offset + 500]
            rows = self.conn.execute(
                'SELECT e.video_id,e.id,e.recording_id,r.identity_key FROM track_sources e '
                'JOIN recordings r ON r.id=e.recording_id WHERE e.video_id IN ('
                + ','.join('?' for _ in batch) + ') ORDER BY e.id', batch)
            for row in rows:
                result.setdefault(row['video_id'], (row['recording_id'], row['id'], row['identity_key']))
        return result

    @synchronized
    def publish(self, state, phase, revision, reference, queue, operation, versions, *, file_id, expected, now,
                new_scope=None):
        owner, account = state['episode_id'], state['identity']
        with self.conn.atomic(join=new_scope is not None):
            head = self.conn.execute(radio_storage.SELECTED_REVISION).fetchone()
            if (tuple(head) if head else None) != expected:
                raise ValueError('Radio session changed in another worker; reload its checkpoint')
            collection_file_writes.complete(self.conn, file_id, 'radio-session', owner, reference)
            scope = new_scope
            if scope is None and state.get('playlist'):
                scope = playlist_scopes.ensure(self.conn, state['playlist'], account)
            operation_json = radio_checkpoint_policy.encode_operation(operation, revision)
            radio_storage.write_checkpoint(self.conn, owner, account, scope, phase, revision, reference, now,
                                           operation_json, observed=head[0] if head else None)
            self._publish_queue(owner, queue, new_session=new_scope is not None)
            radio_policy_roots.save_session(self.conn, owner, versions)
            if head is None or head[0] != owner:
                radio_storage.select_session(self.conn, owner)
        return owner, revision

    def _publish_queue(self, owner, queue, *, new_session):
        if not new_session:
            current = [tuple(row) for row in self.conn.execute(
                'SELECT position,recording_id,entry_id,video_id,state FROM radio_queue_items '
                'WHERE session_id=? ORDER BY position', (owner,))]
            if current == queue:
                return
            self.conn.execute('DELETE FROM radio_queue_items WHERE session_id=?', (owner,))
        for offset in range(0, len(queue), 150):
            batch = queue[offset:offset + 150]
            self.conn.execute('INSERT INTO radio_queue_items VALUES ' + ','.join('(?,?,?,?,?,?)' for _ in batch),
                              [value for row in batch for value in (owner, *row)])

    @synchronized
    def remember_scope(self, scope):
        radio_provenance.mark_scope(self.conn, scope)

    @snapshot_read
    def playlist_ids(self):
        with setting_reads(self._store, (radio_provenance.UNRESOLVED,)):
            return radio_provenance.ids(self.conn)

    @synchronized
    def remember_playlists(self, playlists, account=None):
        with self.conn.atomic(join=True):
            for playlist in playlists:
                radio_provenance.remember(self.conn, playlist, account)
