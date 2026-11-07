"""SQL inventory and admission facts for retained group evidence."""
from yt_playlist.core import group_evidence, mix_choices, recommendation_custody, recommendation_references
from yt_playlist.core.activity import WorkBudgetExceeded
from yt_playlist.core.group_admission import writer
from yt_playlist.core.prepared_references import PREPARED
from yt_playlist.repos.base import Repo, lookup_read, snapshot_read, synchronized


class GroupEvidenceRepo(Repo):
    @synchronized
    def replace_prepared(self, kind, surface, operations, generations, *, historical=False):
        with self.conn.atomic(join=True):
            return group_evidence.register_prepared(self.conn, kind, surface, operations, generations, historical=historical)

    def retain(self, owner, *, strict, recommendations):
        with writer(self._store) as conn:
            changed = group_evidence.retain_owner(conn, owner, strict=strict)
            if recommendations:
                recommendation_references.retain(conn, owner.kind, owner.identity, recommendations)
            return changed

    def release(self, kind, identity):
        with writer(self._store) as conn:
            group_evidence.release(conn, kind, identity)
            if recommendation_references.enabled(self._store):
                recommendation_custody.release(conn, kind, identity)

    def retire(self, kind, identities, *, mixes):
        with writer(self._store) as conn:
            if mixes:
                mix_choices.retire(conn, identities)
            for identity in identities:
                group_evidence.release(conn, kind, identity)
                if recommendation_references.enabled(self._store):
                    recommendation_custody.release(conn, kind, identity)

    def collect_mix_dictionary(self):
        with writer(self._store) as conn:
            return mix_choices.collect_dictionary(conn)

    @lookup_read
    def mix_menu_ids(self, after, limit):
        if not mix_choices.available(self.conn):
            return []
        return [row[0] for row in self.conn.execute('SELECT opportunity_id FROM mix_choice_menus '
            'WHERE opportunity_id>? ORDER BY opportunity_id LIMIT ?', (after, limit))]

    @synchronized
    def remove_unpublished_mixes(self, identities, limit):
        with self.conn.atomic(join=True):
            mix_choices.retire(self.conn, identities)
            mix_choices.collect_dictionary(self.conn, limit)

    @snapshot_read
    def protected_output_files(self, names, owners):
        if self.conn.in_transaction:
            raise ValueError('Prepared output collection requires an idle connection')
        owned = set()
        if names:
            marks = ','.join('?' for _ in names)
            for owner, column in owners:
                if self.conn.execute("SELECT 1 FROM main.sqlite_schema WHERE type='table' AND name=?", (owner,)).fetchone():
                    owned.update(row[0] for row in self.conn.execute(
                        f'SELECT DISTINCT {column} FROM {owner} WHERE {column} IN ({marks})', names))
        return owned

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
        with self.conn.atomic(join=True):
            self.conn.execute('UPDATE group_retention_state SET roots_complete=1 WHERE id=1')

    @synchronized
    def retain_unknown(self, kind, identity, raw, reason, at):
        with self.conn.atomic(join=True):
            group_evidence.retain_unknown_context(self.conn, kind, identity, raw, reason, at)
