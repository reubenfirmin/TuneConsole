"""Publish learned outputs in order, retaining unchanged effective interests."""

from yt_playlist.core.rec_model_policy import CHOICE_SUPPORT_SURFACE


class ChoiceFindings:
    def __init__(self, proposals):
        self.proposals = proposals

    def publish(self, report, interests, now, *, bandit, group_bandit=None, listener_support=None):
        previous = self.proposals.get_proposals('choice_interests')
        effective = lambda value: {key: item for key, item in value.items() if key not in {'at', 'watermark'}}
        self.proposals.put_proposals('listening_choices', report, now)
        self.proposals.put_proposals('choice_bandit', bandit, now)
        if previous is None or effective(previous) != effective(interests):
            self.proposals.put_proposals('choice_interests', interests, now)
        if group_bandit is not None:
            self.proposals.put_proposals('group_choice_bandit', group_bandit, now)
        if listener_support is not None:
            surface = CHOICE_SUPPORT_SURFACE
            previous = self.proposals.proposal_fields(surface, ('version',)) or {}
            if listener_support['version'] != previous.get('version'):
                self.proposals.put_proposals(surface, listener_support, now)
