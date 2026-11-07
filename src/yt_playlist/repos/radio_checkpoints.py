"""SQL checkpoint owners, queue references and revision admission for Radio."""
from yt_playlist.core import append_reads, append_work, entity_names, playlist_scopes, radio_provenance, radio_reads, radio_storage
from yt_playlist.core.db_revisions import setting_reads
from yt_playlist.repos.base import Repo, snapshot_read, synchronized


class RadioCheckpointRepo(Repo):
    @snapshot_read
    def checkpoint_facts(self):
        if not radio_storage.available(self.conn):
            revision = radio_reads.session_selected(self.conn)
            header = self.conn.execute('SELECT * FROM prepared_radio_sessions WHERE generation=?',
                                       (revision,)).fetchone() if revision is not None else None
            return {'native': False, 'prepared': revision is not None, 'header': dict(header) if header else None}
        header = self.conn.execute('SELECT s.* FROM radio_session_head h JOIN radio_sessions s ON s.id=h.session_id').fetchone()
        queue = [dict(row) for row in self.conn.execute('SELECT q.*,r.identity_key FROM radio_queue_items q '
            'JOIN recordings r ON r.id=q.recording_id WHERE q.session_id=? ORDER BY q.position', (header['id'],))] if header else []
        return {'native': True, 'header': dict(header) if header else None, 'queue': queue}

    @snapshot_read
    def operation_facts(self, source, *, native):
        if append_work.available(self.conn):
            header = self.conn.execute('SELECT * FROM radio_operations WHERE session_id=?', (source,)).fetchone()
            if header is None:
                return None
            rows = self.conn.execute('SELECT * FROM radio_operation_items WHERE session_id=? '
                                     'ORDER BY role,list_position,position', (source,))
            roles = {}
            for row in rows:
                roles.setdefault((row['role'], row['list_position']), []).append(row['video_id_snapshot'])
            return {'native': True, 'header': dict(header), 'roles': roles}
        name = 'radio_native' if native else 'radio_legacy'
        header = append_reads.selected_operation(self.conn, name, source)
        if header is None:
            return None
        roles = {('allowed', index): append_reads.sequence(self.conn, name, header, 'allowed', index)
                 for index in range(header['allowed_count'])}
        roles.update(((role, 0), append_reads.sequence(self.conn, name, header, role))
                     for role in ('confirmed', 'waiting', 'play_prefix', 'excluded', 'proposed'))
        return {'native': False, 'header': dict(header), 'roles': roles}

    @snapshot_read
    def save_facts(self, owner, account):
        if not radio_storage.available(self.conn):
            return {'native': False}
        head = self.conn.execute('SELECT s.* FROM radio_session_head h JOIN radio_sessions s ON s.id=h.session_id').fetchone()
        existing = self.conn.execute('SELECT account_id,revision FROM radio_sessions WHERE id=?', (owner,)).fetchone()
        account_exists = self.conn.execute('SELECT 1 FROM '+entity_names.accounts(self.conn)+' WHERE id=?', (account,)).fetchone() is not None
        return {'native': True, 'head': dict(head) if head else None, 'existing': dict(existing) if existing else None,
                'account_exists': account_exists, 'operations': append_work.available(self.conn)}

    @snapshot_read
    def queue_entries(self, videos):
        videos, result = list(dict.fromkeys(videos)), {}
        for offset in range(0, len(videos), 500):
            batch = videos[offset:offset + 500]
            rows = self.conn.execute('SELECT video_id,id,recording_id FROM catalog_entries WHERE video_id IN ('+
                                    ','.join('?' for _ in batch)+') ORDER BY id', batch)
            for row in rows:
                result.setdefault(row['video_id'], (row['recording_id'], row['id']))
        return result

    @synchronized
    def publish(self, state, phase, revision, reference, queue, operation, versions, *, expected, now):
        owner, account = state['episode_id'], state['identity']
        with self.conn.atomic():
            head = self.conn.execute('SELECT s.id,s.revision FROM radio_session_head h '
                                     'JOIN radio_sessions s ON s.id=h.session_id').fetchone()
            if (tuple(head) if head else None) != expected:
                raise ValueError('Radio session changed in another worker; reload its checkpoint')
            scope = playlist_scopes.ensure(self.conn, state['playlist'], account) if state.get('playlist') else None
            self.conn.execute('INSERT INTO radio_sessions(id,account_id,playlist_scope_id,status,revision,recovery_filename,'
                'recovery_digest,recovery_bytes,updated_at) VALUES (?,?,?,?,?,?,?,?,?) '
                'ON CONFLICT(id) DO UPDATE SET playlist_scope_id=excluded.playlist_scope_id,status=excluded.status,'
                'revision=excluded.revision,recovery_filename=excluded.recovery_filename,'
                'recovery_digest=excluded.recovery_digest,recovery_bytes=excluded.recovery_bytes,updated_at=excluded.updated_at',
                (owner, account, scope, phase, revision, *reference, now))
            self.conn.execute('DELETE FROM radio_queue_items WHERE session_id=?', (owner,))
            self.conn.executemany('INSERT INTO radio_queue_items VALUES (?,?,?,?,?,?)', [(owner, *row) for row in queue])
            if self.conn.execute("SELECT 1 FROM sqlite_schema WHERE name='radio_session_policies'").fetchone():
                self.conn.execute('DELETE FROM radio_session_policies WHERE session_id=?', (owner,))
                for version in versions:
                    known = self.conn.execute('SELECT 1 FROM recommendation_policies WHERE version=?', (version,)).fetchone()
                    self.conn.execute('INSERT INTO radio_session_policies VALUES (?,?,?,?)',
                        (owner, version, version if known else None, None if known else 'missing_policy'))
            if append_work.available(self.conn):
                append_work.install_radio(self.conn, owner, revision, operation)
            self.conn.execute('INSERT INTO radio_session_head VALUES (1,?) '
                'ON CONFLICT(singleton) DO UPDATE SET session_id=excluded.session_id', (owner,))
        return owner, revision

    @snapshot_read
    def origin_facts(self):
        if radio_provenance.available(self.conn):
            scopes = entity_names.owner(self.conn, 'playlist_scopes')
            ids = [row[0] for row in self.conn.execute(
                f'SELECT DISTINCT COALESCE(s.{scopes.column("provider_playlist_id")},o.unresolved_provider_id) '
                f'FROM radio_playlist_origins o LEFT JOIN {scopes.table} s '
                f'ON s.{scopes.column("id")}=o.playlist_scope_id ORDER BY 1')]
            return {'native': True, 'ids': ids}
        revision = radio_reads.origin_selected(self.conn)
        if revision is not None:
            return {'native': False, 'ids': radio_reads.origin_ids(self.conn, revision), 'values': {}}
        with setting_reads(self._store, radio_provenance.KEYS):
            return {'native': False, 'ids': None, 'values': dict(self.conn.execute(
                'SELECT key,value FROM settings WHERE key IN (?,?,?,?)', radio_provenance.KEYS))}

    @synchronized
    def remember_playlists(self, playlists, account=None):
        with self.conn.atomic():
            for playlist in playlists:
                radio_provenance.remember(self.conn, playlist, account)
