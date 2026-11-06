"""SQL inventory and admission facts for retained group evidence."""
from yt_playlist.core import group_documents, group_evidence, mix_choices, recommendation_outputs, recommendation_references
from yt_playlist.core.activity import WorkBudgetExceeded
from yt_playlist.core.group_admission import writer
from yt_playlist.core.prepared_references import PREPARED
from yt_playlist.repos.base import Repo, lookup_read, snapshot_read, synchronized


class GroupEvidenceRepo(Repo):
    @synchronized
    def replace_prepared(self, surface, data, at, *, historical=False, resolved_contexts=None):
        from yt_playlist.core.prepared_references import replace_prepared
        return replace_prepared(self._store, surface, data, at,
            historical=historical, resolved_contexts=resolved_contexts)

    def retain(self, kind, identity, value, at, *, strict, recommendations):
        with writer(self._store) as conn:
            changed = group_evidence.retain(conn, kind, identity, value, at, strict=strict)
            if recommendations:
                recommendation_references.retain(conn, kind, identity, recommendations)
            return changed

    def release(self, kind, identity):
        from yt_playlist.repos import recommendation_publication
        with writer(self._store) as conn:
            group_evidence.release(conn, kind, identity)
            if recommendation_references.enabled(self._store):
                recommendation_publication.release(conn, kind, identity)

    def retire(self, kind, identities):
        from yt_playlist.repos import recommendation_publication
        with writer(self._store) as conn:
            if kind == 'offer':
                mix_choices.retire(conn, identities)
            for identity in identities:
                group_evidence.release(conn, kind, identity)
                if recommendation_references.enabled(self._store):
                    recommendation_publication.release(conn, kind, identity)

    def collect_mix_dictionary(self):
        with writer(self._store) as conn:
            return mix_choices.collect_dictionary(conn)

    def collect_unpublished_mixes(self):
        return mix_choices.collect_unpublished(self._store)

    @synchronized
    def output_collector(self, *, groups=False):
        directory = (group_documents if groups else recommendation_outputs).directory(self.conn)
        owners = tuple((table, prefix + '_filename') for table, _keys, prefix, _kind
                       in group_documents.OWNERS) if groups else None
        return recommendation_outputs.OrphanCollector(directory, owners=owners)

    @synchronized
    def collect_output_files(self, collector):
        return collector.step(self.conn)

    @lookup_read
    def has_owner(self, kind, identity):
        return self.conn.execute('SELECT 1 FROM group_evidence_owners WHERE kind=? AND external_key=?',
                                 (kind, identity)).fetchone() is not None

    @lookup_read
    def identities(self, kind, after, limit):
        return [row[0] for row in self.conn.execute(
            'SELECT external_key FROM group_evidence_owners WHERE kind=? AND external_key>? '
            'ORDER BY external_key LIMIT ?', (kind, after, limit))]

    @snapshot_read
    def next_prepared(self, after, max_bytes):
        row = self.conn.execute('SELECT surface,length(CAST(payload AS BLOB)) FROM rec_proposals '
            'WHERE surface>? AND (surface IN (' + ','.join('?' for _ in PREPARED) + ") OR surface LIKE ? ESCAPE '\\') "
            'ORDER BY surface LIMIT 1', (after, *sorted(PREPARED), r'model\_pair:%')).fetchone()
        if row is None:
            return None
        surface, size = row
        if size > max_bytes:
            raise WorkBudgetExceeded('Historical prepared group evidence exceeds its admitted memory')
        payload, at = self.conn.execute('SELECT payload,built_at FROM rec_proposals WHERE surface=?', (surface,)).fetchone()
        return surface, size, payload, at

    @lookup_read
    def matches_prepared(self, surface, payload, at):
        return self.conn.execute('SELECT 1 FROM rec_proposals WHERE surface=? AND payload=? AND built_at IS ?',
                                 (surface, payload, at)).fetchone() is not None

    @lookup_read
    def roots_complete(self):
        return bool(self.conn.execute('SELECT roots_complete FROM group_retention_state WHERE id=1').fetchone()[0])

    @synchronized
    def complete_roots(self):
        if not self.conn.in_transaction:
            raise RuntimeError('Root inventory completion requires its publication transaction')
        self.conn.execute('UPDATE group_retention_state SET roots_complete=1 WHERE id=1')

    @synchronized
    def retain_unknown(self, kind, identity, raw, reason, at):
        group_evidence.retain_unknown_context(self.conn, kind, identity, raw, reason, at)
