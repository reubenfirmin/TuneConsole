"""Onboarding gates over existing, currently eligible product output.

These are menu-capacity policies, not model-quality estimates. Preparation and
ranking remain with the feature owners; reading a gate never schedules work.
"""
import math

from yt_playlist.rec import (album_choices, album_tuning, choice_evidence, fresh_pool, home_cards,
                             home_prepared, home_vibe, mix_tuning, musical_character, vibe_pools,
                             vibe_preparation)

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
    def read():
        taste = bool(store.modes.list_modes(active_only=True))
        for surface, field in ((home_prepared.SURFACE, 'choice_context'),
                               (album_choices.SURFACE, 'context')):
            payload = store.rec_proposals.get_proposals(surface) or {}
            try:
                context = choice_evidence.resolve_context(store, payload.get(field) or {})
            except ValueError:
                continue  # Collected evidence is pending preparation, not a broken Home.
            policy = context.get('policy') or {}
            if (policy.get('content_generation') != store.settings.get_setting('rec_content_generation')
                    or policy.get('collaborative_inputs') != store.settings.get_setting('rec_fit_inputs')):
                continue
            saved = (context.get('layers') or {}).get('saved') or {}
            taste = taste or any((value or {}).get('titles') for value in saved.values())
        tracks = fresh_pool.pick(store, now, size=POOL_TRACKS, minimum=1)
        ranked = {track['key'] for track in tracks
                  if type(track.get('score')) in (int, float) and math.isfinite(track['score'])}
        return bool(taste and len(ranked) >= POOL_TRACKS)
    return store.memoized_read(('growth-foundation', 1, int(now // 60)), read)


def albums(store, now):
    payload = album_choices.load(store, now)
    if not payload:
        return False
    tuning = album_tuning.state(store, now)
    menu, _ = album_choices.menu(album_choices.eligible(store, payload['items'], now),
        tuning['values'], home_cards._epoch(store, 'album_choices'), tuning['genres'], conn=store.conn)
    return len(menu) >= MIN_ALBUMS


def discover(collections):
    return (len(collections['discovery_tracks']) >= MIN_DISCOVER_TRACKS
            or len(collections['discovery_albums']) >= MIN_DISCOVER_ALBUMS)


def revisit(collections):
    return (len(collections['rediscover']) >= 2 or len(collections['rediscover_albums']) >= 3
            or len(collections['rediscover_tracks']) >= MIN_REVISIT_TRACKS)


def _supported(track, preset):
    profile = track.get('musical') or {}
    fit = (profile.get('vibes', {}).get('fits') or {}).get(preset) or {}
    support, score = fit.get('support'), fit.get('score')
    return (profile.get('version') == musical_character.VERSION
            and vibe_preparation.compatible_version(profile.get('vibes', {}).get('inputs_version'))
            and fit.get('source') not in (None, 'heuristic')
            and type(support) in (int, float) and math.isfinite(support) and support > 0
            and type(score) in (int, float) and math.isfinite(score) and 0 <= score <= 1)


def vibes(store, now):
    """Every displayed preset needs four useful, evidenced cards, not just Relax."""
    genres = store.rec_proposals.get_proposals('home_genre_request') or {}
    requested = genres if mix_tuning.genre_active(store, genres) else {}
    signatures = set()
    for preset in home_vibe.PRESETS:
        cards = vibe_pools.assemble(store, now, preset, home_cards._epoch(store, 'cards'),
            include_genres=requested.get('include_genres', ()),
            avoid_genres=requested.get('avoid_genres', ()))
        if len(cards) < 4 or any(sum(_supported(track, preset) for track in card['tracks']) < 4
                                for card in cards):
            return False
        signatures.add(tuple(tuple(track['key'] for track in card['tracks']) for card in cards))
    # Neutral/tied evidence that produces the same menu for every button does
    # not yet deliver the change the newly exposed controls promise.
    return len(signatures) > 1
