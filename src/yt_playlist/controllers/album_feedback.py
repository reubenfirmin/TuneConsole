"""Replace release judgments and coordinate the evidence-retirement fence."""
from contextlib import contextmanager, nullcontext
import sqlite3

from yt_playlist.core.activity import WorkBudgetExceeded
from yt_playlist.rec import album_feedback_policy as policy


class AlbumFeedback:
    def __init__(self, repository, fence):
        self.repository = repository
        self.fence = fence

    def original(self):
        return {row['source_browse_id']: {'kind': row['response'], 'at': row['created_at'],
                'opportunity_id': row['opportunity_id'], 'action_id': row['action_id']}
                for row in self.repository.original_rows()}

    def merge_albums(self, target, retired, now):
        with self.repository.transaction():
            rows = self.repository.merge_inputs(target, retired)
            self.repository.apply_merge(target, retired, policy.merged_withdrawals(rows, now))

    @contextmanager
    def protection(self, capture=None):
        with self.fence(wait_seconds=0):
            with capture.protection() if capture is not None else nullcontext(((), ())) as (artifacts, opportunities):
                try:
                    durable = self.repository.retained_opportunities()
                except sqlite3.OperationalError as exc:
                    if getattr(exc, 'sqlite_errorcode', 0) & 255 not in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED):
                        raise
                    raise WorkBudgetExceeded('Album feedback capture has not committed') from exc
                yield artifacts, durable | set(opportunities)

    def replace(self, browse, kind, now, opportunity_id=None, *, account_id=None):
        with self.fence():
            policy.validate_time(now)
            with self.repository.transaction():
                account_id, active = self.repository.account(account_id)
                policy.validate_account(account_id, active)
                album_id, previous = self.repository.replacement_facts(browse, account_id)
                policy.validate_replacement(previous, now)
                action = policy.action_id(kind)
                intents = policy.response_intents(previous, action, opportunity_id, browse, kind, now)
                self.repository.save_response(album_id, account_id, browse, kind, now, opportunity_id, action, intents)

    def pending_responses(self, *, after, limit):
        rows = self.repository.pending_responses(after=after, limit=limit)
        return self.repository.pending_responses(after=0, limit=limit) if not rows and after else rows

    def acknowledge_response(self, action_id):
        return self.repository.acknowledge_response(action_id)
