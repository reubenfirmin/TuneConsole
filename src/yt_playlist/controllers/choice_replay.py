"""Verify retained replay inputs before the detached chronological evaluation."""
from yt_playlist.core.activity import check_cancellable_work
from yt_playlist.core import mix_choice_policy as mix_choices
from yt_playlist.rec import choice_statistics


class ChoiceReplay:
    def __init__(self, ledger, *, resolve, learning, checkpoint, expand):
        self.ledger, self.resolve, self.learning = ledger, resolve, learning
        self.checkpoint, self.expand = checkpoint, expand

    def run(self, now, predictor=None):
        offers = {row['id']: row for row in self.ledger.records('opportunity')
                  if row['observed_at'] <= now and row['at'] <= now}
        events = sorted((row for row in self.ledger.records('response')
                         if row['observed_at'] <= now and row['at'] <= now),
                        key=lambda row: (row['observed_at'], row['id']))
        captured = {}
        for identity, offer in offers.items():
            check_cancellable_work()
            if (predictor and offer['format'] == 'mixes'
                    and getattr(predictor, 'mix_precision', None) != mix_choices.PRECISION):
                captured[identity] = (None, offer['options'], 'unsupported_precision')
                continue
            try:
                context = self.resolve({'artifact': offer['context']}) if predictor else self.learning(offer['context'])
            except ValueError:
                context = None
            reason = None
            if context is None:
                reason = 'released_context' if predictor and self.checkpoint(offer['context']) else 'missing_context'
            elif context.get('at', offer['at']) > offer['at']:
                reason = 'future_context'
            options = self.expand(offer['options']) if reason is None and offer['format'] == 'mixes' else offer['options']
            captured[identity] = (context, options, reason)
        return choice_statistics.replay_facts(offers, events, captured, now, predictor)
