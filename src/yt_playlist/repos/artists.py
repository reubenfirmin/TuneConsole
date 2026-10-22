"""Canonical artist references, ordered credits, and name lookup for display boundaries."""
from yt_playlist.core import artists
from yt_playlist.repos.base import Repo, synchronized


class ArtistRepo(Repo):
    @synchronized
    def resolve(self, value):
        return artists.lookup(self.conn,value)

    @synchronized
    def ensure_artist(self, name, *, provider=None, provider_id=None):
        with self.conn:
            return artists.ensure(self.conn,name,provider=provider,provider_id=provider_id)

    @synchronized
    def get_artist(self, value):
        aid = artists.lookup(self.conn,value)
        row = self.conn.execute('SELECT * FROM artists WHERE id=?', (aid,)).fetchone()
        if row is None:
            return None
        provider_ids = {r['provider']:r['provider_id'] for r in self.conn.execute(
            'SELECT * FROM artist_provider_ids WHERE artist_id=?', (aid,))}
        return {**dict(row),'provider_ids':provider_ids,'key':artists.token(aid)}

    @synchronized
    def key(self, value):
        aid = artists.lookup(self.conn,value)
        return artists.token(aid) if aid else None

    @synchronized
    def public_key(self, value):
        aid = artists.lookup(self.conn,value)
        return artists.public_key(self.conn,aid) if aid else value

    @synchronized
    def track_credits(self, entry_id):
        return [dict(r) for r in self.conn.execute(
            'SELECT ta.*,a.name,ap.provider_id browse_id FROM track_artists ta '
            'JOIN artists a ON a.id=ta.artist_id LEFT JOIN artist_provider_ids ap '
            "ON ap.artist_id=a.id AND ap.provider='youtube' WHERE entry_id=? ORDER BY position",
            (entry_id,))]

    @synchronized
    def track_keys(self, *, library=False):
        rows = self.conn.execute(
            'SELECT DISTINCT r.identity_key,ta.artist_id FROM recordings r '
            'JOIN catalog_entries e ON e.recording_id=r.id JOIN track_artists ta ON ta.entry_id=e.id '+
            ('JOIN library_memberships m ON m.entry_id=e.id' if library else ''))
        out = {}
        for key,aid in rows:
            out.setdefault(key,set()).add(artists.token(aid))
        return out
