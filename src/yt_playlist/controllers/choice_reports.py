"""Capture report facts, compile detached findings, then publish one coherent projection."""
from yt_playlist.rec import choice_statistics


class ChoiceReports:
    def __init__(self, ledger, *, parameters, album_ids, learners, publish):
        self.ledger, self.parameters, self.album_ids = ledger, parameters, album_ids
        self.learners, self.publish = learners, publish

    def compile(self, now, *, refresh=None):
        offers, responses = self.ledger.records('opportunity'), self.ledger.records('response')
        halflife, attribution = self.parameters()
        aliases = self.album_ids({str(option['id']) for row in offers
            if row['observed_at'] <= now and row['at'] <= now and row['format'] == 'albums'
            for option in row['options']})
        report, projection, admitted, eligible = choice_statistics.compile_facts(
            offers, responses, now, halflife=halflife, attribution=attribution, album_ids=aliases)
        with self.learners() as (commands, head):
            bandit = commands.legacy(admitted, eligible, now)
            group_bandit = commands.groups(admitted, eligible, now, head) if head else None
        report['bandit'] = bandit
        if refresh is not None:
            deadlines = [now + 900]
            deadlines.extend(row[field] for row in offers + responses
                for field in ('at', 'observed_at') if row[field] > now)
            deadlines.extend(state['expires_at'] for state in (bandit, group_bandit)
                if state and state.get('expires_at') is not None and state['expires_at'] > now)
            report['refresh'] = {**refresh, 'next_at': min(deadlines)}
        self.publish(report, projection, now, bandit=bandit, group_bandit=group_bandit)
        return report
