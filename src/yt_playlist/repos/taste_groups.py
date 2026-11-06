"""Storage for versioned groups, separate from the currently served legacy modes."""
import json
import time

from yt_playlist.core.activity import WorkBudgetExceeded
from yt_playlist.core import prepared_inputs, profile_files
from yt_playlist.repos.base import Repo, synchronized
from yt_playlist.repos import group_publication


class TasteGroupsRepo(Repo):
    staged_tables = group_publication.STAGED_TABLES

    @synchronized
    def leaf_members(self, generation):
        """Exact partition for one retained generation, without provenance or vectors."""
        return {row[0]: row[1] for row in self.conn.execute(
            'SELECT m.identity_key,d.version FROM rec_group_members m '
            'JOIN rec_group_definitions d USING(version) JOIN rec_group_publications p USING(generation) '
            'WHERE d.generation=? '
            "AND json_extract(d.payload,'$.leaf')=1", (generation,))}

    @synchronized
    def head_generation(self):
        row = self.conn.execute('SELECT generation FROM rec_group_head WHERE id=1').fetchone()
        return row[0] if row else None

    @synchronized
    def head_checkpoint(self):
        row = self.conn.execute('SELECT g.generation,g.serial,g.policy FROM rec_group_generations g '
            'JOIN rec_group_head h ON h.generation=g.generation WHERE h.id=1').fetchone()
        return dict(row) if row else None

    @synchronized
    def preparation_progress(self):
        # These are separate units: a source job may affect many recordings.
        prepared = self.conn.execute('SELECT COUNT(*) FROM prepared_recording_inputs '
            'WHERE library_present=1').fetchone()[0]
        pending_recordings = self.conn.execute('SELECT COUNT(*) FROM rec_content_pending').fetchone()[0]
        sources = {row[0]: row[1] for row in self.conn.execute(
            'SELECT s.scope,COUNT(*) FROM rec_musical_pending p '
            'JOIN rec_musical_sources s USING(source_key) GROUP BY s.scope')}
        return {'prepared': prepared, 'pending_recordings': pending_recordings, 'sources': sources}

    @synchronized
    def serial(self):
        return self.conn.execute('SELECT serial FROM rec_group_clock WHERE id=1').fetchone()[0]

    @synchronized
    def head(self, *, members=False):
        row = self.conn.execute('SELECT g.* FROM rec_group_generations g JOIN rec_group_head h '
                                'ON h.generation=g.generation JOIN rec_group_publications p '
                                'ON p.generation=g.generation WHERE h.id=1').fetchone()
        if row is None:
            return None
        result = dict(row)
        result['payload'] = json.loads(result['payload'])
        result['groups'] = self.definitions(result['generation'], members=members)
        return result

    @synchronized
    def definitions(self, generation, *, members=False):
        result = []
        for row in self.conn.execute('SELECT d.* FROM rec_group_definitions d '
                                     'JOIN rec_group_publications p USING(generation) '
                                     'WHERE d.generation=? ORDER BY d.rowid',
                                     (generation,)):
            group = {**json.loads(row['payload']), 'group_id': row['group_id'], 'version': row['version'],
                     'parent_version': row['parent_version']}
            if members:
                group['members'] = {r['identity_key']: (r['similarity'], r['support']) for r in
                    self.conn.execute('SELECT * FROM rec_group_members WHERE version=?', (row['version'],))}
            result.append(group)
        return result

    @synchronized
    def corpus_size(self, max_rows):
        size = self.conn.execute('SELECT COUNT(*) FROM prepared_recording_inputs '
                                 'WHERE library_present=1').fetchone()[0]
        if size > max_rows:
            raise WorkBudgetExceeded('Musical group corpus exceeds admitted memory')
        return size

    @synchronized
    def corpus_page(self, after='', *, limit=128, max_bytes=8 * 1024 * 1024):
        # Bound raw bytes before fetching/decoding. Callers project each page and
        # release provenance trees, retaining only compact per-recording features.
        profile_size = (profile_files.recording_size_sql() if profile_files.available(self.conn) else 'length(CAST(m.payload AS BLOB))')
        size = self.conn.execute('SELECT COALESCE(SUM(bytes),0) FROM (SELECT '
            + prepared_inputs.byte_bound() + '+COALESCE((' + profile_size + ')*4,0) bytes '
            'FROM prepared_recording_inputs p JOIN recordings r ON r.id=p.recording_id '
            'LEFT JOIN rec_musical_profiles m ON m.identity_key=r.identity_key '
            'WHERE p.library_present=1 AND r.identity_key>? '
            'ORDER BY r.identity_key LIMIT ?)', (after, limit)).fetchone()[0]
        if size > max_bytes:
            raise WorkBudgetExceeded('Musical group page exceeds admitted memory')
        rows = self.conn.execute('SELECT r.identity_key,p.*,m.payload profile FROM prepared_recording_inputs p '
            'JOIN recordings r ON r.id=p.recording_id LEFT JOIN rec_musical_profiles m ON m.identity_key=r.identity_key '
            'WHERE p.library_present=1 AND r.identity_key>? '
            'ORDER BY r.identity_key LIMIT ?', (after, limit)).fetchall()
        cache = profile_files.Cache()
        return [(r['identity_key'], prepared_inputs.payload(r), self._profile(r, cache))
                for r in rows]

    def _profile(self, row, cache):
        if row['profile'] is None:
            return None
        return json.loads(profile_files.read(self.conn, 'recording', row['identity_key'], cache=cache))

    @synchronized
    def lineage(self):
        return [{**dict(row), 'payload': json.loads(row['payload'])}
                for row in self.conn.execute('SELECT l.* FROM rec_group_lineage l '
                    'JOIN rec_group_definitions d ON d.version=l.target '
                    'JOIN rec_group_publications p USING(generation)')]

    @synchronized
    def successors(self, version, *, limit=512):
        rows = self.conn.execute('SELECT l.* FROM rec_group_lineage l '
                                 'JOIN rec_group_definitions d ON d.version=l.target '
                                 'JOIN rec_group_publications p USING(generation) WHERE l.source=? LIMIT ?',
                                 (version, limit + 1)).fetchall()
        if len(rows) > limit:
            raise WorkBudgetExceeded('Musical lineage exceeds learning work ceiling')
        return [{**dict(row), 'payload': json.loads(row['payload'])} for row in rows]

    @synchronized
    def legacy_versions(self):
        return {row[0] for row in self.conn.execute('SELECT d.version FROM rec_group_definitions d '
            'JOIN rec_group_identities i USING(group_id) JOIN rec_group_publications p USING(generation) '
            'WHERE i.legacy_mode_id IS NOT NULL')}

    @synchronized
    def definition(self, version, *, generation=None):
        row = self.conn.execute('SELECT d.*,g.created_at FROM rec_group_definitions d '
            'JOIN rec_group_generations g USING(generation) JOIN rec_group_publications p USING(generation) '
            'WHERE version=? AND (? IS NULL OR d.generation=?)', (version, generation, generation)).fetchone()
        return {**json.loads(row['payload']), **{key: row[key] for key in
                ('version', 'group_id', 'generation', 'parent_version', 'created_at')}} if row else None

    def stage(self, prepared):
        """Prepare a hidden generation in bounded transactions, yielding between batches."""
        return group_publication.stage(self, prepared)

    def cleanup_staged(self):
        return group_publication.cleanup(self)

    def abandon_staged(self, prepared):
        return group_publication.abandon(self, prepared)

    @synchronized
    def expired_builds(self):
        return self.conn.execute('SELECT 1 FROM rec_group_builds WHERE expires_at<? LIMIT 1',
                                 (time.time(),)).fetchone() is not None

    @synchronized
    def resumable_build(self, fingerprint, serial, policy, parent):
        row = self.conn.execute('SELECT g.generation,g.created_at FROM rec_group_builds b '
            'JOIN rec_group_generations g USING(generation) WHERE g.fingerprint=? AND g.serial=? '
            "AND g.policy=? AND json_extract(g.payload,'$._staging_parent') IS ? "
            "AND json_type(g.payload,'$._staging_digest')='text' ORDER BY g.created_at LIMIT 1",
            (fingerprint, serial, policy, parent)).fetchone()
        return dict(row) if row else None

    @synchronized
    def staged_at(self, generation):
        row = self.conn.execute('SELECT g.created_at FROM rec_group_generations g '
            'JOIN rec_group_builds b USING(generation) WHERE generation=?', (generation,)).fetchone()
        return row[0] if row else None

    @synchronized
    def finish_staged(self, generation, owner):
        """Only the guarded snapshot publisher may advance the last-good head."""
        group_publication.finish(self.conn, generation, owner)

    @synchronized
    def finish_preparation(self, generation, owner, serial, now):
        self.finish_staged(generation, owner)
        from yt_playlist.core import group_preparation_state
        if group_preparation_state.available(self.conn):
            group_preparation_state.checkpoint(self.conn, generation, serial, now)

    @synchronized
    def publish(self, prepared):
        """Atomic import for bounded callers; background preparation uses stage/finish_staged."""
        if not self.conn.in_transaction:
            raise RuntimeError('Group publication requires an outer transaction')
        generation = prepared['generation']
        self.conn.execute('INSERT INTO rec_group_generations VALUES (?,?,?,?,?,?)',
                          group_publication.generation_row(prepared))
        for group in prepared['groups']:
            self.conn.execute('INSERT OR IGNORE INTO rec_group_identities VALUES (?,?,?)',
                              (group['group_id'], group.get('legacy_mode_id'), prepared['at']))
            self.conn.execute('INSERT INTO rec_group_definitions VALUES (?,?,?,?,?)',
                              (group['version'], generation, group['group_id'], group['parent_version'],
                               group['payload']))
            self.conn.executemany('INSERT INTO rec_group_members VALUES (?,?,?,?)',
                                 [(group['version'], *member) for member in group['members']])
        self.conn.executemany('INSERT INTO rec_group_lineage VALUES (?,?,?,?)', prepared['lineage'])
        self.conn.executemany('INSERT INTO rec_group_unknown VALUES (?,?,?)',
                             [(generation, *row) for row in prepared['unknown']])
        self.conn.executemany('INSERT INTO rec_group_graph VALUES (?,?,?,?,?)',
                             [(generation, *row) for row in prepared['edges']])
        group_publication.finish(self.conn, generation)
