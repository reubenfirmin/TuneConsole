"""CollectionRepo: saved-album CRUD plus the aggregate playlist and saved-album
library views that power the collection pages.
"""
import json
from contextlib import contextmanager
from yt_playlist.core import artists as artist_catalog, albums as album_catalog, album_aliases, album_memberships, album_tracklists

from yt_playlist.repos.base import Repo, synchronized, snapshot_read, lookup_read


_COMPLETE_ALBUM_TRACKLISTS = (
    "SELECT p.provider_id FROM {states} f JOIN album_provider_ids p "
    "ON p.album_id=f.album_id AND p.provider='youtube' WHERE f.complete=1 "
    "AND f.expected_track_count>0 AND f.expected_track_count="
    "(SELECT COUNT(*) FROM {tracks} t WHERE t.album_id=f.album_id AND t.is_full=1) "
    "AND NOT EXISTS(SELECT 1 FROM {tracks} t LEFT JOIN library_memberships m ON m.entry_id=t.entry_id "
    "WHERE t.album_id=f.album_id AND t.is_full=1 AND m.entry_id IS NULL)"
)
_PUBLISHED_OCCURRENCE_TRACKLISTS = (
    "SELECT p.provider_id FROM album_provider_ids p JOIN albums a ON a.id=p.album_id "
    "JOIN album_tracklists t ON t.id=a.current_tracklist_id WHERE p.provider='youtube' "
    "AND t.status='complete' AND t.expected_count>0 AND t.expected_count="
    "(SELECT count(*) FROM album_track_occurrences o WHERE o.tracklist_id=t.id) "
)
_COMPLETE_OCCURRENCE_TRACKLISTS = _PUBLISHED_OCCURRENCE_TRACKLISTS + (
    "AND NOT EXISTS(SELECT 1 FROM album_track_occurrences o WHERE o.tracklist_id=t.id "
    "AND NOT EXISTS(SELECT 1 FROM album_occurrence_sources s JOIN library_memberships m ON m.entry_id=s.entry_id "
    "WHERE s.occurrence_id=o.id))"
)


class CollectionRepo(Repo):
    @snapshot_read
    def taste_album_inputs(self):
        return self._store.memoized_read(('album-taste-inputs', 1), self._taste_album_inputs)

    def _taste_album_inputs(self):
        from yt_playlist.core import genre_classifications
        genre = genre_classifications.read_expression(self.conn, 'a', 'albums')
        rows = self.conn.execute(
            f"SELECT a.id,a.title,a.artist,p.provider_id browse,a.year,{genre} AS genre,a.thumbnail,"
            "at.id occurrence,at.is_full,f.complete,f.expected_track_count,"
            "COALESCE(r.identity_key,er.identity_key,at.legacy_track_key) key,"
            f"EXISTS(SELECT 1 FROM {album_memberships.read_relation(self.conn)} m WHERE m.album_id=a.id) saved "
            f"FROM albums a JOIN {self.album_tracks_table} at ON at.album_id=a.id "
            "LEFT JOIN catalog_entries e ON e.id=at.entry_id "
            "LEFT JOIN recordings er ON er.id=e.recording_id "
            "LEFT JOIN recordings r ON r.id=at.recording_id "
            f"LEFT JOIN {self.album_fetch_state_table} f ON f.album_id=a.id "
            "JOIN album_primary_provider_ids p ON p.album_id=a.id AND p.provider='youtube'").fetchall()
        days = {row['identity_key']: row['days'] for row in self.conn.execute(
            "SELECT d.identity_key,COUNT(DISTINCT d.day) days FROM listening_days d "
            "WHERE d.identity_key IN ("
            "SELECT COALESCE(r.identity_key,er.identity_key,at.legacy_track_key) "
            f"FROM {self.album_tracks_table} at LEFT JOIN catalog_entries e ON e.id=at.entry_id "
            "LEFT JOIN recordings er ON er.id=e.recording_id "
            "LEFT JOIN recordings r ON r.id=at.recording_id) GROUP BY d.identity_key")}
        return {'tracks': [dict(row) for row in rows], 'days': days}

    @lookup_read
    def uses_occurrences(self):
        return album_tracklists.available(self.conn)

    @lookup_read
    def tracklist_inputs(self, album_id, keys):
        inputs = {}
        keys = list(dict.fromkeys(keys))
        for offset in range(0, len(keys), 500):
            page = keys[offset:offset + 500]
            placeholders = ','.join('?' for _ in page)
            for row in self.conn.execute('SELECT id,identity_key FROM recordings WHERE identity_key IN (' + placeholders + ')', page):
                inputs[row['identity_key']] = {'recording_id': row['id'], 'entries': []}
            for row in self.conn.execute('SELECT DISTINCT e.id,r.identity_key FROM catalog_entries e '
                    'JOIN recordings r ON r.id=e.recording_id JOIN album_tracks t ON t.entry_id=e.id '
                    'WHERE t.album_id=? AND r.identity_key IN (' + placeholders + ')', (album_id, *page)):
                inputs[row['identity_key']]['entries'].append(row['id'])
        return {'occurrences': album_tracklists.available(self.conn), 'keys': inputs}

    @synchronized
    def store_album_tracklist(self, album_id, browse_id, rows, *, occurrences, ordered, complete, expected_count, now):
        if occurrences:
            identity = album_tracklists.begin(self.conn, album_id, browse_id,
                capture_kind='provider' if ordered else 'legacy_full', order_status='known' if ordered else 'unknown',
                expected_count=expected_count, fetched_at=now)
            for row in rows:
                album_tracklists.occurrence(self.conn, identity, **row)
            album_tracklists.finish(self.conn, identity, complete=complete)
        else:
            self.conn.execute('DELETE FROM album_tracks WHERE album_id=? AND is_full=1', (album_id,))
            self.conn.executemany('INSERT INTO album_tracks(album_id,entry_id,recording_id,legacy_track_key,position,'
                'disc_number,track_number,is_full,source) VALUES (?,?,?,?,?,?,?,1,\'full_fetch\')', [
                    (album_id, *(row.get(field) for field in
                        ('entry_id', 'recording_id', 'legacy_track_key', 'position', 'disc_number', 'track_number'))) for row in rows])
            self.conn.execute('DELETE FROM album_tracks WHERE album_id=? AND is_full=0 AND entry_id IN '
                '(SELECT entry_id FROM album_tracks WHERE album_id=? AND is_full=1)', (album_id, album_id))
            self.conn.execute('INSERT INTO album_fetch_state VALUES (?,?,1,?) ON CONFLICT(album_id) DO UPDATE SET '
                'fetched_at=excluded.fetched_at,complete=1,expected_track_count=excluded.expected_track_count',
                (album_id, now, expected_count))

    @synchronized
    def store_album_share(self, album_id, browse_id, title, videos, *, occurrences, now):
        if occurrences:
            album_tracklists.freeze(self.conn, album_id, browse_id, title, videos, now=now)
        else:
            self.conn.execute('DELETE FROM album_share_snapshots WHERE browse_id=?', (browse_id,))
            if videos:
                self.conn.execute('INSERT INTO album_share_snapshots VALUES (?,?,?)', (browse_id, title, json.dumps(videos)))

    @lookup_read
    def unreferenced_tracklists(self, browse_id):
        return [dict(row) for row in self.conn.execute('SELECT t.id,t.status FROM album_tracklists t '
            "WHERE t.provider='youtube' AND t.provider_id=? AND t.capture_kind='provider' "
            "AND t.status IN ('complete','partial') AND NOT EXISTS(SELECT 1 FROM albums a WHERE a.current_tracklist_id=t.id) "
            'AND NOT EXISTS(SELECT 1 FROM album_share_snapshots s WHERE s.tracklist_id=t.id) '
            'ORDER BY t.status,t.id DESC', (browse_id,))]

    @synchronized
    def delete_tracklists(self, identities):
        for identity in identities:
            self.conn.execute('DELETE FROM album_occurrence_sources WHERE occurrence_id IN '
                '(SELECT id FROM album_track_occurrences WHERE tracklist_id=?)', (identity,))
            self.conn.execute('DELETE FROM album_track_occurrences WHERE tracklist_id=?', (identity,))
            self.conn.execute('DELETE FROM album_tracklists WHERE id=?', (identity,))

    @snapshot_read
    def album_share_inputs(self, browse_id):
        if album_tracklists.available(self.conn):
            aid = album_catalog.lookup(self.conn,browse_id)
            frozen = self.conn.execute('SELECT id,title FROM album_share_snapshots WHERE album_id=? '
                'ORDER BY captured_provider_id=? DESC,id DESC LIMIT 1', (aid, browse_id)).fetchone()
            if frozen:
                videos = [row[0] for row in self.conn.execute(
                    'SELECT COALESCE(i.raw_source_ref,e.video_id) FROM album_share_items i '
                    'LEFT JOIN catalog_entries e ON e.id=i.entry_id WHERE i.snapshot_id=? ORDER BY i.position', (frozen['id'],))]
                return {'layout': 'frozen', 'title': frozen['title'], 'videos': videos}
            if not self.has_published_album_tracklist(browse_id):
                return {'layout': 'missing'}
            rows = self.conn.execute('SELECT o.position,e.video_id FROM albums a '
                'JOIN album_track_occurrences o ON o.tracklist_id=a.current_tracklist_id '
                "LEFT JOIN album_occurrence_sources s ON s.occurrence_id=o.id AND s.provider='youtube' AND s.entry_id="
                "(SELECT MIN(entry_id) FROM album_occurrence_sources WHERE occurrence_id=o.id AND provider='youtube') "
                'LEFT JOIN catalog_entries e ON e.id=s.entry_id WHERE a.id=? ORDER BY o.position', (aid,)).fetchall()
            title = self.conn.execute('SELECT title FROM albums WHERE id=?', (aid,)).fetchone()
            return {'layout': 'occurrences', 'title': title[0], 'items': [dict(row) for row in rows]}
        if not self.is_album_tracklist_complete(browse_id):
            return {'layout': 'missing'}
        from yt_playlist.core.preparation.album_share_publication import OWNER, selected_share
        selected = OWNER.selected(self.conn)
        if selected is not None:
            share = selected_share(self.conn, selected, browse_id)
            return {'layout': 'frozen', 'title': share['title'], 'videos': share['video_ids']} if share else {'layout': 'missing'}
        row = self.conn.execute("SELECT title,video_ids_json FROM album_share_snapshots WHERE browse_id=?",
                                (browse_id,)).fetchone()
        if row is None:
            row = self.conn.execute('SELECT s.title,s.video_ids_json FROM album_share_snapshots s '
                "JOIN album_provider_ids p ON p.provider='youtube' AND p.provider_id=s.browse_id "
                'WHERE p.album_id=? ORDER BY p.is_primary DESC,p.provider_id LIMIT 1',
                (album_catalog.lookup(self.conn, browse_id),)).fetchone()
        return {'layout': 'frozen', 'title': row['title'], 'videos': json.loads(row['video_ids_json'])} if row else {'layout': 'missing'}

    @synchronized
    def cached_album_tracklist_ids(self):
        from yt_playlist.core.preparation.album_publication import relation as album_relation
        relation = 'album_tracklist_keys' if album_tracklists.available(self.conn) else album_relation(self.conn, 'album_tracklists')
        return {r[0] for r in self.conn.execute('SELECT browse_id FROM '+relation)}

    @synchronized
    def complete_album_tracklist_ids(self):
        """Fetched metadata alone does not mean every track was folded into the library."""
        query = self._complete_album_tracklists_query()
        return {row[0] for row in self.conn.execute(query)}

    @synchronized
    def is_album_tracklist_complete(self, browse_id):
        """Check one release using its provider ID, without enumerating the library."""
        query = self._complete_album_tracklists_query()
        return self.conn.execute(query + " AND p.provider_id=?", (browse_id,)).fetchone() is not None

    def _complete_album_tracklists_query(self):
        return (_COMPLETE_OCCURRENCE_TRACKLISTS if album_tracklists.available(self.conn) else
            _COMPLETE_ALBUM_TRACKLISTS.format(states=self.album_fetch_state_table, tracks=self.album_tracks_table))

    @lookup_read
    def has_published_album_tracklist(self, browse_id):
        """Provider completeness does not require saving every source to the library."""
        if not album_tracklists.available(self.conn):
            from yt_playlist.core.preparation.album_publication import selected, table
            if selected(self.conn) is None:
                return self.is_album_tracklist_complete(browse_id)
            return self.conn.execute('SELECT 1 FROM '+table('groups')+' g JOIN '+table('head')+
                " h ON h.revision=g.stage JOIN album_provider_ids p ON p.album_id=g.album_id AND p.provider='youtube' "
                "WHERE h.active=1 AND g.status='complete' AND p.provider_id=?", (browse_id,)).fetchone() is not None
        return self.conn.execute(_PUBLISHED_OCCURRENCE_TRACKLISTS + " AND p.provider_id=?",
                                 (browse_id,)).fetchone() is not None

    @lookup_read
    def playback_source_inputs(self, playlist_ytm_id, video_id, album_browse_id):
        playlist = self.conn.execute(
            "SELECT p.id,p.title FROM playlists p JOIN accounts i ON i.id=p.account_id "
            "WHERE p.provider_playlist_id=? ORDER BY i.is_master DESC,p.id LIMIT 1",
            (playlist_ytm_id,)).fetchone() if playlist_ytm_id else None
        albums = [dict(row) for row in self.conn.execute(
            "SELECT p.provider_id browse,a.title FROM albums a JOIN album_provider_ids p ON p.album_id=a.id "
            "WHERE p.provider='youtube' AND p.provider_id=?", (album_browse_id,))] if album_browse_id else [
                dict(row) for row in self.conn.execute(
                    f"SELECT DISTINCT p.provider_id browse,al.title FROM {self.album_tracks_table} a "
                    "JOIN catalog_entries e ON e.id=a.entry_id JOIN albums al ON al.id=a.album_id "
                    "JOIN album_primary_provider_ids p ON p.album_id=a.album_id AND p.provider='youtube' "
                    "WHERE e.video_id=? LIMIT 2", (video_id,))]
        return {'playlist': dict(playlist) if playlist else None, 'albums': albums}

    # --- saved albums (mirror of the YouTube Music library, synced locally) ---
    @contextmanager
    def membership_transaction(self):
        with self._lock, self.conn.atomic(join=True):
            self.conn.execute('UPDATE albums SET metadata_rank=metadata_rank WHERE 0')
            yield

    @synchronized
    def reset_legacy_cohort(self, *, replace):
        if album_memberships.available(self.conn):
            if replace:
                self.conn.execute('UPDATE account_album_state SET legacy_cohort_present=0,legacy_cohort_observed_at=NULL')
                self.conn.execute('INSERT INTO account_album_state(account_id,legacy_cohort_present) SELECT id,1 FROM accounts WHERE 1 '
                    'ON CONFLICT(account_id) DO UPDATE SET legacy_cohort_present=1,legacy_cohort_observed_at=NULL')
            else:
                self.conn.execute('UPDATE account_album_state SET legacy_cohort_observed_at=NULL WHERE legacy_cohort_present=1')
        elif replace:
            self.conn.execute('DELETE FROM album_library_legacy_cohort')
            self.conn.execute('INSERT INTO album_library_legacy_cohort(account_id) SELECT id FROM accounts')
        else:
            self.conn.execute('UPDATE album_library_legacy_cohort SET observed_at=NULL')

    @synchronized
    def advance_album_revision(self, account_id):
        if album_memberships.available(self.conn):
            album_memberships.advance(self.conn, account_id)
            return
        owners = [account_id] if account_id is not None else [r[0] for r in self.conn.execute('SELECT id FROM accounts')]
        self.conn.executemany('INSERT INTO album_library_revisions VALUES (?,1) '
                              'ON CONFLICT(account_id) DO UPDATE SET revision=revision+1', [(i,) for i in owners])

    @synchronized
    def saved_album_revision(self, account_id):
        """Capture before remote work; snapshot commits compare under a write reservation."""
        if album_memberships.available(self.conn):
            return album_memberships.revision(self.conn, account_id)
        row = self.conn.execute('SELECT revision FROM album_library_revisions WHERE account_id=?',
                                (account_id,)).fetchone()
        return row[0] if row else 0

    @snapshot_read
    def snapshot_inputs(self, accounts):
        accounts, result = list(dict.fromkeys(accounts)), {}
        state = 'account_album_state' if album_memberships.available(self.conn) else 'album_library_revisions'
        for offset in range(0, len(accounts), 500):
            page = accounts[offset:offset + 500]
            rows = self.conn.execute('SELECT a.id,a.active,COALESCE(s.revision,0) revision FROM accounts a '
                f'LEFT JOIN {state} s ON s.account_id=a.id WHERE a.id IN (' + ','.join('?' for _ in page) + ')', page)
            result.update((row['id'], {'active': bool(row['active']), 'revision': row['revision']}) for row in rows)
        return result

    @lookup_read
    def legacy_cohort_inputs(self):
        consolidated = album_memberships.available(self.conn)
        membership = 'unattributed_album_memberships' if consolidated else 'album_library_memberships'
        predicate = '' if consolidated else ' WHERE account_id IS NULL'
        legacy = self.conn.execute(f'SELECT 1 FROM {membership}' + predicate + ' LIMIT 1').fetchone()
        cohort = self.conn.execute('SELECT COUNT(*) count,SUM(legacy_cohort_observed_at IS NULL) missing '
            'FROM account_album_state WHERE legacy_cohort_present=1' if consolidated else
            'SELECT COUNT(*) count,SUM(observed_at IS NULL) missing FROM album_library_legacy_cohort').fetchone()
        return {'has_legacy': legacy is not None, 'count': cohort['count'], 'missing': cohort['missing'] or 0}

    @synchronized
    def delete_album_memberships(self, account_id):
        if album_memberships.available(self.conn):
            if account_id is None:
                self.conn.execute('DELETE FROM unattributed_album_memberships')
            else:
                self.conn.execute('DELETE FROM account_albums WHERE account_id=?', (account_id,))
        else:
            self.conn.execute('DELETE FROM album_library_memberships WHERE account_id IS ?', (account_id,))

    @synchronized
    def save_album_membership(self, album_id, account_id):
        if album_memberships.available(self.conn):
            album_memberships.save(self.conn, album_id, account_id)
        else:
            self.conn.execute("INSERT OR IGNORE INTO album_library_memberships(album_id,account_id,source) VALUES (?,?,'youtube')",
                              (album_id, account_id))

    @synchronized
    def store_snapshot_observation(self, account_id, now):
        if album_memberships.available(self.conn):
            album_memberships.observe_snapshot(self.conn, account_id, now)
        else:
            self.advance_album_revision(account_id)
            self.conn.execute('INSERT INTO album_library_snapshots VALUES (?,?) ON CONFLICT(account_id) '
                              'DO UPDATE SET observed_at=excluded.observed_at', (account_id, now))
            self.conn.execute('UPDATE album_library_legacy_cohort SET observed_at=? WHERE account_id=?', (now, account_id))

    @snapshot_read
    def saved_album_ids(self, *, browse_ids=None) -> set:
        if browse_ids is None:
            return {r[0] for r in self.conn.execute("SELECT p.provider_id FROM album_provider_ids p "
                f"WHERE p.provider='youtube' AND EXISTS(SELECT 1 FROM {album_memberships.read_relation(self.conn)} m WHERE m.album_id=p.album_id)")}
        result = set()
        for bid in set(browse_ids):
            if self.conn.execute(f'SELECT 1 FROM album_provider_ids p JOIN {album_memberships.read_relation(self.conn)} m '
                "ON m.album_id=p.album_id WHERE p.provider='youtube' AND p.provider_id=? LIMIT 1", (bid,)).fetchone():
                result.add(bid)
        return result

    @synchronized
    def album_id(self, browse_id):
        return album_catalog.lookup(self.conn,browse_id)

    @synchronized
    def ensure_album_identity(self, browse_id):
        """Resolve or insert an empty release identity without observing metadata."""
        with self.conn.atomic():
            identity = album_catalog.lookup(self.conn, browse_id)
            if identity is None:
                identity = self.conn.execute('INSERT INTO albums(metadata_rank) VALUES (0)').lastrowid
                self.conn.execute("INSERT INTO album_provider_ids(provider,provider_id,album_id) VALUES ('youtube',?,?)",
                                  (browse_id, identity))
            return identity

    @synchronized
    def canonical_album_ids(self, browse_ids):
        return {bid: album_catalog.lookup(self.conn, bid) for bid in set(browse_ids) if bid}

    @lookup_read
    def preferred_album_ids(self, browse_ids):
        """Resolve only proven aliases; unknown IDs and distinct editions stay independent."""
        ids = list(set(browse_ids))
        result = {bid: bid for bid in ids}
        for start in range(0, len(ids), 400):
            batch = ids[start:start + 400]
            placeholders = ','.join('?' for _ in batch)
            result.update(self.conn.execute(
                'SELECT p.provider_id,preferred.provider_id FROM album_provider_ids p '
                'JOIN album_primary_provider_ids preferred ON preferred.album_id=p.album_id '
                "AND preferred.provider=p.provider WHERE p.provider='youtube' "
                f'AND p.provider_id IN ({placeholders})', batch))
        return result

    @snapshot_read
    def album_alias_revision(self):
        """Append-only reconciliations invalidate derived release attribution."""
        return self.conn.execute('SELECT count(*) FROM album_alias_reconciliations').fetchone()[0]

    @snapshot_read
    def album_title_rows(self):
        return [dict(row) for row in self.conn.execute(
            "SELECT p.provider_id browse_id,a.title,a.artist FROM albums a "
            "JOIN album_primary_provider_ids p ON p.album_id=a.id AND p.provider='youtube'")]

    @synchronized
    def observe_album_representation(self, browse_id, album, *, now=None):
        with self.conn:
            if not self.conn.in_transaction:
                self.conn.execute('BEGIN IMMEDIATE')
            return album_aliases.observe(self.conn, browse_id, album, now=now)

    @synchronized
    def claim_album_alias_check(self, first, second, now, *, expire_before, retry_before):
        with self.conn:
            if not self.conn.in_transaction:
                self.conn.execute('BEGIN IMMEDIATE')
            left, right = album_catalog.lookup(self.conn, first), album_catalog.lookup(self.conn, second)
            if left is not None and left == right:
                return False
            self.conn.execute('DELETE FROM album_alias_checks WHERE checked_at<?', (expire_before,))
            return bool(self.conn.execute('INSERT INTO album_alias_checks VALUES (?,?,?) '
                'ON CONFLICT(first_id,second_id) DO UPDATE SET checked_at=excluded.checked_at '
                'WHERE checked_at<=?', (first, second, now, retry_before)).rowcount)

    @synchronized
    def remove_saved_album(self, browse_id, *, account_id=None) -> None:
        with self.conn:
            if not self.conn.in_transaction:
                self.conn.execute('BEGIN IMMEDIATE')
            self.advance_album_revision(account_id)
            album_id = album_catalog.lookup(self.conn,browse_id)
            if album_memberships.available(self.conn):
                album_memberships.remove(self.conn, album_id, account_id)
            else:
                self.conn.execute('DELETE FROM album_library_memberships WHERE album_id=? AND account_id IS ?',
                                  (album_id, account_id))

    @synchronized
    def get_saved_albums(self, *, account_id=None) -> list[dict]:
        rows = self.conn.execute(
            "SELECT browse_id browse, title, artist, COALESCE(year,'') year, type, thumbnail FROM saved_albums "
            f"WHERE (? IS NULL OR EXISTS (SELECT 1 FROM {album_memberships.read_relation(self.conn)} m "
            "JOIN album_provider_ids p ON p.album_id=m.album_id AND p.provider='youtube' "
            f"WHERE p.provider_id=saved_albums.browse_id AND m.{album_memberships.account_column(self.conn)}=?)) "
            "ORDER BY artist COLLATE NOCASE, title COLLATE NOCASE", (account_id,account_id)).fetchall()
        return [dict(r) for r in rows]

    @synchronized
    def saved_album_owners(self, browse_id=None) -> dict:
        """Retain inactive and unknown owners in combined-library reads and mutations."""
        owners = {}
        relation = album_memberships.read_relation(self.conn)
        account = album_memberships.account_column(self.conn)
        for row in self.conn.execute(
                f"SELECT p.provider_id browse,m.{account} account_id,i.label,i.active "
                f"FROM {relation} m JOIN album_provider_ids p "
                "ON p.album_id=m.album_id AND p.provider='youtube' "
                f"LEFT JOIN accounts i ON i.id=m.{account} "
                f"WHERE (? IS NULL OR p.provider_id=?) ORDER BY i.label COLLATE NOCASE,m.{account}",
                (browse_id,browse_id)):
            owners.setdefault(row['browse'], []).append({
                'account_id': row['account_id'], 'label': row['label'], 'active': bool(row['active'])})
        return owners

    @snapshot_read
    def saved_albums_recency(self, *, include_generated=False, browse_ids=None) -> dict:
        """{browse_id: newest play ts across the album's tracks | None}. Plays come from history (a
        track's identity_key in a snapshot). LEFT JOINs throughout, so an album whose tracks aren't in
        the library (or were never played) comes back None, i.e. as cold as possible."""
        activity = ('SELECT identity_key,played_at at FROM play_event_subjects UNION ALL '
                    'SELECT identity_key,day_ts+86400 FROM listening_days'
                    if include_generated else 'SELECT identity_key,at FROM listening_activity')
        if browse_ids is not None:
            # Home asks about a small prepared reserve. Limit both membership
            # and listening aggregation to those releases and their recordings.
            requested = list(dict.fromkeys(browse_ids))
            def read():
                result = {}
                for offset in range(0, len(requested), 500):
                    page = requested[offset:offset + 500]
                    qs = ','.join('?' for _ in page)
                    rows = self.conn.execute(
                        'WITH selected AS (SELECT sa.browse_id,t.identity_key FROM saved_albums sa '
                        f'LEFT JOIN {self.album_track_links_table} a ON a.browse_id=sa.browse_id '
                        'LEFT JOIN tracks t ON t.id=a.entry_id '
                        f'WHERE sa.browse_id IN ({qs})), '
                        'recent AS (SELECT identity_key,MAX(at) at FROM (' + activity + ') '
                        'WHERE identity_key IN (SELECT identity_key FROM selected) GROUP BY identity_key) '
                        'SELECT s.browse_id,MAX(r.at) FROM selected s '
                        'LEFT JOIN recent r ON r.identity_key=s.identity_key GROUP BY s.browse_id', page)
                    result.update(rows)
                return result
            return self._store.memoized_read(('saved-albums-recency', include_generated, tuple(requested)), read)
        # Only the newest observation matters. Aggregate once, with a typed
        # key SQLite can index, rather than scan the activity view per track.
        rows = self.conn.execute(
            "SELECT sa.browse_id AS browse,MAX(hi.at) AS last "
            "FROM saved_albums sa "
            f"LEFT JOIN {self.album_track_links_table} a ON a.browse_id=sa.browse_id "
            "LEFT JOIN tracks t ON t.id=a.entry_id "
            f"LEFT JOIN (SELECT CAST(identity_key AS TEXT) identity_key,MAX(at) at "
            f"FROM ({activity}) GROUP BY identity_key) hi ON hi.identity_key=t.identity_key "
            "GROUP BY sa.browse_id").fetchall()
        return {r["browse"]: r["last"] for r in rows}

    # --- aggregate library facts ---
    @snapshot_read
    def album_listening_summary(self, browse_ids):
        """Track-play totals and release coverage for a familiarity indicator."""
        browse_ids = list(dict.fromkeys(browse_ids))
        if not browse_ids:
            return {}
        placeholders = ','.join('?' for _ in browse_ids)
        rows = self.conn.execute(
            "WITH membership AS MATERIALIZED ("
            "SELECT DISTINCT p.provider_id browse,"
            "COALESCE(r.identity_key,er.identity_key,at.legacy_track_key) key,COALESCE(f.complete,0) complete "
            f"FROM album_provider_ids p JOIN {self.album_tracks_table} at ON at.album_id=p.album_id "
            f"LEFT JOIN {self.album_fetch_state_table} f ON f.album_id=p.album_id "
            "LEFT JOIN recordings r ON r.id=at.recording_id "
            "LEFT JOIN catalog_entries e ON e.id=at.entry_id "
            "LEFT JOIN recordings er ON er.id=e.recording_id "
            f"WHERE p.provider='youtube' AND p.provider_id IN ({placeholders}) "
            "AND (coalesce(f.complete,0)=0 OR at.is_full=1)"
            "), requested AS MATERIALIZED (SELECT DISTINCT key FROM membership WHERE key IS NOT NULL),"
            "exact_days AS MATERIALIZED (SELECT CAST(pe.identity_key AS TEXT) identity_key,pe.account_id,c.day "
            "FROM play_event_subjects pe JOIN play_event_calendar c ON c.event_id=pe.id "
            "WHERE pe.identity_key IN (SELECT key FROM requested)),"
            "exact AS (SELECT pe.identity_key,COUNT(*) n FROM play_event_subjects pe "
            "WHERE pe.identity_key IN (SELECT key FROM requested) GROUP BY pe.identity_key),"
            "fallback AS (SELECT d.identity_key,COUNT(*) n FROM listening_days d "
            "WHERE d.identity_key IN (SELECT key FROM requested) "
            "AND NOT EXISTS(SELECT 1 FROM exact_days pe "
            "WHERE pe.identity_key=d.identity_key AND pe.account_id=d.account_id AND pe.day=d.day) "
            "GROUP BY d.identity_key) "
            "SELECT m.browse,COUNT(m.key) tracks,MAX(m.complete) complete,"
            "SUM(COALESCE(e.n,0)+COALESCE(d.n,0)) plays FROM membership m "
            "LEFT JOIN exact e ON e.identity_key=m.key LEFT JOIN fallback d ON d.identity_key=m.key "
            "GROUP BY m.browse", browse_ids).fetchall()
        return {**{browse: {'plays': 0, 'tracks': 0, 'complete': False} for browse in browse_ids},
                **{row['browse']: {'plays': row['plays'], 'tracks': row['tracks'],
                                  'complete': bool(row['complete'])} for row in rows}}

    def _play_counts(self, keys):
        keys, counts = list(dict.fromkeys(keys)), {}
        for offset in range(0, len(keys), 500):
            page = keys[offset:offset + 500]
            counts.update(self.conn.execute('SELECT identity_key,COUNT(*) FROM listening_days '
                'WHERE identity_key IN (' + ','.join('?' for _ in page) + ') GROUP BY identity_key', page))
        return counts

    @snapshot_read
    def collection_album_inputs(self):
        rows = self.conn.execute(
            "SELECT a.id album_id,a.title album,t.identity_key key,a.artist artist,p.provider_id browse, "
            "       MIN(t.thumbnail) thumb, GROUP_CONCAT(DISTINCT pt.playlist_id) pls "
            f"FROM albums a JOIN {self.album_tracks_table} at ON at.album_id=a.id JOIN tracks t ON t.id=at.entry_id "
            "JOIN playlist_tracks pt ON pt.track_id=t.id JOIN album_primary_provider_ids p "
            "ON p.album_id=a.id AND p.provider='youtube' WHERE NULLIF(a.title,'') IS NOT NULL "
            "GROUP BY a.id,t.identity_key").fetchall()
        return {'tracks': [dict(row) for row in rows], 'plays': self._play_counts(row['key'] for row in rows)}

    @snapshot_read
    def collection_artist_inputs(self):
        rows = self.conn.execute(
            # Flattening the metadata-backed tracks view into this aggregate
            # can make SQLite scan recordings once for every artist credit.
            # Project the four required fields once before joining membership.
            # The occurrence view's computed entry key has no type affinity.
            # Give the materialized projection an integer key so SQLite can
            # index this join instead of scanning all album entries per track.
            f"WITH album_entries AS MATERIALIZED (SELECT CAST(entry_id AS INTEGER) entry_id,album_id "
            f"FROM {self.album_tracks_table} WHERE entry_id IS NOT NULL), "
            "library_tracks AS MATERIALIZED (SELECT id,artist,identity_key,thumbnail FROM tracks) "
            f"SELECT CASE WHEN (SELECT COUNT(*) FROM {self.entry_artists_table} x WHERE x.entry_id=t.id)=1 "
            "THEN t.artist ELSE ta.credit END artist,ta.artist_id,t.identity_key key,a.id album_id,ap.provider_id browse, "
            "       p.provider_id album_browse,MIN(NULLIF(t.thumbnail,'')) thumb, "
            "       GROUP_CONCAT(DISTINCT pt.playlist_id) pls "
            f"FROM library_tracks t JOIN {self.entry_artists_table} ta ON ta.entry_id=t.id LEFT JOIN artist_provider_ids ap "
            "ON ap.artist_id=ta.artist_id AND ap.provider='youtube' "
            "LEFT JOIN album_entries at ON at.entry_id=t.id LEFT JOIN albums a ON a.id=at.album_id "
            "LEFT JOIN album_primary_provider_ids p ON p.album_id=a.id AND p.provider='youtube' "
            "LEFT JOIN playlist_tracks pt ON pt.track_id=t.id "
            "WHERE t.artist<>'' AND (pt.track_id IS NOT NULL OR "
            f"EXISTS(SELECT 1 FROM {album_memberships.read_relation(self.conn)} m WHERE m.album_id=a.id)) "
            "GROUP BY ta.artist_id,t.identity_key,a.id").fetchall()
        saved = self.conn.execute(
                'SELECT DISTINCT a.id album_id,a.thumbnail,aa.artist_id,aa.credit artist,ap.provider_id browse '
                f'FROM albums a JOIN {album_memberships.read_relation(self.conn)} m ON m.album_id=a.id '
                'JOIN album_artists aa ON aa.album_id=a.id LEFT JOIN artist_provider_ids ap '
                "ON ap.artist_id=aa.artist_id AND ap.provider='youtube'").fetchall()
        return {'tracks': [dict(row) for row in rows], 'saved': [dict(row) for row in saved],
                'plays': self._play_counts(row['key'] for row in rows)}

    @snapshot_read
    def artist_album_inputs(self, artist, browse_id=None):
        aid = artist_catalog.lookup(self.conn,artist)
        if browse_id:
            row = self.conn.execute("SELECT artist_id FROM artist_provider_ids WHERE provider='youtube' AND provider_id=?", (browse_id,)).fetchone()
            if row:
                aid = row[0]
        if aid is None:
            return {'albums': [], 'singles': False, 'tracks': [], 'plays': {}}
        albums = self.conn.execute(
                "SELECT a.id,a.title album,a.year,a.thumbnail thumb,p.provider_id browse, "
                f"EXISTS(SELECT 1 FROM {album_memberships.read_relation(self.conn)} m WHERE m.album_id=a.id) saved "
                "FROM albums a JOIN album_primary_provider_ids p ON p.album_id=a.id AND p.provider='youtube' "
                f"WHERE EXISTS(SELECT 1 FROM {self.album_tracks_table} at JOIN tracks t ON t.id=at.entry_id "
                f"JOIN {self.entry_artists_table} ta ON ta.entry_id=t.id WHERE at.album_id=a.id AND ta.artist_id=?) "
                f"OR (EXISTS(SELECT 1 FROM {album_memberships.read_relation(self.conn)} m WHERE m.album_id=a.id) "
                "AND EXISTS(SELECT 1 FROM album_artists aa WHERE aa.album_id=a.id AND aa.artist_id=?))", (aid,aid)).fetchall()
        singles = self.conn.execute(
                f"SELECT 1 FROM tracks t JOIN {self.entry_artists_table} ta ON ta.entry_id=t.id WHERE ta.artist_id=? AND NULLIF(TRIM(t.album),'') IS NULL "
                f'AND NOT EXISTS(SELECT 1 FROM {self.album_tracks_table} at WHERE at.entry_id=t.id) LIMIT 1', (aid,)).fetchone()
        tracks = self.conn.execute(
                f"WITH album_entries AS MATERIALIZED (SELECT CAST(entry_id AS INTEGER) entry_id,album_id "
                f"FROM {self.album_tracks_table} WHERE entry_id IS NOT NULL) "
                "SELECT at.album_id,t.identity_key,MIN(NULLIF(t.thumbnail,'')) thumb, "
                "GROUP_CONCAT(DISTINCT pt.playlist_id) pls FROM tracks t LEFT JOIN album_entries at ON at.entry_id=t.id "
                f"JOIN {self.entry_artists_table} ta ON ta.entry_id=t.id LEFT JOIN playlist_tracks pt ON pt.track_id=t.id "
                "WHERE ta.artist_id=? AND (at.album_id IS NOT NULL OR NULLIF(TRIM(t.album),'') IS NULL) "
                "GROUP BY at.album_id,t.identity_key", (aid,)).fetchall()
        return {'albums': [dict(row) for row in albums], 'singles': singles is not None,
                'tracks': [dict(row) for row in tracks],
                'plays': self._play_counts(row['identity_key'] for row in tracks)}

    @synchronized
    def artist_browse_id(self, artist):
        """The unambiguous artist identity's YouTube channel/browse ID, or None."""
        aid = artist_catalog.lookup(self.conn,artist)
        r = self.conn.execute("SELECT provider_id b FROM artist_provider_ids WHERE artist_id=? AND provider='youtube'", (aid,)).fetchone()
        return r["b"] if r else None
