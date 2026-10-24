"""Last-good prepared recommendations and pre-encoded snapshot publication.

The snapshot publisher owns the transaction for publish_prepared_* methods. Policy
rows belong to rec_ledger and must participate in that same outer transaction.
"""
import json

from yt_playlist.repos.base import Repo, synchronized


class RecProposalsRepo(Repo):
    @synchronized
    def publish_prepared_bundles(self, bundles_json, fresh_json, now) -> None:
        """Write already encoded output; the snapshot publisher owns validation and commit."""
        self.conn.executemany(
            "INSERT INTO rec_proposals(surface,payload,built_at) VALUES (?,?,?) "
            "ON CONFLICT(surface) DO UPDATE SET payload=excluded.payload,built_at=excluded.built_at",
            [("mode_bundles", bundles_json, now), ("fresh_pool", fresh_json, now)])

    @synchronized
    def publish_prepared_home(self, payload, now, *, surface="home_prepared") -> None:
        """Publish pre-encoded Home output; the snapshot publisher owns the transaction."""
        self.conn.execute(
            "INSERT INTO rec_proposals(surface,payload,built_at) VALUES (?,?,?) "
            "ON CONFLICT(surface) DO UPDATE SET payload=excluded.payload,built_at=excluded.built_at",
            (surface, payload, now))

    @synchronized
    def remember_home(self, payload, now) -> None:
        self.publish_prepared_home(payload, now, surface="home_tuned")
        self.conn.commit()

    @synchronized
    def put_proposals(self, surface, data, now=None) -> None:
        """Materialize a surface's proposals as JSON (the rec worker writes; routes read last-good)."""
        self.conn.execute(
            "INSERT INTO rec_proposals(surface, payload, built_at) VALUES (?,?,?) "
            "ON CONFLICT(surface) DO UPDATE SET payload=excluded.payload, built_at=excluded.built_at",
            (surface, json.dumps(data), now))
        self.conn.commit()

    def get_proposals(self, surface):
        """Last materialized proposals for a surface, or None if never built."""
        payload = self._proposal_json(surface)
        # Large disposable collections are decoded after releasing the serving connection.
        return json.loads(payload) if payload is not None else None

    @synchronized
    def _proposal_json(self, surface):
        from yt_playlist.core.db_revisions import keyed_reads

        with keyed_reads(self._store, "rec_proposals", (surface,)):
            row = self.conn.execute(
                "SELECT payload FROM rec_proposals WHERE surface=?", (surface,)).fetchone()
        return row["payload"] if row else None

