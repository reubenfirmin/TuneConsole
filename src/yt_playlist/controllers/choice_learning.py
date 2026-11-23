"""Verify offer admission before acquiring descriptors for detached learning."""
from yt_playlist.rec import choice_bandit, group_learning


class ChoiceLearning:
    def __init__(self, *, expand, parameters, modes, axes, rating_advances):
        self.expand, self.parameters, self.modes, self.axes = expand, parameters, modes, axes
        self.rating_advances = rating_advances

    def capture(self, offers, responses, now, *, groups=False):
        params = self.parameters()
        excluded = dict.fromkeys(('unseen', 'future_context', 'missing_context',
            *(('unmapped_definition', 'legacy_ambiguous') if groups else ('unmapped_mode', 'drifted_mode'))), 0)
        decisions = list(choice_bandit.admitted_decisions(offers, responses, now, excluded, params=params))
        admitted = {oid: offer for oid, offer, *_ in decisions}
        expanded = {oid: self.expand(offer['options']) for oid, offer in admitted.items()}
        exit_ids = {event['details']['player_event_id'] for event in responses if event['opportunity_id'] in admitted
                    and event['kind'] == 'experience' and event['details'].get('kind') == 'track_exit'
                    and 'player_event_id' in event['details']}
        observed = list(choice_bandit.observations(decisions, excluded, params=params, expanded_options=expanded,
                                                  rating_advances=self.rating_advances(exit_ids)))
        return observed, params, excluded

    def groups(self, offers, responses, now, head):
        observed, params, excluded = self.capture(offers, responses, now, groups=True)
        return group_learning.compile(observed, now, head, params=params, excluded=excluded)

    def legacy(self, offers, responses, now):
        observed, params, excluded = self.capture(offers, responses, now)
        current = self.modes()
        mode_axes = self.axes(genre for mode in current for entry in mode.get('families') or []
                              for genre in [entry[0] if isinstance(entry, (list, tuple)) else entry])
        axes = self.axes(genre for row in observed for genre in row['option']['genres'])
        return choice_bandit.compile(observed, current, now, params=params, axes=axes,
                                     mode_axes=mode_axes, excluded=excluded)
