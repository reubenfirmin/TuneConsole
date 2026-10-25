"""Bounded attribution of raw player observations to confirmed context starts."""
import logging

from yt_playlist.core.activity import WorkClosing
from yt_playlist.rec import choice_evidence, rec_params

logger = logging.getLogger(__name__)


def bind(store, identity_id, playlist, video, opportunity, option, now):
    if identity_id is None or not playlist or not video:
        return
    try:
        offer = choice_evidence.opportunity(store, opportunity)
        if not offer or str(option) not in {str(row['id']) for row in offer['options']}:
            return
        store.rec_ledger.bind_choice_playback(identity_id, playlist, video, opportunity, option,
            now, rec_params.get_param(store, 'choice_attribution_h') * 3600)
    except WorkClosing:
        raise
    except Exception:
        logger.warning('Listening playback binding failed', exc_info=True)


def observe(store, identity_id, playlist, video, kind, position, duration, event_id, now):
    if kind not in {'track_exit', 'ended', 'bye'} or not playlist or not video:
        return False
    try:
        binding = store.rec_ledger.choice_playback(identity_id, playlist, video, now,
            rec_params.get_param(store, 'choice_attribution_h') * 3600)
        if not binding:
            return False
        return choice_evidence.safe_respond(store, binding['opportunity_id'], 'experience', now,
            option_id=binding['option_id'], action_id=f'player:{event_id}', details={
                'player_event_id': event_id, 'video_id': video, 'playlist': playlist,
                'identity_id': identity_id, 'kind': kind, 'position': position, 'duration': duration,
                'start_at': binding['at']})
    except WorkClosing:
        raise
    except Exception:
        logger.warning('Listening outcome attribution failed', exc_info=True)
        return False


def continue_playback(store, identity_id, playlist, video, now):
    """An observed Radio continuation stays in its last confirmed chosen context."""
    try:
        previous = store.rec_ledger.choice_playback(identity_id, playlist, None, now,
            rec_params.get_param(store, 'choice_attribution_h') * 3600)
        if not previous:
            return
        bind(store, identity_id, playlist, video, previous['opportunity_id'], previous['option_id'], now)
        choice_evidence.safe_respond(store, previous['opportunity_id'], 'experience', now,
            option_id=previous['option_id'],
            action_id=f"continuation:{previous['opportunity_id']}:{video}",
            details={'video_id': video, 'playlist': playlist, 'continuation': True})
    except WorkClosing:
        raise
    except Exception:
        logger.warning('Listening continuation attribution failed', exc_info=True)
