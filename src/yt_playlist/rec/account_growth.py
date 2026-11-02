"""Account growth is a read-only projection of music, preparation and use.

No milestone-completion records are written. Disposable notification visibility
belongs to the browser; a successful listening choice comes from the existing
choice evidence projection. Serving this view never starts provider or model work.
"""
import json
import math

from yt_playlist.core import vibe_versions
from yt_playlist.providers import deepseek, enrichment, lastfm
from yt_playlist.rec import (artist_start, festival_layout, growth_readiness, home_preferences, home_prepared, home_vibe, into_recently,
                             onboarding_outputs, tv, vibe_preparation)
from yt_playlist.repos.enrichment_sql import pending_coverage

_SCOPE = ("WITH scope AS (SELECT e.recording_id FROM catalog_entries e "
          "JOIN library_memberships l ON l.entry_id=e.id UNION "
          "SELECT recording_id FROM discovery_memberships) ")

# Revisit is an early optional detour: a pool-only account may have nothing to
# revisit yet. Its absence must never block the listening formats.
MAIN_PATH = ('first', 'foundation', 'albums', 'discover', 'listening', 'radio', 'tv', 'vibes', 'recent')
PARENTS = dict(zip(MAIN_PATH[1:], MAIN_PATH)) | {
    'rediscover': 'listening', 'takeout': 'foundation', 'lastfm': 'foundation',
    'deepseek': 'recent', 'milestones': 'vibes'}


def _node(identity, label, *, ready=False, used=False, bonus=False, optional=False, started=False,
          detail='', href='', x=0, y=0):
    state = 'explored' if ready and used else 'ready' if ready else 'preparing' if started else 'untouched'
    return {'id': identity, 'label': label, 'ready': ready, 'usable': ready, 'used': used,
            'optional': optional or bonus, 'unlocked': False, 'state': state, 'bonus': bonus,
            'status': ('Done' if bonus else 'Explored') if state == 'explored' else
                      'Ready to try' if state == 'ready' else
                      'Preparing' if state == 'preparing' else 'Not started',
            'detail': detail, 'href': href, 'x': x, 'y': y}


def _connect_paths(nodes, *, resume=()):
    """Project one fixed journey; preparation ahead of it never skips a stop."""
    stops = {node['id']: node for node in nodes}
    previous = True
    historical = max((MAIN_PATH.index(identity) for identity in resume if identity in MAIN_PATH), default=-1)
    for index, identity in enumerate(MAIN_PATH):
        stop = stops[identity]
        stop['unlocked'] = index <= historical or bool(previous and stop['usable'])
        stop['ready'] = bool(stop['unlocked'] and stop['usable'])
        frontier = previous and not stop['unlocked']
        stop['state'] = ('explored' if stop['ready'] and stop['used'] else 'ready' if stop['ready']
                         else 'preparing' if stop['unlocked'] or frontier and (identity != 'first' or stop['state'] == 'preparing')
                         else 'untouched')
        previous = stop['unlocked']
    for identity in set(stops) - set(MAIN_PATH):
        stop = stops[identity]
        # Optional setup remains independently actionable; Revisit still needs
        # the main path to reach its early detour and its own usable output.
        stop['unlocked'] = bool(stop['bonus'] or stops[PARENTS[identity]]['unlocked'])
        stop['ready'] = bool(stop['unlocked'] and stop['usable'])
        if not stop['ready'] and not stop['bonus']:
            stop['state'] = 'untouched'
    for stop in nodes:
        stop['href'] = stop['href'] if (stop['ready'] or stop['bonus']
            or stop['id'] in MAIN_PATH and stop['unlocked']) else ''
        stop['status'] = (('Done' if stop['bonus'] else 'Explored') if stop['state'] == 'explored'
                          else 'Ready to try' if stop['ready'] else
                          'Preparing' if stop['state'] == 'preparing' else 'Not started')
    # Preparation changes lighting, never the illustrated route or its parent.
    for stop in nodes:
        layout = festival_layout.LANDMARKS[stop['id']]
        stop.update(shape=layout['shape'], route=layout['route'],
                    sign_x=layout['sign'][0], sign_y=layout['sign'][1],
                    x=layout['entrance'][0] / 15.36, y=layout['entrance'][1] / 10.24)
        if stop['id'] in PARENTS:
            stop['parent'] = PARENTS[stop['id']]


def view(store, now, *, mixes_ready=None, albums_ready=None, radio=None, collections=None):
    """Read a consistent local snapshot; optional prepared outputs come from Home."""
    with home_vibe.natural(), store.read_view() as reader:
        def read():
            return _view(reader, now, mixes_ready=mixes_ready, albums_ready=albums_ready,
                         radio=radio, collections=collections)
        if mixes_ready is not None or albums_ready is not None or radio is not None or collections is not None:
            return read()
        return reader.memoized_read(('growth-journey', 2, int(now // 60)), read)


def radio_access(store, now, *, radio):
    """Check the same journey through Radio without assembling later outputs.

    Prior TV/Vibes use still participates in earned access. Their current
    prepared menus cannot change readiness of an earlier stop on the path.
    """
    with home_vibe.natural(), store.read_view() as reader:
        return has_access(_view(reader, now, radio=radio, for_radio_access=True), 'radio')


def has_access(state, area):
    """Keep earned destinations open while their eligible output refreshes.

    Readiness still describes usable output and controls new-user progression.
    Historical use can retain access, but never asserts that output is ready.
    """
    if area == 'vibes':
        return state['vibes_ready'] or any(node['id'] == 'vibes' and node['unlocked']
                                         for node in state['nodes'])
    return state['ready_areas'].get(area, False) or any(
        node['unlocked'] and node['href'] == '/#'+area for node in state['nodes'])


def retained_access(store, area):
    """Answer only access already established by deliberate prior listening.

    Match the historical prefix in _connect_paths without evaluating current
    menus. Missing history still needs the ordinary readiness projection.
    """
    destination = {'directions': 'listening', 'album-choices': 'albums',
                   'radio': 'radio', 'tv': 'tv', 'discover': 'discover',
                   'vibes': 'vibes', 'recent': 'recent'}.get(area)
    if destination is None:
        return False
    with store.read_view() as reader:
        usage = reader.rec_proposals.choice_usage()
        episodes = usage.get('episodes') or {}
        resume = {identity for format, identity in
                  (('mixes', 'listening'), ('albums', 'albums'), ('radio', 'radio'))
                  if episodes.get(format)}
        if usage.get('vibe_episodes'):
            resume.add('vibes')
        if tv.draw_number(reader) > 0:
            resume.add('tv')
        return any(MAIN_PATH.index(destination) <= MAIN_PATH.index(identity) for identity in resume)


def vibe_access(store, now):
    """Resolve a saved Vibe without rebuilding menus for already earned access."""
    with store.read_view() as reader:
        if reader.rec_proposals.choice_usage().get('vibe_episodes'):
            return True
        return has_access(view(reader, now), 'vibes')


def _view(store, now, *, mixes_ready=None, albums_ready=None, radio=None, collections=None,
          for_radio_access=False):
    with store.read_view() as reader:
        # Read queue subjects directly. Expanding every catalog metadata field on
        # this frequently polled surface makes large libraries needlessly slow.
        coverage = reader.conn.execute(_SCOPE +
            'SELECT COUNT(*),COALESCE(SUM(r.first_enriched_at IS NOT NULL AND NOT ' +
            pending_coverage('r.id') + ') ,0) FROM scope s JOIN recordings r ON r.id=s.recording_id').fetchone()
        total, processed = coverage
        imported = reader.settings.get_setting('last_sync_at') is not None
        mixes = onboarding_outputs.prepared_mixes_ready(reader, now) if mixes_ready is None else mixes_ready
        albums = growth_readiness.albums(reader, now) if albums_ready is None else albums_ready
        # A radio menu is an actual prepared offer, not a fitted model or a key.
        if radio is None:
            try:
                radio = json.loads(reader.settings.get_setting('radio_seed_offer') or '{}')
            except (ValueError, TypeError):
                radio = {}
            seeds = radio.get('tracks') or radio.get('seeds') or []
            menu = radio.get('offer_id') or radio.get('menu_id')
            active = False
        else:
            seeds, menu = radio.get('seeds') or [], radio.get('seedMenu')
            active = radio.get('state', {}).get('active')
        from yt_playlist.rec.radio_candidates import eligible_seeds
        seeds = eligible_seeds(reader, seeds, now)
        radio_usable = bool(active or menu and len({track['key'] for track in seeds}) >= growth_readiness.MIN_RADIO_SEEDS)
        format_usage = reader.rec_proposals.choice_usage()
        usage = format_usage.get('episodes') or {}
        radio_ready = bool(radio_usable and (usage.get('radio') or total and processed * 10 >= total * 9))
        has_starts = bool(reader.conn.execute(
            "SELECT 1 FROM play_event_sources WHERE source='live' LIMIT 1").fetchone())
        playable_owned = reader.conn.execute('SELECT 1 FROM playable_library_tracks WHERE '
            + reader.rec_eligibility.track_length_clause() + ' LIMIT 1').fetchone()
        first_ready = bool(playable_owned
                           or onboarding_outputs.starter_album(reader) or artist_start.first_set(reader, now))
        # Read the same bounded, currently eligible outputs that each area serves.
        # A cache row or a fitted model alone does not open a destination.
        if collections is None:
            prepared = reader.rec_proposals.get_proposals(home_prepared.SURFACE) or {}
            collections = home_prepared.collections(reader, prepared, now)
        foundation_ready = growth_readiness.foundation(reader, now)
        discover_ready = growth_readiness.discover(collections)
        revisit_ready = growth_readiness.revisit(collections)
        stories = [] if for_radio_access else into_recently.load(reader, now)[0]
        groups = into_recently.story_groups(stories)
        recent_ready = (len(groups) >= growth_readiness.MIN_STORY_GROUPS
                        and all(any(card.get('songs') for card in group) for group in groups))
        tv_ready = not for_radio_access and len(tv.queue(reader, now)) >= tv.QUEUE_SIZE
        takeout = reader.conn.execute(
            "SELECT COUNT(DISTINCT e.id) FROM play_events e JOIN play_event_sources s ON s.event_id=e.id "
            "WHERE s.source='takeout' AND e.played_at IS NOT NULL AND e.video_id IS NOT NULL").fetchone()[0]
        takeout_started = bool(reader.conn.execute('SELECT 1 FROM takeout_imports LIMIT 1').fetchone())
        lastfm_config = reader.conn.execute(
            "SELECT status,fingerprint FROM enrichment_provider_config WHERE provider='lastfm'").fetchone()
        current_lastfm = enrichment.provider_configuration(reader, {'lastfm': lastfm})['lastfm']
        lastfm_available = current_lastfm['status'] == 'available'
        lastfm_done = reader.conn.execute(_SCOPE +
            "SELECT COUNT(*) FROM scope s WHERE EXISTS (SELECT 1 FROM enrichment_provider_state p "
            "WHERE p.recording_id=s.recording_id AND p.provider='lastfm' AND p.status IN ('found','no_match')) "
            "AND NOT EXISTS (SELECT 1 FROM enrichment_provider_jobs j "
            "WHERE j.recording_id=s.recording_id AND j.provider='lastfm')").fetchone()[0]
        lastfm_ready = bool(lastfm_available and total and lastfm_done * 10 >= total * 9
                            and lastfm_config and tuple(lastfm_config) == (
                                current_lastfm['status'], current_lastfm['fingerprint'])
                            and not reader.enrichment_state.provider_refresh_pending('lastfm'))
        prefix = vibe_preparation.serving_prefix()
        version = vibe_versions.expression(reader.conn, 'recording_vibes', 'v')
        evidence = reader.conn.execute(_SCOPE +
            "SELECT COUNT(DISTINCT CASE WHEN v.source IN ('audio_measurement','provider_measurement') "
            "THEN v.recording_id END), COUNT(DISTINCT CASE WHEN v.source IN ('artist_genre','artist','genre') "
            "THEN v.recording_id END) FROM recording_vibes v JOIN scope s ON s.recording_id=v.recording_id "
            "WHERE v.support>0 AND substr(" + version + ",1,?)=? AND length(" + version + ")>?",
            (len(prefix), prefix, len(prefix))).fetchone()
        measured, predicted = evidence
        vibes_ready = bool(not for_radio_access and measured + predicted and mixes
                           and growth_readiness.vibes(reader, now))
        deep = deepseek.settings(reader, now)
        descriptions, descriptions_pending = reader.album_descriptions.summary()
        explicit_home = reader.settings.get_setting('home_format') in {*home_preferences.PANELS, 'both', 'auto'}
        created = reader.conn.execute(
            "SELECT COUNT(DISTINCT playlist_scope_id) FROM playlists WHERE created_at_source='local'").fetchone()[0]
        likes = len(reader.rec_model.recent_liked_keys())
        achievements = [
            {'id': 'playlist', 'label': 'Make it yours', 'count': created, 'goal': 1,
             'detail': 'Save your first playlist from a set you enjoy.'},
            {'id': 'likes', 'label': 'A set of favorites', 'count': likes, 'goal': 12,
             'detail': 'Like twelve songs as you listen.'},
            {'id': 'audio', 'label': 'Hear the detail', 'count': measured, 'goal': 12,
             'detail': 'Prepare measured sound details for twelve songs.'},
        ]
        for achievement in achievements:
            achievement['complete'] = achievement['count'] >= achievement['goal']
        achievements_done = all(item['complete'] for item in achievements)

    nodes = [
        _node('first', 'First sounds', ready=first_ready, used=has_starts, started=bool(imported or total),
              detail='We import your YouTube Music playlists and saved albums to prepare your first songs. '
                     'If you have no collection, choose three artists so we can make a first set from their music.',
              x=6, y=75),
        _node('foundation', 'Your music', ready=foundation_ready, started=first_ready,
              detail='First we scan your YouTube Music collection. If it is empty, we use the artists you choose instead. '
                     'We then build an initial AI model that predicts your taste and find more music for you to explore.',
              x=21, y=62),
        _node('listening', 'Mix tapes', ready=mixes, used=bool(usage.get('mixes')), started=bool(total),
              detail='We group and rank songs from your collection and the music we have found for you, '
                     'then prepare four playable mix tapes. You can adjust a mix before playing it.',
              href='/#directions',
              x=25, y=38),
        _node('albums', 'Long play', ready=albums, used=bool(usage.get('albums')), started=radio_ready,
              detail='We use your taste model to recommend albums and load their track lists '
                     'so you can choose a record and play it from the beginning.',
              href='/#album-choices',
              x=84, y=46),
        _node('radio', 'On air', ready=radio_ready, used=bool(usage.get('radio')), started=bool(mixes),
              detail='We prepare starting songs using your taste model. Choose one to start a station; '
                     'your listening and ratings help select the songs that follow.',
              href='/#radio', x=56, y=34),
        _node('vibes', 'Vibes', ready=vibes_ready, used=bool(format_usage.get('vibe_episodes')),
              started=radio_ready, detail='We use sound analysis and artist and genre information to estimate which songs fit each mood. '
                     'We then prepare different mix tapes for the moods you can choose.',
              href='/#directions', x=73, y=16),
        _node('tv', 'TV', ready=tv_ready, used=tv.draw_number(reader) > 0, started=radio_ready,
              detail='We search YouTube for videos of recommended songs, check that they contain moving pictures, '
                     'and prepare a queue of music videos to play.',
              href='/#tv', x=7, y=26),
        _node('discover', 'Discover', ready=discover_ready, started=bool(mixes),
              detail='We use your taste model to rank songs, artists and albums outside your collection '
                     'and list the results for you to browse.',
              href='/#discover', x=44, y=53),
        _node('rediscover', 'Revisit', ready=revisit_ready, started=albums, optional=True,
              detail='We compare your saved playlists, albums and songs with your listening history '
                     'to find music you have not played recently.',
              href='/#rediscover', x=86, y=75),
        _node('recent', 'Into recently', ready=recent_ready, started=discover_ready,
              detail='We use your recent listening to identify artists and genres, then find Wikipedia articles '
                     'about them and pair those articles with songs you can play.',
              href='/#recent', x=30, y=72),
        _node('takeout', 'Your past listening', ready=bool(takeout), used=bool(takeout), bonus=True,
              started=takeout_started, detail=f'{takeout:,} past listens imported' if takeout else 'Import past YouTube Music listening from Google Takeout.',
              href='/setup?tab=import', x=54, y=79),
        _node('lastfm', 'Richer music details', ready=lastfm_ready, used=lastfm_ready, bonus=True,
              started=lastfm_available, detail=(f'{lastfm_done:,} of {total:,} songs checked' if lastfm_available else
              'Look up song and artist genre information with Last.fm.'), href='/setup?tab=enrichment', x=71, y=61),
        _node('deepseek', 'Stories behind the records', ready=bool(descriptions), used=bool(descriptions), bonus=True,
              started=deep['configured'] and deep['enabled'], detail=(f'{descriptions:,} album descriptions' if descriptions else
                  'Paused in Setup' if deep['configured'] and not deep['enabled'] else
                  'Preparing album descriptions' if descriptions_pending else
                  'Open an album to try descriptions' if deep['configured'] else 'Generate short AI album descriptions when you open a record.'),
              href='/setup?tab=enrichment', x=68, y=81),
        _node('milestones', 'Little victories', ready=achievements_done, used=achievements_done,
              bonus=True, started=any(item['count'] for item in achievements),
              detail='Save a playlist, like songs and prepare sound details to earn these optional achievements.'),
    ]
    # Confirmed prior use keeps access earned even when its current output is
    # temporarily unavailable. A remembered preference alone proves no unlock.
    # An incidental live play is not evidence of reaching First sounds. Its
    # playable starter remains the admission condition; deliberate later use
    # can still restore the preceding path.
    resume = {node['id'] for node in nodes if node['used'] and node['id'] in MAIN_PATH[1:]}
    _connect_paths(nodes, resume=resume)
    order = list(MAIN_PATH)
    order.insert(order.index('radio'), 'rediscover')
    nodes.sort(key=lambda node: order.index(node['id']) if node['id'] in order else len(order))
    stops = {node['id']: node for node in nodes}
    mixes, albums, radio_ready = (stops[identity]['ready'] for identity in ('listening', 'albums', 'radio'))
    vibes_ready = stops['vibes']['ready']
    return {'nodes': nodes, 'foundation_ready': foundation_ready, 'achievements': achievements,
            'vibes_ready': vibes_ready, 'health': {'total': total, 'processed': processed,
            'percent': math.floor(processed * 100 / total) if total else 0,
            'label': 'Preparing your music' if processed < total else 'Ready to keep growing' if total else 'Waiting for music',
            'measured': measured, 'predicted': predicted},
            'home_choice_available': bool(mixes and albums and radio_ready and not explicit_home),
            'ready_formats': {'mixes': mixes, 'albums': albums, 'radio': radio_ready},
            'ready_areas': {'directions': mixes, 'album-choices': albums, 'radio': radio_ready,
                            'tv': stops['tv']['ready'], 'discover': stops['discover']['ready'],
                            'rediscover': stops['rediscover']['ready'], 'recent': stops['recent']['ready']},
            'provider_status': {'lastfm': lastfm_config[0] if lastfm_config else None, 'deepseek': deep['status']}}
