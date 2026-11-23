"""Onboarding gates over existing, currently eligible product output.

These are menu-capacity policies, not model-quality estimates. Preparation and
ranking remain with the feature owners; reading a gate never schedules work.
"""
from yt_playlist.controllers.growth_readiness import GrowthReadiness

from yt_playlist.rec import (album_choices, album_tuning, choice_evidence, fresh_pool, group_viz, home_cache, home_cards,
                             home_prepared, mix_tuning)

# Enough ranked discoveries to fill one ordinary four-card row. Selection still
# uses Fresh's existing diversity/exclusion rules, including for pool-only users.
POOL_TRACKS = 4 * home_cards.PROTO_SIZE
MIN_ALBUMS = 2
MIN_DISCOVER_TRACKS = 10
MIN_DISCOVER_ALBUMS = 3
MIN_RADIO_SEEDS = 4
MIN_REVISIT_TRACKS = home_cards.PROTO_SIZE
MIN_STORY_GROUPS = 3


def foundation(store, now):
    """Read prepared preference evidence and the current usable Fresh reserve."""
    def contexts():
        for surface, field in ((home_prepared.SURFACE, 'choice_context'),
                               (album_choices.SURFACE, 'context')):
            payload = (home_cache.read(store, surface, fields=(field,)) if surface in home_cache.SURFACES
                       else store.rec_proposals.proposal_fields(surface, (field,))) or {}
            try:
                context = choice_evidence.resolve_context(store, payload.get(field) or {})
            except ValueError:
                continue  # Collected evidence is pending preparation, not a broken Home.
            yield context
    def reserve():
        return fresh_pool.pick(store, now, size=POOL_TRACKS, minimum=1)
    def configuration():
        recording = store.rec_model.recording_state()
        return recording.generation, recording.fit_fingerprint
    command = GrowthReadiness(modes=lambda: (group_viz.prepared(store) or {}).get('groups', ()),
        contexts=contexts, configuration=configuration, reserve=reserve, album_menu=lambda: [])
    return store.memoized_read(('growth-foundation', 1, int(now // 60)), lambda: command.foundation(POOL_TRACKS))


def albums(store, now):
    def menu():
        payload = album_choices.load(store, now)
        if not payload:
            return []
        tuning = album_tuning.state(store, now)
        _, selected, _ = album_choices.selection(store, payload['items'], now,
            tuning['values'], home_cards._epoch(store, 'album_choices'), tuning['genres'])
        return selected
    return GrowthReadiness(modes=None, contexts=None, configuration=None, reserve=None, album_menu=menu).albums(MIN_ALBUMS)


def discover(collections):
    return (len(collections['discovery_tracks']) >= MIN_DISCOVER_TRACKS
            or len(collections['discovery_albums']) >= MIN_DISCOVER_ALBUMS)


def revisit(collections):
    return (len(collections['rediscover']) >= 2 or len(collections['rediscover_albums']) >= 3
            or len(collections['rediscover_tracks']) >= MIN_REVISIT_TRACKS)


def vibes(store, now):
    """Every displayed preset needs four useful, evidenced cards, not just Relax."""
    from yt_playlist.rec import vibe_readiness
    genres = store.rec_proposals.get_proposals('home_genre_request') or {}
    requested = genres if mix_tuning.genre_active(store, genres) else {}
    return vibe_readiness.ready(store, now, home_cards._epoch(store, 'cards'),
        include_genres=requested.get('include_genres', ()),
        avoid_genres=requested.get('avoid_genres', ()))
