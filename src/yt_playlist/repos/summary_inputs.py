"""Revision and publication facts for explicitly supplied summary dependencies."""
from yt_playlist.core import home_outputs, library_taste_summary, listening_outputs, recommendation_reports
from yt_playlist.repos.base import Repo, synchronized


class SummaryInputsRepo(Repo):
    @synchronized
    def capture(self, dependencies, surfaces):
        revisions = self._store._snapshot_revisions.capture(dependencies)
        external = self.conn.execute('PRAGMA data_version').fetchone()[0]
        present = {surface for owner in (home_outputs, recommendation_reports) for surface in surfaces
                   if surface in owner.SURFACES and owner.present(self.conn, surface)}
        if library_taste_summary.KEY in surfaces and library_taste_summary.present(self.conn):
            present.add(library_taste_summary.KEY)
        if 'listening_analytics' in surfaces and listening_outputs.reference(self.conn, 'listening_analytics', ''):
            present.add('listening_analytics')
        return revisions, external, present
