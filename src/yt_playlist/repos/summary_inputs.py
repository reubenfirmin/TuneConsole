"""Revision and publication facts for explicitly supplied summary dependencies."""
from yt_playlist.repos.base import Repo, synchronized


class SummaryInputsRepo(Repo):
    @synchronized
    def capture(self, dependencies, surfaces):
        revisions = self._store._snapshot_revisions.capture(dependencies)
        external = self.conn.execute('PRAGMA data_version').fetchone()[0]
        present = {row[0] for row in self.conn.execute(
            'SELECT surface FROM rec_proposals WHERE surface IN (' + ','.join('?' for _ in surfaces) + ')', surfaces)}
        if self._store._home_display_files:
            present.update(row[0] for row in self.conn.execute('SELECT surface FROM home_display_heads'))
        if self._store._recommendation_reports:
            present.update(row[0] for row in self.conn.execute('SELECT surface FROM recommendation_report_heads'))
        if self._store._listening_files and self.conn.execute('SELECT 1 FROM listening_output_heads h '
                'JOIN listening_output_files f ON f.publication_id=h.publication_id '
                "WHERE h.slot='current' AND f.surface='listening_analytics'").fetchone():
            present.add('listening_analytics')
        return revisions, external, present
