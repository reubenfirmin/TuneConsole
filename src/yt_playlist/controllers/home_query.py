"""Assemble Home's navigation and the selected tab."""
from yt_playlist.rec import growth_access, home_preferences, onboarding


PANELS = ('unlocks', 'directions', 'radio', 'album-choices', 'tv', 'discover', 'rediscover', 'recent', 'notices')


class PanelLocked(Exception):
    pass


class HomeQuery:
    def __init__(self, settings, accounts, model, board, *, artists_required, setup_completed,
                 update, recap, spotify, radio, radio_tracks, revisit, access, earned, visit,
                 growth, albums, stories, preferred, tv_minutes, actions, sync, writable):
        self.settings, self.accounts, self.model, self.board = settings, accounts, model, board
        self.artists_required, self.setup_completed = artists_required, setup_completed
        self.update, self.recap, self.spotify, self.radio, self.radio_tracks = update, recap, spotify, radio, radio_tracks
        self.revisit, self.access, self.earned, self.visit = revisit, access, earned, visit
        self.growth, self.albums, self.stories, self.preferred = growth, albums, stories, preferred
        self.tv_minutes, self.actions, self.sync, self.writable = tv_minutes, actions, sync, writable

    def page(self, now, *, requested=None, fragment=False, launch=None, flash=None, toast=None, attempt=0):
        update, spotify = self.update(), self.spotify(status_only=True)
        if spotify and spotify.get('dismissed'):
            spotify = None
        admitted = set(self.earned()) | {'unlocks', 'notices'}

        def open_panel(panel):
            if panel not in admitted and self.access(panel):
                admitted.add(panel)
                admitted.update(growth_access.main_path_through(panel).intersection(PANELS))
            return panel in admitted

        preferred = self.preferred()
        panel = requested or ('radio' if launch else preferred)
        if requested is None and not launch and spotify and spotify.get('status') in ('done', 'error', 'preview'):
            panel = 'notices'
        if not open_panel(panel):
            if fragment:
                raise PanelLocked
            override = self.settings.get_setting('home_format')
            automatic = override not in {*home_preferences.PANELS, 'both'}
            panel = ('album-choices' if requested is None and automatic and panel == 'directions'
                     and open_panel('album-choices') else 'unlocks')
        artists_required = False
        getting_started = False
        if panel in ('unlocks', 'notices') and not admitted.intersection(home_preferences.PANELS.values()):
            artists_required = self.artists_required()
            getting_started = ((artists_required or not self.setup_completed())
                               and not any(open_panel(area) for area in home_preferences.PANELS.values()))
        if spotify and (panel == 'notices' or getting_started):
            spotify = self.spotify()
        growth = {'nodes': [], 'trails': [], 'ready_areas': {}, 'ready_formats': {}, 'vibes_ready': None}
        if panel in ('unlocks', 'notices'):
            growth = self.growth(now)
        radio = None
        content = {}
        if not getting_started:
            self.visit(panel)
            if panel == 'directions':
                content = self.board.feed(self.board.load(now), now, vibes_ready=None, lazy_tuning=True,
                                          record=self.writable(), path='/', attempt=attempt, complete_only=True)
                content['muted_count'] = len(self.model.muted_artists())
            if panel == 'radio':
                radio = self.radio(launch)
                content['radio_tracks'] = self.radio_tracks(radio['state']) if radio['state'].get('active') else []
            if panel == 'album-choices':
                content['album_board'] = self.albums(now)
            if panel == 'recent':
                content['story_board'] = self.stories(now)
            if panel == 'discover':
                content = self.board.discovery_query(None, now)
            if panel == 'rediscover':
                content = self.revisit(now)
        synced = self.settings.get_setting('last_sync_at') is not None
        reminders = {}
        nodes = {node['id']: node for node in growth['nodes']}
        for identity, snooze, name in (
                ('lastfm', onboarding.LASTFM_NUDGE_SNOOZE_S, 'lastfm_nudge'),
                ('takeout', onboarding.TAKEOUT_NAG_SNOOZE_S, 'takeout_nag')):
            reminders['show_' + name] = not getting_started and onboarding.integration_reminder_due(
                recommendation_at=nodes.get(identity, {}).get('recommendation_at'),
                dismissed_at=self.settings.get_setting(name + '_dismissed_at'), now=now, snooze=snooze)
        return {'growth': {**growth, 'open_areas': sorted(admitted)}, 'album_board': None, 'story_board': None,
            'home_default_panel': panel, 'radio_bootstrap': radio,
            'tv_max_minutes': self.tv_minutes() if panel == 'tv' else None,
            'actions': self.actions(now) if panel == 'notices' else [],
            'sync': self.sync(now),
            'flash': flash, 'toast': toast,
            'show_intro': synced and self.settings.get_setting('intro_dismissed') != '1',
            'show_welcome': getting_started and self.settings.get_setting('onboard_welcome_dismissed') != '1',
            **reminders,
            'backend_update': update, 'show_backend_update': update is not None,
            'show_identities_nudge': synced and len(self.accounts.get_accounts()) <= 1
                and self.settings.get_setting('identities_nudge_dismissed') != '1',
            'onboarding': getting_started, 'onboard_artists_required': artists_required,
            'recap': self.recap(now) if panel == 'notices' else None,
            'spotify_library_job': spotify, **content}
