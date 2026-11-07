"""Bounded SQL inputs and checkpoint writes shared by group storage formats."""
from yt_playlist.core import group_preparation_state, prepared_inputs, profile_files
from yt_playlist.core.activity import WorkBudgetExceeded
from .base import Repo, synchronized


class GroupPreparationRepo(Repo):
    @synchronized
    def preparation_progress(self):
        prepared = self.conn.execute('SELECT COUNT(*) FROM prepared_recording_inputs '
            'WHERE library_present=1').fetchone()[0]
        pending = self.conn.execute('SELECT COUNT(*) FROM rec_content_pending').fetchone()[0]
        sources = {row[0]: row[1] for row in self.conn.execute(
            'SELECT s.scope,COUNT(*) FROM rec_musical_pending p '
            'JOIN rec_musical_sources s USING(source_key) GROUP BY s.scope')}
        return {'prepared': prepared, 'pending_recordings': pending, 'sources': sources}

    @synchronized
    def serial(self):
        return self.conn.execute('SELECT serial FROM rec_group_clock WHERE id=1').fetchone()[0]

    @synchronized
    def corpus_size(self, max_rows):
        size = self.conn.execute('SELECT COUNT(*) FROM prepared_recording_inputs '
                                 'WHERE library_present=1').fetchone()[0]
        if size > max_rows:
            raise WorkBudgetExceeded('Musical group corpus exceeds admitted memory')
        return size

    @synchronized
    def corpus_page(self, after='', *, limit=128, max_bytes=8 * 1024 * 1024):
        profile_size = (profile_files.recording_size_sql() if profile_files.available(self.conn)
                        else 'length(CAST(m.payload AS BLOB))')
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
        references = {row[0]: row for row in profile_files.reference_rows(
            self.conn, 'recording', [r['identity_key'] for r in rows if r['profile'] is not None])}
        return [(r['identity_key'], prepared_inputs.payload(r), references.get(r['identity_key'])) for r in rows]

    @synchronized
    def checkpoint_preparation(self, generation, serial, now):
        if group_preparation_state.available(self.conn):
            with self.conn.atomic(join=True):
                group_preparation_state.checkpoint(self.conn, generation, serial, now)
