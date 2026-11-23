"""SQL records for taste weights, feedback, standing controls and checkpoints."""
from yt_playlist.repos.base import Repo, synchronized, lookup_read, snapshot_read
from yt_playlist.core.db_revisions import column_group_reads, setting_reads
from yt_playlist.core import artists, catalog, genre_identity, model_state, recording_library, taste_axes, taste_control_state
from yt_playlist.core import recommendation_impressions
from yt_playlist.repos import collection_file_writes
from yt_playlist.repos.settings import SettingsRepo


class RecModelRepo(Repo):
    @lookup_read
    def recording_state(self):
        return model_state.recording(self.conn)

    @lookup_read
    def artist_state(self):
        return model_state.artist(self.conn)

    @synchronized
    def reserve_encoder(self, owner, integrity):
        with self.conn.atomic():
            return collection_file_writes.reserve(self.conn, 'taste-settings', owner, integrity)

    @synchronized
    def publish_model_state(self, recording, artist, *, file_writes, related_settings):
        with self.conn.atomic():
            for identity, owner, reference in file_writes:
                collection_file_writes.complete(self.conn, identity, 'taste-settings', owner, reference)
            if recording is not None:
                model_state.save_recording(self.conn, *recording)
            if artist is not None:
                model_state.save_artist(self.conn, *artist)
            if related_settings:
                SettingsRepo(self._store).publish_values(related_settings)

    @synchronized
    def reserve_checkpoint_file(self, integrity):
        with self.conn.atomic():
            return collection_file_writes.reserve(self.conn, 'taste-checkpoint', 1, integrity)

    @lookup_read
    def feedback_expiry(self, now):
        row = self.conn.execute('SELECT min(until) FROM rec_feedback WHERE until>? '
            "AND (kind IN ('dislike','mute') OR (surface='for_you' AND kind IN ('dismiss','not_now')))",
            (now,)).fetchone()
        return row[0]

    @snapshot_read
    def recent_recommendation_feedback(self, start, now):
        return [dict(row) for row in self.conn.execute(
            "SELECT surface,item_key,kind,reason,scope,created_at FROM rec_feedback "
            "WHERE target_type<>'artist' AND kind IN ('more','less','dismiss','not_now') "
            "AND COALESCE(reason,'')<>'own_it' AND created_at BETWEEN ? AND ? "
            "ORDER BY created_at DESC, surface, item_key, scope", (start, now))]

    @lookup_read
    def weight_records(self):
        with column_group_reads(self._store, 'taste_axes', 'weights'):
            return {row['axis']: dict(row) for row in self.conn.execute(
                f'SELECT {taste_axes.token_expression()} axis,weight,weight_updated_at updated_at '
                'FROM taste_axes a WHERE weight IS NOT NULL')}

    @lookup_read
    def legacy_dislike_weight_events(self):
        rows = self.conn.execute(f"""
            WITH events AS MATERIALIZED (SELECT * FROM {taste_axes.GRADUATIONS}), first_dislikes AS (
                SELECT axis_id, min(id) AS first_id FROM events
                WHERE source='dislike' GROUP BY axis_id
            ), recorded_weights AS (
                SELECT w.id axis_id, w.weight, w.weight_updated_at updated_at, d.first_id, max(e.id) AS last_id
                FROM first_dislikes d JOIN taste_axes w ON w.id=d.axis_id
                JOIN events e ON e.axis_id=w.id
                    AND e.id>=d.first_id AND e.created_at=w.weight_updated_at AND e.new_weight=w.weight
                WHERE w.weight<>0 AND e.source<>'dated-support'
                GROUP BY w.id
            )
            SELECT e.axis, w.weight, w.updated_at, e.source, e.factor, e.new_weight, e.created_at
            FROM recorded_weights w JOIN events e ON e.axis_id=w.axis_id
            WHERE e.id BETWEEN w.first_id AND w.last_id AND e.source<>'dated-support'
            ORDER BY e.id
        """)
        history = {}
        for axis, weight, updated_at, source, factor, new_weight, at in rows:
            history.setdefault((axis, weight, updated_at), []).append((source, factor, new_weight, at))
        return history

    @lookup_read
    def weight_record(self, axis):
        with column_group_reads(self._store, 'taste_axes', 'weights'):
            row = self.conn.execute('SELECT weight,weight_updated_at updated_at FROM taste_axes '
                                    'WHERE id=? AND weight IS NOT NULL', (taste_axes.identity(self.conn, axis),)).fetchone()
            return dict(row) if row else None

    @synchronized
    def put_weight(self, axis, weight, now):
        with self.conn.atomic():
            reference = taste_axes.identity(self.conn, axis, create=True)
            self.conn.execute('UPDATE taste_axes SET weight=?,weight_updated_at=? WHERE id=?', (weight, now, reference))

    @synchronized
    def reset_weights(self):
        self.conn.execute('UPDATE taste_axes SET weight=NULL,weight_updated_at=NULL WHERE weight IS NOT NULL')
        self.conn.commit()

    @lookup_read
    def checkpoint_facts(self):
        return taste_control_state.checkpoint_facts(self.conn)

    @lookup_read
    def parameter_values(self, keys):
        with setting_reads(self._store, keys):
            return dict(self.conn.execute('SELECT key,value FROM settings WHERE key IN ('
                                          + ','.join('?' for _ in keys) + ')', keys))

    @synchronized
    def publish_checkpoint(self, revision, saved_at, weights, reference, *, file_id):
        with self.conn.atomic():
            collection_file_writes.complete(self.conn, file_id, 'taste-checkpoint', 1, reference)
            taste_control_state.save_checkpoint(self.conn, revision, saved_at, reference, weights)

    @synchronized
    def checkpoint_axes(self, positions):
        with self.conn.atomic():
            return taste_control_state.checkpoint_axes(self.conn, positions)

    @synchronized
    def replace_weights(self, weights):
        with self.conn.atomic():
            self.reset_weights()
            self.conn.executemany('UPDATE taste_axes SET weight=?,weight_updated_at=? WHERE id=?',
                                  ((weight, updated_at, axis) for axis, weight, updated_at in weights))

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
        return {row[0] for row in self.conn.execute(
            'SELECT item_key FROM rec_feedback WHERE ((surface=? AND (scope=\'\' OR scope=?) '
            f'AND kind IN ({kinds})) OR kind=?) AND (until IS NULL OR until>?)' + clause +
            ' UNION SELECT r.identity_key FROM rec_feedback f '
            f'JOIN {self.entry_artists_table} a ON a.artist_id=f.artist_id '
            'JOIN track_sources e ON e.id=a.entry_id JOIN recordings r ON r.id=e.recording_id '
            'WHERE f.kind=? AND (f.until IS NULL OR f.until>?)' +
            ((' AND r.identity_key IN (' + ','.join('?' for _ in keys) + ')') if keys is not None else ''),
            (surface, scope, *local_kinds, global_kind, now, *(keys or []), artist_kind, now, *(keys or [])))}

    @snapshot_read
    def genre_rows(self, keys):
        clause = (' AND identity_key IN (' + ','.join('?' for _ in keys) + ')') if keys is not None else ''
        library = recording_library.TRACKS_SQL
        return [tuple(row) for row in self.conn.execute(
            f"SELECT identity_key,genre FROM ({library}) WHERE genre<>''" + clause +
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
        with self.conn.atomic(join=True):
            self._publish_ratings(deleted, actions, inserted)

    def _publish_ratings(self, deleted, actions, inserted):
        self.conn.executemany("DELETE FROM rec_feedback WHERE item_key=? AND kind IN ('like','dislike') AND kind<>?", deleted)
        self.conn.executemany("UPDATE rec_feedback SET reason='action' WHERE surface='like' AND item_key=? AND scope=''", actions)
        self.conn.executemany('INSERT INTO rec_feedback(surface,item_key,kind,reason,scope,until,created_at) '
                              "VALUES (?,?,?,?,'',?,?)", inserted)

    @synchronized
    def publish_association_ratings(self, deleted, actions, inserted, ages, guards, *, association=None):
        with self.conn.atomic():
            self._publish_ratings(deleted, actions, inserted)
            self.conn.executemany(
                "UPDATE rec_feedback SET created_at=?,until=?,reason=? WHERE surface=? AND item_key=? AND scope=''", ages)
            self.conn.executemany('INSERT INTO rec_feedback(surface,item_key,kind,reason,scope,until,created_at) '
                "VALUES ('like_grad',?,'like_grad',?,'',NULL,?) ON CONFLICT(surface,item_key,scope) "
                'DO UPDATE SET created_at=excluded.created_at,reason=excluded.reason '
                'WHERE rec_feedback.created_at IS NULL OR excluded.created_at<rec_feedback.created_at', guards)
            from yt_playlist.core.association_work import complete
            complete(self.conn, association, 'model')

    @lookup_read
    def unban_facts(self, key):
        key = catalog.resolve_key(self.conn, key) or key
        recording = self.conn.execute('SELECT id FROM recordings WHERE identity_key=?', (key,)).fetchone()
        cutoff = self.conn.execute('SELECT MAX(ir.observed_at) FROM item_ratings ir JOIN track_sources y '
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
    def disliked_identity_keys(self, *, keys=None, now=None):
        sql = "SELECT item_key FROM rec_feedback WHERE kind='dislike'"
        args = []
        if now is not None:
            sql += ' AND (until IS NULL OR until>?)'
            args.append(now)
        if keys is not None:
            keys = list(dict.fromkeys(keys))
            if not keys:
                return set()
            sql += ' AND item_key IN (' + ','.join('?' for _ in keys) + ')'
            args.extend(keys)
        return {row[0] for row in self.conn.execute(sql, args)}

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
        reference = taste_axes.identity(self.conn, axis, create=True)
        self.conn.execute('UPDATE taste_axes SET lean=?,lean_updated_at=? WHERE id=?', (float(value), now, reference))
        self.conn.commit()

    @lookup_read
    def get_leans(self):
        with column_group_reads(self._store, 'taste_axes', 'mix_controls'):
            return dict(self.conn.execute(f'SELECT {taste_axes.token_expression()},lean FROM taste_axes a '
                                          'WHERE lean IS NOT NULL'))

    @lookup_read
    def get_lean(self, axis):
        with column_group_reads(self._store, 'taste_axes', 'mix_controls'):
            row = self.conn.execute('SELECT lean FROM taste_axes WHERE id=? AND lean IS NOT NULL',
                                    (taste_axes.identity(self.conn, axis),)).fetchone()
            return row[0] if row else 1.

    @lookup_read
    def lean_rows(self):
        with column_group_reads(self._store, 'taste_axes', 'mix_controls'):
            return self.conn.execute(f'SELECT {taste_axes.token_expression()} axis,lean value,'
                'lean_updated_at updated_at,lean_last_graduated_day last_graduated_day '
                'FROM taste_axes a WHERE lean IS NOT NULL').fetchall()

    @synchronized
    def set_lean_graduated_day(self, axis, day):
        self.conn.execute('UPDATE taste_axes SET lean_last_graduated_day=? WHERE id=? AND lean IS NOT NULL',
                          (day, taste_axes.identity(self.conn, axis)))
        self.conn.commit()

    @synchronized
    def clear_lean(self, axis):
        self.conn.execute('UPDATE taste_axes SET lean=NULL,lean_updated_at=NULL,lean_last_graduated_day=NULL '
                          'WHERE id=? AND lean IS NOT NULL', (taste_axes.identity(self.conn, axis),))
        self.conn.commit()

    @synchronized
    def clear_all_leans(self):
        self.conn.execute('UPDATE taste_axes SET lean=NULL,lean_updated_at=NULL,lean_last_graduated_day=NULL '
                          'WHERE lean IS NOT NULL')
        self.conn.commit()

    @lookup_read
    def hidden_facets(self):
        with column_group_reads(self._store, 'taste_axes', 'mix_controls'):
            return {row[0] for row in self.conn.execute(
                f'SELECT {taste_axes.token_expression()} FROM taste_axes a WHERE home_hidden=1')}

    @synchronized
    def hide_facet(self, axis):
        self.conn.execute('UPDATE taste_axes SET home_hidden=1 WHERE id=? AND home_hidden=0',
                          (taste_axes.identity(self.conn, axis, create=True),))
        self.conn.commit()

    @synchronized
    def unhide_facet(self, axis):
        self.conn.execute('UPDATE taste_axes SET home_hidden=0 WHERE id=? AND home_hidden=1',
                          (taste_axes.identity(self.conn, axis),))
        self.conn.commit()

    @synchronized
    def clear_hidden_facets(self):
        self.conn.execute('UPDATE taste_axes SET home_hidden=0 WHERE home_hidden=1')
        self.conn.commit()

    @lookup_read
    def artist_mute_targets(self, now):
        ids, unresolved = set(), set()
        for row in self.conn.execute("SELECT artist_id,item_key FROM rec_feedback WHERE kind='mute' "
                                     'AND (until IS NULL OR until>?)', (now,)):
            if row['artist_id'] is not None:
                ids.add(row['artist_id'])
            elif row['item_key'].startswith('artist:'):
                unresolved.add(row['item_key'][7:])
        return ids, unresolved

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
    def muted_artists(self, now=None):
        result = set()
        for row in self.conn.execute("SELECT item_key,artist_id FROM rec_feedback WHERE kind='mute' "
                                     'AND (? IS NULL OR until IS NULL OR until>?)', (now, now)):
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
            recommendation_impressions.clear(self.conn)

    @lookup_read
    def next_feedback_expiry(self, now):
        return self.conn.execute('SELECT MIN(until) FROM rec_feedback WHERE until>?', (now,)).fetchone()[0]
