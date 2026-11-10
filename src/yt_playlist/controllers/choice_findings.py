"""Publish learned outputs in order, retaining unchanged effective interests."""


class ChoiceFindings:
    def __init__(self, proposals):
        self.proposals = proposals

    def publish(self, report, interests, now, *, bandit, group_bandit=None):
        previous = self.proposals.get_proposals('choice_interests')
        effective = lambda value: {key: item for key, item in value.items() if key not in {'at', 'watermark'}}
        self.proposals.put_proposals('listening_choices', report, now)
        self.proposals.put_proposals('choice_bandit', bandit, now)
        if previous is None or effective(previous) != effective(interests):
            self.proposals.put_proposals('choice_interests', interests, now)
        if group_bandit is not None:
            self.proposals.put_proposals('group_choice_bandit', group_bandit, now)
