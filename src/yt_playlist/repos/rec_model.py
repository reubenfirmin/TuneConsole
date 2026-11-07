"""SQL records for taste weights, feedback, standing controls and checkpoints."""
from yt_playlist.repos.base import Repo, synchronized, lookup_read, snapshot_read
from yt_playlist.core import artists, catalog, genre_identity, taste_axes, taste_control_state


class RecModelRepo(Repo):
    @lookup_read
    def weight_records(self):
        return {row['axis']: dict(row) for row in self.conn.execute(
            taste_axes.projection(self.conn, 'rec_weights', 'axis,weight,updated_at'))}

    @lookup_read
    def weight_record(self, axis):
        row = taste_axes.lookup(self.conn, 'rec_weights', axis, 'weight,updated_at')
        return dict(row) if row else None

    @synchronized
    def put_weight(self, axis, weight, now):
        with self.conn.atomic():
            column = taste_axes.column(self.conn)
            reference = taste_axes.reference(self.conn, 'rec_weights', axis, create=True)
            self.conn.execute(f'INSERT INTO rec_weights({column},weight,updated_at) VALUES (?,?,?) '
                f'ON CONFLICT({column}) DO UPDATE SET weight=excluded.weight,updated_at=excluded.updated_at',
                (reference, weight, now))

    @synchronized
    def reset_weights(self):
        self.conn.execute('DELETE FROM rec_weights')
        self.conn.commit()

    @lookup_read
    def checkpoint_facts(self, key):
        typed = taste_control_state.available(self.conn)
        if typed and key != taste_control_state.CHECKPOINT:
            raise ValueError('Use the retained taste settings checkpoint owner')
        row = self.conn.execute('SELECT * FROM taste_settings_checkpoints').fetchone() if typed else None
        weights = [dict(row) for row in self.conn.execute(
            'SELECT * FROM taste_checkpoint_weights ORDER BY position')] if typed and row else []
        return {'typed': typed, 'header': dict(row) if row else None, 'weights': weights}

    @lookup_read
    def parameter_values(self, keys):
        return dict(self.conn.execute('SELECT key,value FROM settings WHERE key IN ('
                                      + ','.join('?' for _ in keys) + ')', keys))

    @synchronized
    def publish_checkpoint(self, revision, saved_at, weights, reference):
        with self.conn.atomic():
            rows = [(1, position, taste_axes.identity(self.conn, row['axis'], create=True),
                     row['axis'], row['weight'], row['updated_at']) for position, row in enumerate(weights)]
            self.conn.execute('DELETE FROM taste_settings_checkpoints')
            self.conn.execute('INSERT INTO taste_settings_checkpoints VALUES (1,?,?,?,?,?)',
                              (revision, saved_at, *reference))
            self.conn.executemany('INSERT INTO taste_checkpoint_weights VALUES (?,?,?,?,?,?)', rows)

    @synchronized
    def checkpoint_axes(self, positions):
        selected, result = set(positions), []
        with self.conn.atomic():
            for row in self.conn.execute('SELECT w.*,a.kind,a.genre_key FROM taste_checkpoint_weights w '
                    'JOIN taste_axes a ON a.id=w.axis_id ORDER BY position').fetchall():
                if row['position'] not in selected:
                    continue
                axis = row['axis_id']
                if row['kind'] == 'genre':
                    key = genre_identity.current_key(self.conn, row['genre_key'])
                    axis = taste_axes.identity(self.conn, 'genre:' + key, create=True)
                result.append((axis, row['weight'], row['updated_at']))
        return result

    @synchronized
    def replace_weights(self, weights, *, typed):
        with self.conn.atomic():
            rows = weights if typed else [(taste_axes.reference(self.conn, 'rec_weights', axis, create=True), value, at)
                                         for axis, value, at in weights]
            self.conn.execute('DELETE FROM rec_weights')
            self.conn.executemany(f'INSERT INTO rec_weights({taste_axes.column(self.conn)},weight,updated_at) VALUES (?,?,?)', rows)

    @lookup_read
    def resolve_key(self, key):
        return catalog.resolve_key(self.conn, key)

    @synchronized
    def insert_feedback(self, surface, key, kind, reason, scope, until, now, artist):
        with self.conn.atomic():
            artist_id = artists.state_id(self.conn, artist) if artist is not None else None
            self.conn.execute('INSERT INTO rec_feedback(surface,item_key,kind,reason,scope,until,created_at,artist_id,target_type) '
                'VALUES (?,?,?,?,?,?,?,?,?) ON CONFLICT(surface,item_key,scope) DO UPDATE SET '
                'kind=excluded.kind,reason=excluded.reason,until=excluded.until,created_at=excluded.created_at,'
                "artist_id=excluded.artist_id,target_type=CASE WHEN excluded.artist_id IS NOT NULL THEN 'artist' ELSE rec_feedback.target_type END",
                (surface, key, kind, reason, scope, until, now, artist_id,
                 'artist' if artist_id is not None else 'legacy_unresolved'))

    @snapshot_read
    def feedback_suppression(self, surface, scope, now, keys, *, local_kinds, global_kind, artist_kind):
        clause = (' AND item_key IN (' + ','.join('?' for _ in keys) + ')') if keys is not None else ''
        kinds = ','.join('?' for _ in local_kinds)
        blocked = {row[0] for row in self.conn.execute(
            'SELECT item_key FROM rec_feedback WHERE ((surface=? AND (scope=\'\' OR scope=?) '
            f'AND kind IN ({kinds})) OR kind=?) AND (until IS NULL OR until>?)' + clause,
            (surface, scope, *local_kinds, global_kind, now, *(keys or [])))}
        blocked.update(row[0] for row in self.conn.execute(
            'SELECT DISTINCT r.identity_key FROM rec_feedback f '
            f'JOIN {self.entry_artists_table} a ON a.artist_id=f.artist_id '
            'JOIN catalog_entries e ON e.id=a.entry_id JOIN recordings r ON r.id=e.recording_id '
            'WHERE f.kind=? AND (f.until IS NULL OR f.until>?)' +
            ((' AND r.identity_key IN (' + ','.join('?' for _ in keys) + ')') if keys is not None else ''),
            (artist_kind, now, *(keys or []))))
        return blocked

    @snapshot_read
    def genre_rows(self, keys):
        clause = (' AND identity_key IN (' + ','.join('?' for _ in keys) + ')') if keys is not None else ''
        return [tuple(row) for row in self.conn.execute(
            "SELECT identity_key,genre FROM tracks WHERE genre<>''" + clause +
            " UNION SELECT identity_key,genre FROM discovered_tracks WHERE genre<>''" + clause, (keys or []) * 2)]

    @lookup_read
    def rating_rows(self, keys):
        result = []
        for start in range(0, len(keys), 500):
            batch = keys[start:start + 500]
            result.extend(dict(row) for row in self.conn.execute('SELECT * FROM rec_feedback '
                'WHERE item_key IN (' + ','.join('?' for _ in batch) + ')', batch))
        return result

    @synchronized
    def publish_ratings(self, deleted, actions, inserted):
        with self.conn.atomic():
            self._publish_ratings(deleted, actions, inserted)

    def _publish_ratings(self, deleted, actions, inserted):
        self.conn.executemany("DELETE FROM rec_feedback WHERE item_key=? AND kind IN ('like','dislike') AND kind<>?", deleted)
        self.conn.executemany("UPDATE rec_feedback SET reason='action' WHERE surface='like' AND item_key=? AND scope=''", actions)
        self.conn.executemany('INSERT INTO rec_feedback(surface,item_key,kind,reason,scope,until,created_at) '
                              "VALUES (?,?,?,?,'',?,?)", inserted)

    @synchronized
    def publish_association_ratings(self, deleted, actions, inserted, ages, guards):
        with self.conn.atomic():
            self._publish_ratings(deleted, actions, inserted)
            self.conn.executemany("UPDATE rec_feedback SET created_at=?,until=? WHERE surface=? AND item_key=? AND scope=''", ages)
            self.conn.executemany('INSERT INTO rec_feedback(surface,item_key,kind,reason,scope,until,created_at) '
                "VALUES ('like_grad',?,'like_grad',?,'',NULL,?) ON CONFLICT(surface,item_key,scope) "
                'DO UPDATE SET created_at=excluded.created_at,reason=excluded.reason '
                'WHERE rec_feedback.created_at IS NULL OR excluded.created_at<rec_feedback.created_at', guards)

    @lookup_read
    def unban_facts(self, key):
        key = catalog.resolve_key(self.conn, key) or key
        recording = self.conn.execute('SELECT id FROM recordings WHERE identity_key=?', (key,)).fetchone()
        cutoff = self.conn.execute('SELECT MAX(ir.observed_at) FROM item_ratings ir JOIN youtube_items y '
            'ON y.video_id=ir.video_id JOIN recordings r ON r.id=y.recording_id '
            "WHERE r.identity_key=? AND ir.source='api' AND ir.status='DISLIKE'", (key,)).fetchone()[0]
        return key, recording[0] if recording else None, cutoff

    @synchronized
    def publish_unban(self, key, recording, cutoff):
        with self.conn.atomic():
            self.conn.execute("DELETE FROM rec_feedback WHERE item_key=? AND kind='dislike'", (key,))
            self.conn.execute('INSERT INTO rec_feedback(surface,item_key,kind,scope,created_at,target_type,recording_id) '
                "VALUES ('rating_override',?,'unban','',?,?,?) ON CONFLICT(surface,item_key,scope) DO UPDATE SET "
                'kind=excluded.kind,created_at=excluded.created_at,target_type=excluded.target_type,recording_id=excluded.recording_id',
                (key, cutoff, 'recording' if recording is not None else 'legacy_unresolved', recording))

    @lookup_read
    def disliked_identity_keys(self):
        return {row[0] for row in self.conn.execute("SELECT item_key FROM rec_feedback WHERE kind='dislike'")}

    @lookup_read
    def disliked_key_page(self, *, after=None, limit=128):
        if (after is not None and not isinstance(after, str) or type(limit) is not int or not 1 <= limit <= 512):
            raise ValueError('Invalid bounded dislike page')
        predicate = ' AND item_key>?' if after is not None else ''
        return [row[0] for row in self.conn.execute("SELECT DISTINCT item_key FROM rec_feedback WHERE kind='dislike'"
            + predicate + ' ORDER BY item_key LIMIT ?', (*((after,) if after is not None else ()), limit))]

    @lookup_read
    def list_dislikes(self):
        return self.conn.execute("SELECT item_key,until,created_at FROM rec_feedback WHERE kind='dislike' ORDER BY created_at DESC").fetchall()

    @lookup_read
    def recent_liked_keys(self, limit=None):
        rows = [row[0] for row in self.conn.execute("SELECT item_key FROM rec_feedback WHERE kind='like' ORDER BY created_at DESC")]
        return rows[:limit] if limit else rows

    @lookup_read
    def recent_liked_with_ts(self, limit=None):
        rows = [(row[0], float(row[1])) for row in self.conn.execute(
            "SELECT item_key,created_at FROM rec_feedback WHERE kind='like' AND reason='action' ORDER BY created_at DESC")]
        return rows[:limit] if limit else rows

    @lookup_read
    def like_provenance(self, key):
        row = self.conn.execute("SELECT reason FROM rec_feedback WHERE kind='like' AND item_key=? AND scope=''", (key,)).fetchone()
        return row[0] if row else None

    @synchronized
    def mark_graduated_once(self, key, kind, now):
        if self.conn.execute("SELECT 1 FROM rec_feedback WHERE surface=? AND item_key=? AND scope='' AND kind=?", (kind, key, kind)).fetchone():
            return False
        self.conn.execute('INSERT INTO rec_feedback(surface,item_key,kind,reason,scope,until,created_at) '
                          "VALUES (?,?,?,NULL,'',NULL,?)", (kind, key, kind, now))
        self.conn.commit()
        return True

    @lookup_read
    def disliked_with_ts(self):
        return [(row[0], float(row[1])) for row in self.conn.execute(
            "SELECT item_key,created_at FROM rec_feedback WHERE kind='dislike' ORDER BY created_at DESC")]

    @synchronized
    def set_lean(self, axis, value, now):
        column = taste_axes.column(self.conn)
        reference = taste_axes.reference(self.conn, 'rec_lean', axis, create=True)
        self.conn.execute(f'INSERT INTO rec_lean({column},value,updated_at) VALUES (?,?,?) '
            f'ON CONFLICT({column}) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at', (reference, float(value), now))
        self.conn.commit()

    @lookup_read
    def get_leans(self):
        return {row['axis']: row['value'] for row in self.conn.execute(taste_axes.projection(self.conn, 'rec_lean', 'axis,value'))}

    @lookup_read
    def get_lean(self, axis):
        row = taste_axes.lookup(self.conn, 'rec_lean', axis, 'value')
        return row['value'] if row else 1.

    @lookup_read
    def lean_rows(self):
        return self.conn.execute(taste_axes.projection(self.conn, 'rec_lean', 'axis,value,updated_at,last_graduated_day')).fetchall()

    @synchronized
    def set_lean_graduated_day(self, axis, day):
        self.conn.execute(f'UPDATE rec_lean SET last_graduated_day=? WHERE {taste_axes.column(self.conn)}=?',
                          (day, taste_axes.reference(self.conn, 'rec_lean', axis)))
        self.conn.commit()

    @synchronized
    def clear_lean(self, axis):
        self.conn.execute(f'DELETE FROM rec_lean WHERE {taste_axes.column(self.conn)}=?', (taste_axes.reference(self.conn, 'rec_lean', axis),))
        self.conn.commit()

    @synchronized
    def clear_all_leans(self):
        self.conn.execute('DELETE FROM rec_lean')
        self.conn.commit()

    @lookup_read
    def hidden_facets(self):
        return {row['axis'] for row in self.conn.execute(taste_axes.projection(self.conn, 'home_hidden_facet', 'axis'))}

    @synchronized
    def hide_facet(self, axis):
        self.conn.execute(f'INSERT OR IGNORE INTO home_hidden_facet({taste_axes.column(self.conn)}) VALUES (?)',
                          (taste_axes.reference(self.conn, 'home_hidden_facet', axis, create=True),))
        self.conn.commit()

    @synchronized
    def unhide_facet(self, axis):
        predicate, args = taste_axes.matches(self.conn, 'home_hidden_facet', axis)
        self.conn.execute('DELETE FROM home_hidden_facet WHERE ' + predicate, args)
        self.conn.commit()

    @synchronized
    def clear_hidden_facets(self):
        self.conn.execute('DELETE FROM home_hidden_facet')
        self.conn.commit()

    @lookup_read
    def muted_album_ids(self, browse_ids, now):
        browse_ids = list(dict.fromkeys(browse_ids))
        if not browse_ids:
            return set()
        return {row[0] for row in self.conn.execute('SELECT DISTINCT p.provider_id FROM album_provider_ids p '
            'JOIN album_artists a ON a.album_id=p.album_id JOIN rec_feedback f ON f.artist_id=a.artist_id '
            "WHERE p.provider='youtube' AND f.kind='mute' AND (f.until IS NULL OR f.until>?) AND p.provider_id IN ("
            + ','.join('?' for _ in browse_ids) + ')', (now, *browse_ids))}

    @lookup_read
    def muted_artists(self):
        result = set()
        for row in self.conn.execute("SELECT item_key,artist_id FROM rec_feedback WHERE kind='mute'"):
            if row['artist_id']:
                name = self.conn.execute('SELECT name FROM artists WHERE id=?', (row['artist_id'],)).fetchone()[0]
                result.add(name if artists.lookup(self.conn, name) == row['artist_id'] else artists.token(row['artist_id']))
            elif row['item_key'].startswith('artist:'):
                result.add(row['item_key'][7:])
        return result

    @lookup_read
    def feedback_summary(self):
        return dict(self.conn.execute("SELECT kind,COUNT(*) FROM rec_feedback WHERE kind<>'unban' GROUP BY kind"))

    @lookup_read
    def recent_rating_inputs(self, since, until):
        rows = self.conn.execute("WITH inputs AS (SELECT kind,item_key,MAX(CASE WHEN reason='action' THEN 1 ELSE 0 END) action "
            "FROM rec_feedback WHERE kind IN ('like','dislike') AND created_at>=? AND created_at<=? GROUP BY kind,item_key) "
            "SELECT kind,CASE WHEN kind='like' AND action=1 THEN 'action' ELSE 'sync' END provenance,COUNT(*) n "
            'FROM inputs GROUP BY kind,provenance', (since, until))
        counts = {(row['kind'], row['provenance']): row['n'] for row in rows}
        return {'likes': counts.get(('like', 'action'), 0), 'synced_likes': counts.get(('like', 'sync'), 0),
                'dislikes': sum(count for (kind, _), count in counts.items() if kind == 'dislike')}

    @synchronized
    def clear_feedback(self, *, retained_kinds):
        with self.conn.atomic():
            self.conn.execute('DELETE FROM rec_feedback WHERE kind NOT IN (' + ','.join('?' for _ in retained_kinds) + ')', retained_kinds)
            self.conn.execute('DELETE FROM rec_impressions')

    @lookup_read
    def next_feedback_expiry(self, now):
        return self.conn.execute('SELECT MIN(until) FROM rec_feedback WHERE until>?', (now,)).fetchone()[0]
