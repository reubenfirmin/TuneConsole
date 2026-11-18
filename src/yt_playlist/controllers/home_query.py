"""Assemble the complete Home document before presentation starts."""
from yt_playlist.rec import home_preferences, onboarding
from yt_playlist.rec.growth_access import has_access


class HomeQuery:
    def __init__(self, settings, accounts, model, board, *, artists_required, setup_completed,
                 update, recap, spotify, radio, radio_tracks, collections, album_ready, mix_ready,
                 growth, albums, stories, default_panel, tv_minutes, actions, sync, writable):
        self.settings, self.accounts, self.model, self.board = settings, accounts, model, board
        self.artists_required, self.setup_completed = artists_required, setup_completed
        self.update, self.recap, self.spotify, self.radio, self.radio_tracks = update, recap, spotify, radio, radio_tracks
        self.collections, self.album_ready, self.mix_ready, self.growth = collections, album_ready, mix_ready, growth
        self.albums, self.stories, self.default_panel = albums, stories, default_panel
        self.tv_minutes, self.actions, self.sync, self.writable = tv_minutes, actions, sync, writable

    def page(self, now, *, launch=None, flash=None, toast=None, attempt=0):
        artists_required = self.artists_required()
        update, recap, spotify = self.update(), self.recap(now), self.spotify()
        if spotify and spotify.get('dismissed'):
            spotify = None
        radio = self.radio(launch)
        tracks = self.radio_tracks(radio['state']) if radio['state'].get('active') else []
        payload = self.board.load(now)
        collections = self.collections(payload, now)
        albums_ready, mixes_ready = self.album_ready(now), self.mix_ready(now)
        growth = self.growth(now, mixes_ready=mixes_ready, albums_ready=albums_ready,
                             radio=radio, collections=collections)
        self.board.artist_images(collections, now)
        getting_started = (not any(has_access(growth, panel) for panel in home_preferences.PANELS.values())
                           and (artists_required or not self.setup_completed()))
        feed = self.board.feed(payload, now, vibes_ready=growth['vibes_ready'],
                               record=self.writable() and growth['ready_formats']['mixes'], path='/', attempt=attempt)
        albums = self.albums(now) if not getting_started and has_access(growth, 'album-choices') else None
        stories = self.stories(now) if not getting_started and has_access(growth, 'recent') else None
        synced = self.settings.get_setting('last_sync_at') is not None
        reminders = {}
        nodes = {node['id']: node for node in growth['nodes']}
        for identity, snooze, name in (
                ('lastfm', onboarding.LASTFM_NUDGE_SNOOZE_S, 'lastfm_nudge'),
                ('takeout', onboarding.TAKEOUT_NAG_SNOOZE_S, 'takeout_nag')):
            reminders['show_' + name] = not getting_started and onboarding.integration_reminder_due(
                recommendation_at=nodes.get(identity, {}).get('recommendation_at'),
                dismissed_at=self.settings.get_setting(name + '_dismissed_at'), now=now, snooze=snooze)
        return {'growth': growth, 'album_board': albums, 'story_board': stories,
            'home_default_panel': self.default_panel(growth), 'radio_bootstrap': radio,
            'tv_max_minutes': self.tv_minutes(), 'radio_tracks': tracks,
            'actions': self.actions(now), 'sync': self.sync(now), 'muted_count': len(self.model.muted_artists()),
            **collections, 'flash': flash, 'toast': toast,
            'show_intro': synced and self.settings.get_setting('intro_dismissed') != '1',
            'show_welcome': getting_started and self.settings.get_setting('onboard_welcome_dismissed') != '1',
            **reminders,
            'backend_update': update, 'show_backend_update': update is not None,
            'show_identities_nudge': synced and len(self.accounts.get_accounts()) <= 1
                and self.settings.get_setting('identities_nudge_dismissed') != '1',
            'onboarding': getting_started, 'onboard_artists_required': artists_required,
            'recap': recap, 'spotify_library_job': spotify, 'discovery_busy': self.board.busy(), **feed}
