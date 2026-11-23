"""Attribute verified player events through explicit binding and response ports."""
import logging

from yt_playlist.core.activity import WorkClosing
from yt_playlist.core.history_policy import EXACT_TOLERANCE_S

logger = logging.getLogger(__name__)


class PlaybackChoices:
    def __init__(self, ledger, *, paused, opportunity, respond, horizon,
                 album_capture=None, source=None, recipe=None, browse_onset=None, propagate_errors=False):
        self.ledger, self.paused = ledger, paused
        self.opportunity, self.respond, self.horizon = opportunity, respond, horizon
        self.album_capture, self.source, self.recipe = album_capture, source, recipe
        self.browse_onset = browse_onset
        self.propagate_errors = propagate_errors

    def album_context(self, message, now, *, account_id=None):
        from yt_playlist.core.playback_attribution import album_context
        try:
            pending, horizon = self.album_capture()
            return album_context(pending, message, now, account_id=account_id, horizon=horizon)
        except (ValueError, TypeError, KeyError):
            logger.warning('Invalid pending album playback context', exc_info=True)
        return None

    def mix_context(self, playlist):
        source = self.source(playlist) if playlist else None
        if source and source['kind'] == 'playlist':
            recipe = self.recipe(playlist) or {}
            if recipe.get('opportunity_id') and recipe.get('option_id'):
                return source['id'], recipe
        return None

    def attributed(self, message, now, account_id, album):
        playlist, video = message.get('playlist'), message.get('videoId')
        if album:
            return True
        if not playlist or not video:
            return False
        try:
            mix = self.mix_context(playlist)
            if mix:
                offer = self.opportunity(mix[1]['opportunity_id'])
                if offer and 0 <= now - offer['at'] <= self.horizon():
                    return True
            return self.ledger.choice_playback(account_id, playlist, video, now, self.horizon()) is not None
        except WorkClosing:
            raise
        except Exception:
            logger.warning('Listening playback provenance lookup failed', exc_info=True)
            return False

    def started(self, account_id, playlist, video, opportunity, option, now, *, ids, details=None,
                binding_playlist=None):
        """Keep one verified playback onset with the provenance shared by its observations."""
        playlist = playlist or binding_playlist
        details = {**(details or {}), 'video_id': video, 'account_id': account_id, 'playlist': playlist}
        for kind, identity in zip(('start', 'experience'), ids, strict=True):
            self.respond(opportunity, kind, now, option_id=option, action_id=identity, details=details)
        self.bind(account_id, playlist, video, opportunity, option, now, response_id=ids[1])

    def record(self, message, now, *, account_id=None):
        """Record a verified new play; preserve original receipt identities."""
        playlist, video = message.get('playlist'), message.get('videoId')
        try:
            pending = self.album_context(message, now, account_id=account_id)
            if pending:
                if account_id is not None and pending.get('account_id') is None:
                    self.ledger.bind_album_playback_context(pending, account_id)
                opportunity, browse = pending['opportunity_id'], pending['browse']
                action = pending.get('action_id')
                prefix = action or opportunity
                self.started(account_id, playlist, video, opportunity, browse, now,
                    ids=(f'album:{prefix}:start:{video}', f'album:{prefix}:experience:{video}'),
                    details={'album_browse_id': browse, 'choice_action_id': action or f'{opportunity}:choice:{browse}'},
                    binding_playlist=pending['playlist'])
            elif (mix := self.mix_context(playlist)) is not None:
                pid, recipe = mix
                self.started(account_id, playlist, video, recipe['opportunity_id'], recipe['option_id'], now,
                    ids=(f'mix:{pid}:start', f'mix:{pid}:track:{video}'),
                    details={'playlist_id': pid, 'choice_action_id': f'generate:{playlist}:choice'})
        except WorkClosing:
            raise
        except Exception:
            if self.propagate_errors:
                raise
            logger.warning('Listening playback attribution failed', exc_info=True)

    def bind(self, account_id, playlist, video, opportunity, option, now, *, response_id=None):
        if self.paused() or account_id is None or not playlist or not video:
            return
        try:
            offer = self.opportunity(opportunity)
            if not offer or str(option) not in {str(row['id']) for row in offer['options']}:
                return
            options = {'response_id': response_id} if response_id is not None else {}
            self.ledger.bind_choice_playback(account_id, playlist, video, opportunity, option, now, self.horizon(), **options)
        except WorkClosing:
            raise
        except Exception:
            if self.propagate_errors:
                raise
            logger.warning('Listening playback binding failed', exc_info=True)

    def observe(self, account_id, playlist, video, kind, position, duration, event_id, now):
        if self.paused() or kind not in {'track_exit', 'ended', 'bye'} or not video:
            return False
        try:
            horizon = self.horizon()
            if playlist:
                binding = self.ledger.choice_playback(account_id, playlist, video, now, horizon)
            else:
                binding = self._browse_binding(account_id, video, now, horizon)
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
            if self.propagate_errors:
                raise
            logger.warning('Listening outcome attribution failed', exc_info=True)
            return False

    def _browse_binding(self, account, video, now, horizon):
        if self.browse_onset is None:
            return None
        onset = self.browse_onset(account, video, now - horizon, now)
        if onset is None:
            return None
        through = min(now, onset + EXACT_TOLERANCE_S)
        since = max(now - horizon, onset - EXACT_TOLERANCE_S)
        binding = self.ledger.choice_playback(account, None, video, through, through - since)
        if binding:
            offer = self.opportunity(binding['opportunity_id'])
            if offer and offer['format'] == 'albums':
                return binding
        return None

    def observe_like(self, account, playlist, video, event_id, requested_ms, now):
        requested_at = min(requested_ms / 1000, now) if requested_ms is not None else now
        return self.save_rating(account, playlist, video, f'player:{event_id}', requested_at, now)

    def save_rating(self, account, playlist, video, capture_id, requested_at, confirmed_at):
        """Credit a confirmed Like to the episode playing when it was requested."""
        if self.paused():
            return False
        if playlist:
            binding = self.ledger.choice_playback(account, playlist, video, requested_at, self.horizon())
        else:
            binding = self._browse_binding(account, video, requested_at, self.horizon())
        if binding is None:
            return False
        details = {'video_id': video, 'account_id': account, 'playlist': playlist, 'start_at': binding['at']}
        if origin := binding.get('choice_action_id'):
            details['choice_action_id'] = origin
        return self.respond(binding['opportunity_id'], 'save', confirmed_at, option_id=binding['option_id'],
                            action_id=f'rating:{capture_id}', details=details)

    def continue_playback(self, account_id, playlist, video, now):
        if self.paused() or not playlist:
            return
        try:
            previous = self.ledger.choice_playback(account_id, playlist, None, now, self.horizon())
            if not previous:
                return
            response_id = f"continuation:{previous['opportunity_id']}:{video}:{now}"
            self.bind(account_id, playlist, video, previous['opportunity_id'], previous['option_id'], now,
                      response_id=response_id)
            details = {'video_id': video, 'playlist': playlist, 'account_id': account_id,
                       'continuation': True, 'start_at': previous['at']}
            if origin := previous.get('choice_action_id'):
                details['choice_action_id'] = origin
            self.respond(previous['opportunity_id'], 'experience', now, option_id=previous['option_id'],
                         action_id=response_id, details=details)
        except WorkClosing:
            raise
        except Exception:
            logger.warning('Listening continuation attribution failed', exc_info=True)
