"""Publish durable feedback intents and acknowledge only matching immutable evidence."""
from yt_playlist.core.action_errors import ActionInputError
from yt_playlist.core.activity import check_cancellable_work


class FeedbackPublication:
    def __init__(self, repository, *, respond, evidence, paused, cursor, advance):
        self.repository, self.respond, self.evidence = repository, respond, evidence
        self.paused, self.cursor, self.advance = paused, cursor, advance

    def replace(self, browse, kind, now, opportunity_id=None, *, account_id=None):
        if kind not in {'more', 'known', 'reject', 'clear'} or not browse:
            raise ActionInputError('album_feedback_required')
        self.repository.replace(browse, kind, now, opportunity_id, account_id=account_id)

    def drain(self, *, limit):
        rows = self.repository.pending_responses(after=self.cursor(), limit=limit)
        completed = 0
        for row in rows:
            check_cancellable_work()
            if self.paused():
                break
            self.advance(row['id'])
            details = ({'kind': row['feedback_kind']} if row['kind'] == 'feedback'
                       else {'action_id': row['target_action_id']})
            result = self.respond(row['opportunity_id'], row['kind'], row['observed_at'],
                option_id=row['option_id'], action_id=row['action_id'], details=details, synchronous=True)
            if result is None:
                break
            stored = self.evidence('response', row['action_id'])
            if stored is not None:
                if (stored['opportunity_id'], stored['kind'], stored['option_id'], stored['details']) != (
                        row['opportunity_id'], row['kind'], row['option_id'], details):
                    raise ValueError('Published album feedback differs from its durable intent')
                if row['kind'] == 'feedback' and stored['at'] != row['observed_at']:
                    raise ValueError('Published album feedback differs from its durable intent')
                self.repository.acknowledge_response(row['action_id'])
                completed += 1
        return completed
