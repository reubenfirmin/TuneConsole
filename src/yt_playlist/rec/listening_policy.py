"""Interpretation and exposure rules for the personal listening portrait."""

PLAYER_KINDS = ('listen_start', 'tick', 'state', 'volume', 'track_exit', 'ended', 'bye')
FEEDBACK_KINDS = ('like', 'dislike', 'ban')
POSITIVE_FEEDBACK = 'like'
NEGATIVE_FEEDBACK = 'dislike'
IMPRESSION_COOLDOWN_SECONDS = 6 * 3600


def output_identity(surface):
    if surface in ('listening_analytics', 'listening_edition'):
        return surface, ''
    if surface.startswith('listening_chunk:'):
        key = surface.removeprefix('listening_chunk:')
        if len(key) == 24 and all(c in '0123456789abcdef' for c in key):
            return 'listening_chunk', key
        return None
    raise ValueError('Unknown listening output surface')
