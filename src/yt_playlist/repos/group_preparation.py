"""Bounded SQL inputs and checkpoint writes shared by group storage formats."""
from yt_playlist.core import content_work, group_preparation_state, prepared_inputs, profile_files
from yt_playlist.core.activity import WorkBudgetExceeded
from yt_playlist.core.worker_settings import number
from .base import Repo, synchronized


class GroupPreparationRepo(Repo):
    @synchronized
    def preparation_progress(self):
        prepared = self.conn.execute('SELECT COUNT(*) FROM prepared_recording_inputs '
            'WHERE library_present=1 AND recording_id IS NOT NULL').fetchone()[0]
        pending = content_work.count(self.conn)
        sources = {row[0]: row[1] for row in self.conn.execute(
            'SELECT scope,COUNT(*) FROM rec_musical_sources WHERE pending_position IS NOT NULL GROUP BY scope')}
        return {'prepared': prepared, 'pending_recordings': pending, 'sources': sources}

    @synchronized
    def serial(self):
        return number(self.conn, group_preparation_state.SERIAL)

    @synchronized
    def corpus_size(self, max_rows):
        size = self.conn.execute('SELECT COUNT(*) FROM prepared_recording_inputs '
                                 'WHERE library_present=1 AND recording_id IS NOT NULL').fetchone()[0]
        if size > max_rows:
            raise WorkBudgetExceeded('Musical group corpus exceeds admitted memory')
        return size

    @synchronized
    def corpus_page(self, after='', *, limit=128, max_bytes=8 * 1024 * 1024):
        size = self.conn.execute('SELECT COALESCE(SUM(bytes),0) FROM (SELECT '
            + prepared_inputs.byte_bound() + '+COALESCE(r.musical_profile_size*4,0) bytes '
            'FROM recordings r CROSS JOIN prepared_recording_inputs p ON r.id=p.recording_id '
            'WHERE p.library_present=1 AND r.identity_key>? '
            'ORDER BY r.identity_key LIMIT ?)', (after, limit)).fetchone()[0]
        if size > max_bytes:
            raise WorkBudgetExceeded('Musical group page exceeds admitted memory')
        rows = self.conn.execute('SELECT r.identity_key,p.*,r.musical_profile_version FROM recordings r '
            'CROSS JOIN prepared_recording_inputs p ON r.id=p.recording_id '
            'WHERE p.library_present=1 AND r.identity_key>? '
            'ORDER BY r.identity_key LIMIT ?', (after, limit)).fetchall()
        references = {row[0]: row for row in profile_files.reference_rows(
            self.conn, 'recording', [r['identity_key'] for r in rows if r['musical_profile_version'] is not None])}
        return [(r['identity_key'], prepared_inputs.payload(r), references.get(r['identity_key'])) for r in rows]

    @synchronized
    def checkpoint_preparation(self, generation, serial, now):
        with self.conn.atomic(join=True):
            group_preparation_state.checkpoint(self.conn, generation, serial, now)
