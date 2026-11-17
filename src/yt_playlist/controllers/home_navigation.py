"""Home format, prepared stories and their confirmed visibility commands."""
from yt_playlist.rec import story_rules as into_recently
from yt_playlist.rec.growth_access import has_access


class HomeNavigation:
    def __init__(self, settings, proposals, ledger, *, retained, stories, epoch, respond, refresh,
                 group_stories=into_recently.story_groups):
        self.settings, self.proposals, self.ledger = settings, proposals, ledger
        self.retained, self.stories, self.epoch = retained, stories, epoch
        self.respond, self.refresh = respond, refresh
        self.group_stories = group_stories

    def choose(self, preference, panel, growth):
        if preference is not None:
            if preference not in {'auto', 'albums', 'mixes', 'radio', 'both'}:
                return 400
            if not all(growth()['ready_formats'].values()):
                return 409
            self.settings.set_setting('home_format', preference)
        if panel in {'album-choices', 'directions', 'radio'}:
            if not self.retained(panel) and not has_access(growth(), panel):
                return 409
            self.settings.set_setting('home_last_panel', panel)
        return 204

    def story_query(self, now):
        cards, preparing = self.stories(now, self.epoch())
        groups = self.group_stories(cards)
        return {'cards': cards, 'groups': groups, 'spreads': into_recently.editorial_spreads(groups),
                'preparing': preparing}

    def story_impressions(self, subjects, now):
        payload = self.proposals.get_proposals(into_recently.SURFACE) or {}
        known = {card['subject'] for card in payload.get('cards', [])}
        self.ledger.record_story_views(known.intersection(subjects), now)

    def response(self, opportunity, option, kind, now):
        if kind not in {'visible', 'open', 'choice'}:
            return False
        self.respond(opportunity, kind, now, option_id=option, visible=[option] if kind == 'visible' else ())
        if kind == 'choice':
            self.refresh()
        return True
