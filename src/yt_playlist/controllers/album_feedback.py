"""Replace release judgments and coordinate the evidence-retirement fence."""
from contextlib import contextmanager, nullcontext
import json
import sqlite3

from yt_playlist.core.activity import WorkBudgetExceeded
from yt_playlist.rec import album_feedback_policy as policy


class AlbumFeedback:
    def __init__(self, repository, collection, fence, read_original):
        self.repository = repository
        self.collection = collection
        self.fence = fence
        self.read_original = read_original

    def _original(self, facts):
        if 'rows' in facts:
            return {row['source_browse_id']: {'kind': row['response'], 'at': row['created_at'],
                'opportunity_id': row['opportunity_id'], 'action_id': row['action_id']} for row in facts['rows']}
        raw = self.read_original(facts['plan']) if facts['selected'] and facts['plan'] is not None else (
            None if facts['selected'] else facts['raw'])
        return json.loads(raw or '{}')

    def original(self):
        return self._original(self.repository.original_facts())

    def merge_albums(self, target, retired, now):
        if not self.repository.uses_rows():
            return
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
            if self.repository.uses_rows():
                policy.validate_time(now)
                with self.repository.transaction():
                    account_id, active = self.repository.account(account_id)
                    policy.validate_account(account_id, active)
                    album_id, previous = self.repository.replacement_facts(browse, account_id)
                    policy.validate_replacement(previous, now)
                    action = policy.action_id(kind)
                    intents = policy.response_intents(previous, action, opportunity_id, browse, kind, now)
                    self.repository.save_response(album_id, account_id, browse, kind, now, opportunity_id, action, intents)
                return
            for _attempt in range(3):
                facts = self.repository.original_facts()
                original = self._original(facts)
                preferred = self.collection.preferred_album_ids(set(original) | {browse})
                current, intents = policy.replace_legacy(original, preferred, browse, kind, now, opportunity_id)
                if self.repository.save_legacy(facts['raw'], json.dumps(current), intents):
                    return
            raise WorkBudgetExceeded('Album feedback changed during replacement')

    def pending_responses(self, *, after, limit):
        rows = self.repository.pending_responses(after=after, limit=limit)
        return self.repository.pending_responses(after=0, limit=limit) if not rows and after else rows

    def acknowledge_response(self, action_id):
        return self.repository.acknowledge_response(action_id)
