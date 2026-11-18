"""Verify offer admission before acquiring descriptors for detached learning."""
from yt_playlist.rec import choice_bandit, group_learning


class ChoiceLearning:
    def __init__(self, *, expand, parameters, modes, axes):
        self.expand, self.parameters, self.modes, self.axes = expand, parameters, modes, axes

    def capture(self, offers, responses, now, *, groups=False):
        params = self.parameters()
        excluded = dict.fromkeys(('unseen', 'future_context', 'missing_context',
            *(('unmapped_definition', 'legacy_ambiguous') if groups else ('unmapped_mode', 'drifted_mode'))), 0)
        decisions = list(choice_bandit.admitted_decisions(offers, responses, now, excluded, params=params))
        admitted = {oid: offer for oid, offer, *_ in decisions}
        expanded = {oid: self.expand(offer['options']) for oid, offer in admitted.items()}
        observed = list(choice_bandit.observations(decisions, excluded, params=params, expanded_options=expanded))
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
