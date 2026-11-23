"""OverlapRepo: the Cleanup page's dismissal state: suppressed overlap pairs, overlap-ignored
playlists, kept pairs, plus the category-scoped cleanup ignores (per-playlist empty/tiny dismissals
and per-merge dismissals)."""
import json
from yt_playlist.core import playlist_scope_state as scope_state
from yt_playlist.core.playlist_scopes import ensure
from yt_playlist.repos.base import Repo, synchronized
from yt_playlist.repos.models import CleanupPreferences


_SUPPRESS = '''INSERT INTO playlist_overlap_decisions
    (first_playlist_scope_id,second_playlist_scope_id,suppressed_at) VALUES (?,?,?)
    ON CONFLICT(first_playlist_scope_id,second_playlist_scope_id) DO UPDATE
    SET is_suppressed=1,suppressed_at=excluded.suppressed_at WHERE is_suppressed=0'''
_PAIR = 'first_playlist_scope_id=? AND second_playlist_scope_id=?'
_DISMISSAL_TIMES = "json_group_object(key,CASE WHEN value IS NOT NULL THEN json(printf('%!.26g',value)) END)"


class OverlapRepo(Repo):
    def _scope_pair(self, first, second):
        return tuple(sorted((ensure(self.conn, first), ensure(self.conn, second))))

    @synchronized
    def cleanup_preferences(self) -> CleanupPreferences:
        """Read preferences for a cleanup calculation on the caller's snapshot."""
        return CleanupPreferences(
            self.get_cleanup_ignored(), self.get_suppressed_overlaps(),
            self.get_overlap_ignored(), self.get_overlap_kept_pairs(),
            self.get_ignored_merges())

    @synchronized
    def suppress_overlap(self, ytm_a, ytm_b, now) -> None:
        with self.conn:
            self.conn.execute(_SUPPRESS, (*self._scope_pair(ytm_a, ytm_b), now))

    @synchronized
    def suppress_overlaps(self, pairs, now) -> None:
        """Publish one bulk dismissal, preserving timestamps of previously hidden pairs."""
        if not isinstance(pairs, (list, tuple)):
            return
        pairs = {tuple(pair) for pair in pairs
                 if isinstance(pair, (list, tuple)) and len(pair) == 2
                 and all(isinstance(ref, str) and ref for ref in pair)}
        if not pairs:
            return
        with self.conn:
            refs = {ref: ensure(self.conn, ref) for ref in sorted({ref for pair in pairs for ref in pair})}
            normalized = {tuple(sorted((refs[first], refs[second]))) for first, second in pairs}
            self.conn.executemany(_SUPPRESS, [(*pair, now) for pair in sorted(normalized)])

    @synchronized
    def unsuppress_overlap(self, ytm_a, ytm_b) -> None:
        with self.conn:
            pair = self._scope_pair(ytm_a, ytm_b)
            self.conn.execute(f'DELETE FROM playlist_overlap_decisions WHERE {_PAIR} AND is_kept=0', pair)
            self.conn.execute('UPDATE playlist_overlap_decisions SET is_suppressed=0,suppressed_at=NULL '
                              f'WHERE {_PAIR} AND is_suppressed=1', pair)

    @synchronized
    def get_suppressed_overlap_pairs(self) -> set:
        rows = self.conn.execute('SELECT first_playlist_scope_id,second_playlist_scope_id '
                                 'FROM playlist_overlap_decisions WHERE is_suppressed=1')
        return {frozenset(row) for row in rows}

    @synchronized
    def get_suppressed_overlaps(self) -> list[tuple]:
        rows = self.conn.execute('SELECT first_playlist_scope_id,second_playlist_scope_id,suppressed_at '
                                 'FROM playlist_overlap_decisions WHERE is_suppressed=1 ORDER BY suppressed_at DESC')
        return [tuple(row) for row in rows]

    @synchronized
    def ignore_overlap_playlist(self, ytm, now) -> None:
        with self.conn:
            if not self.conn.in_transaction:
                self.conn.execute('BEGIN IMMEDIATE')
            ytm = ensure(self.conn,ytm)
            scope_state.ignore_overlap(self.conn, ytm, now)

    @synchronized
    def unignore_overlap_playlist(self, ytm) -> None:
        with self.conn:
            if not self.conn.in_transaction:
                self.conn.execute('BEGIN IMMEDIATE')
            ytm = ensure(self.conn,ytm)
            scope_state.unignore_overlap(self.conn, ytm)

    @synchronized
    def get_overlap_ignored(self) -> set:
        return scope_state.overlap_ignored(self.conn)

    @synchronized
    def keep_overlap_pair(self, ytm_a, ytm_b, now) -> None:
        with self.conn:
            self.conn.execute('''INSERT INTO playlist_overlap_decisions
                (first_playlist_scope_id,second_playlist_scope_id,is_suppressed,is_kept,kept_at) VALUES (?,?,0,1,?)
                ON CONFLICT(first_playlist_scope_id,second_playlist_scope_id) DO UPDATE
                SET is_kept=1,kept_at=excluded.kept_at WHERE is_kept=0''',
                (*self._scope_pair(ytm_a, ytm_b), now))

    @synchronized
    def get_overlap_kept_pairs(self) -> set:
        rows = self.conn.execute('SELECT first_playlist_scope_id,second_playlist_scope_id '
                                 'FROM playlist_overlap_decisions WHERE is_kept=1')
        return {frozenset(row) for row in rows}

    # --- category-scoped per-playlist ignores (Empty / Tiny sections) ---------------------------
    @synchronized
    def ignore_cleanup(self, ytm, category, now) -> None:
        """Dismiss one playlist from ONE cleanup category (e.g. 'this is empty, stop suggesting it').
        Scoped: it stays eligible in every other category."""
        with self.conn:
            scope = ensure(self.conn, ytm)
            self.conn.execute(f'''UPDATE playlist_scopes SET cleanup_dismissals=(
                SELECT {_DISMISSAL_TIMES} FROM (
                    SELECT key,value FROM json_each(cleanup_dismissals)
                    UNION ALL SELECT CAST(? AS TEXT),?
                )
            ) WHERE id=? AND ? IS NOT NULL
                AND NOT EXISTS (SELECT 1 FROM json_each(cleanup_dismissals) WHERE key=CAST(? AS TEXT))''',
                (category, now, scope, category, category))

    @synchronized
    def unignore_cleanup(self, ytm, category) -> None:
        with self.conn:
            scope = ensure(self.conn, ytm)
            self.conn.execute(f'''UPDATE playlist_scopes SET cleanup_dismissals=(
                SELECT {_DISMISSAL_TIMES} FROM json_each(cleanup_dismissals) WHERE key<>CAST(? AS TEXT)
            ) WHERE id=? AND EXISTS (SELECT 1 FROM json_each(cleanup_dismissals) WHERE key=CAST(? AS TEXT))''',
                (category, scope, category))

    @synchronized
    def get_cleanup_ignored(self) -> dict:
        """{category: set(ytm)} of per-playlist cleanup dismissals."""
        out: dict = {}
        for scope, category in self.conn.execute(
                'SELECT p.id,d.key FROM playlist_scopes p JOIN json_each(p.cleanup_dismissals) d'):
            out.setdefault(category, set()).add(scope)
        return out

    # --- per-merge ignores (Exact / Near-duplicate groups) ---------------------------------------
    # A changed member set has a new signature and produces a new suggestion.
    def _merge_dismissals(self):
        row = self.conn.execute('SELECT value FROM settings WHERE key=?', (scope_state.MERGE_DISMISSALS,)).fetchone()
        return json.loads(row[0]) if row else {}

    def _save_merge_dismissals(self, dismissals):
        self.conn.execute('INSERT INTO settings(key,value) VALUES (?,?) ON CONFLICT(key) DO UPDATE '
                          'SET value=excluded.value', (scope_state.MERGE_DISMISSALS, json.dumps(dismissals)))

    @synchronized
    def ignore_merge(self, signature, members, now) -> None:
        with self.conn.atomic(join=True):
            members = sorted({ensure(self.conn, member) for member in members})
            signature = '|'.join(members)
            dismissals = self._merge_dismissals()
            if signature not in dismissals:
                dismissals[signature] = {'members': members, 'created_at': now}
                self._save_merge_dismissals(dismissals)

    @synchronized
    def unignore_merge(self, signature) -> None:
        with self.conn.atomic(join=True):
            dismissals = self._merge_dismissals()
            if signature in dismissals:
                del dismissals[signature]
                self._save_merge_dismissals(dismissals)

    @synchronized
    def get_ignored_merge_sigs(self) -> set:
        """Just the signatures, for filtering groups out of the cleanup view and the home count."""
        return set(self._merge_dismissals())

    @synchronized
    def get_ignored_merges(self) -> list:
        """[{signature, members:[ytm]}] newest-first, for the 'Ignored cleanups' display + un-ignore."""
        dismissals = self._merge_dismissals()
        signatures = sorted(dismissals, key=lambda signature: (
            dismissals[signature]['created_at'] is not None, dismissals[signature]['created_at']), reverse=True)
        return [{'signature': signature, 'members': dismissals[signature]['members']} for signature in signatures]
