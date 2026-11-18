"""Compatibility bridge entry points for admitted observation commands."""
from yt_playlist.core.player_frames import PLAYBACK_KINDS, CURATION_KINDS, _curation_fields, _PAYLOAD_EXTRAS, _RATE_STATUS, _BODY_CAP, _extract_action_from_url
from yt_playlist.library.live_plays import resolve_identity
from yt_playlist.controllers.playback_observations import Observation
from yt_playlist.library.playback_composition import observations


def handle_player_event(ctx, msg, now):
    return observations(ctx.store, getattr(ctx, 'rec_worker', None)).observe('player', msg, now)


def persist_player_event(ctx, msg, now, ident, *, capture_id=None):
    return observations(ctx.store, getattr(ctx, 'rec_worker', None)).project(
        Observation('player', ident, msg, now), capture_id=capture_id)
