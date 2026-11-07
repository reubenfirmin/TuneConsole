"""Immutable discovery checkpoint selections used by serving readers."""
from dataclasses import dataclass


NATIVE = {'fetch-history': 'discovery_fetch_history', 'artist-images': 'discovery_artist_image_state',
          'provider-refresh': 'provider_refresh_frontiers'}


@dataclass(frozen=True)
class Reader:
    name: str

    def table(self, suffix):
        return 'published_discovery_checkpoint_'+self.name.replace('-', '_')+'_'+suffix

    def selected(self, conn):
        if conn.execute("SELECT 1 FROM main.sqlite_schema WHERE type='table' AND name=?", (NATIVE[self.name],)).fetchone():
            return None
        if not conn.execute('SELECT 1 FROM main.sqlite_schema WHERE name=?', (self.table('head'),)).fetchone():
            return None
        row = conn.execute('SELECT revision FROM '+self.table('head')+' WHERE active=1').fetchone()
        return row[0] if row else None

    def read(self, conn, revision, source):
        return conn.execute('SELECT * FROM '+self.table('rows')+' WHERE stage=? AND source=?', (revision,source)).fetchone()


OWNERS = {name:Reader(name) for name in NATIVE}
