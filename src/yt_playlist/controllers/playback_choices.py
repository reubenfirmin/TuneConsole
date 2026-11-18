"""Attribute verified player events through explicit binding and response ports."""
import logging

from yt_playlist.core.activity import WorkClosing

logger = logging.getLogger(__name__)


class PlaybackChoices:
    def __init__(self, ledger, *, paused, opportunity, respond, horizon,
                 album_capture=None, source=None, recipe=None):
        self.ledger, self.paused = ledger, paused
        self.opportunity, self.respond, self.horizon = opportunity, respond, horizon
        self.album_capture, self.source, self.recipe = album_capture, source, recipe

    def album_context(self, message, now, *, account_id=None):
        from yt_playlist.core.playback_attribution import album_context
        try:
            pending, horizon = self.album_capture()
            return album_context(pending, message, now, account_id=account_id, horizon=horizon)
        except (ValueError, TypeError, KeyError):
            logger.warning('Invalid pending album playback context', exc_info=True)
        return None

    def record(self, message, now, *, account_id=None):
        """Record a verified new play; preserve original receipt identities."""
        def bind(opportunity, option):
            if account_id is not None and message.get('playlist') and message.get('videoId'):
                self.ledger.bind_choice_playback(account_id, message['playlist'], message['videoId'],
                    opportunity, option, now, self.horizon())
        try:
            pending = self.album_context(message, now, account_id=account_id)
            if pending:
                if account_id is not None and pending.get('account_id') is None:
                    self.ledger.bind_album_playback_context(pending, account_id)
                for kind in ('start', 'experience'):
                    self.respond(pending['opportunity_id'], kind, now, option_id=pending['browse'],
                        action_id=f"album:{pending.get('action_id', pending['opportunity_id'])}:{kind}:{message.get('videoId')}",
                        details={'video_id': message.get('videoId'), 'album_browse_id': pending['browse'],
                                 'playlist': message.get('playlist')})
                bind(pending['opportunity_id'], pending['browse'])
            source = self.source(message['playlist']) if message.get('playlist') else None
            if source and source['kind'] == 'playlist':
                recipe = self.recipe(message['playlist']) or {}
                if recipe.get('opportunity_id') and recipe.get('option_id'):
                    self.respond(recipe['opportunity_id'], 'start', now, option_id=recipe['option_id'],
                        action_id=f"mix:{source['id']}:start", details={'playlist_id': source['id']})
                    self.respond(recipe['opportunity_id'], 'experience', now, option_id=recipe['option_id'],
                        action_id=f"mix:{source['id']}:track:{message.get('videoId')}",
                        details={'playlist_id': source['id'], 'video_id': message.get('videoId')})
                    bind(recipe['opportunity_id'], recipe['option_id'])
        except WorkClosing:
            raise
        except Exception:
            logger.warning('Listening playback attribution failed', exc_info=True)

    def bind(self, account_id, playlist, video, opportunity, option, now):
        if self.paused() or account_id is None or not playlist or not video:
            return
        try:
            offer = self.opportunity(opportunity)
            if not offer or str(option) not in {str(row['id']) for row in offer['options']}:
                return
            self.ledger.bind_choice_playback(account_id, playlist, video, opportunity, option, now, self.horizon())
        except WorkClosing:
            raise
        except Exception:
            logger.warning('Listening playback binding failed', exc_info=True)

    def observe(self, account_id, playlist, video, kind, position, duration, event_id, now):
        if self.paused() or kind not in {'track_exit', 'ended', 'bye'} or not playlist or not video:
            return False
        try:
            binding = self.ledger.choice_playback(account_id, playlist, video, now, self.horizon())
            if not binding:
                return False
            return self.respond(binding['opportunity_id'], 'experience', now,
                option_id=binding['option_id'], action_id=f'player:{event_id}', details={
                    'player_event_id': event_id, 'video_id': video, 'playlist': playlist,
                    'account_id': account_id, 'kind': kind, 'position': position, 'duration': duration,
                    'start_at': binding['at']})
        except WorkClosing:
            raise
        except Exception:
            logger.warning('Listening outcome attribution failed', exc_info=True)
            return False

    def continue_playback(self, account_id, playlist, video, now):
        if self.paused():
            return
        try:
            previous = self.ledger.choice_playback(account_id, playlist, None, now, self.horizon())
            if not previous:
                return
            self.bind(account_id, playlist, video, previous['opportunity_id'], previous['option_id'], now)
            self.respond(previous['opportunity_id'], 'experience', now, option_id=previous['option_id'],
                action_id=f"continuation:{previous['opportunity_id']}:{video}",
                details={'video_id': video, 'playlist': playlist, 'continuation': True})
        except WorkClosing:
            raise
        except Exception:
            logger.warning('Listening continuation attribution failed', exc_info=True)
