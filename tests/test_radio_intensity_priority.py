"""Requested intensity can change a stream's style without widening genre intent."""
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from tests.radio_candidate_support import intensity_inputs
from tests.radio_session_support import controlled_radio_checkpoints, radio_controller, play
from tests.store_fixture_support import bounded_consumer_schema
from yt_playlist.core import catalog
from yt_playlist.rec import embedding_vectors, musical_character, radio_tuning
from yt_playlist.rec.radio_candidates import CLUSTER_POOL_LIMIT, RadioCandidates
from yt_playlist.rec.radio_directions import RadioDirections
from yt_playlist.rec.radio_policy import RadioPolicy
from yt_playlist.web import radio_session
from yt_playlist.web.request_priority import RequestPriorityMiddleware
from yt_playlist.web.routes import radio_session as radio_routes
from fastapi import FastAPI
from fastapi.testclient import TestClient


@pytest.fixture(autouse=True)
def disable_periodic_library_sync():
    """These controller checks build their own app without background jobs."""


@pytest.fixture(scope='module')
def _pristine_current_schema():
    with bounded_consumer_schema((('electronic', 'Electronic'), ('ambient', 'Ambient'))) as schema:
        yield schema


@pytest.fixture
def intensity_controller(store, intensity_inputs, monkeypatch, controlled_radio_checkpoints):
    # Controller/persistence/HTTP behavior uses a real small database. Prepared
    # recommendation inputs are controlled below RadioCandidates, as above.
    # Four six-song directions need 24 quiet alternatives. The separate refill
    # contract above owns exhausting more than a complete 96-song lookahead.
    intensity_inputs.pools['9'][:] = intensity_inputs.pools['9'][:12 + 24]
    # Two loud Electronic styles leave 24 alternatives after initial playback;
    # the quiet Ambient style distinguishes retuning across the genre boundary.
    # Neighbour policy has its own nine-style cases above.
    for mode in list(intensity_inputs.pools):
        if mode not in {'1', '2', '9'}:
            del intensity_inputs.pools[mode]
            del intensity_inputs.modes[mode]
    tracks = [track for rows in intensity_inputs.pools.values() for track in rows]
    with store.conn:
        store.conn.executemany('INSERT INTO recordings(id,identity_key,title,artist,unresolved_genre) VALUES (?,?,?,?,?)',
            [(i, t['key'], t['title'], t['artist'], t['genre']) for i, t in enumerate(tracks, 1)])
        store.conn.executemany('INSERT INTO youtube_items(video_id,recording_id,title,artist) VALUES (?,?,?,?)',
            [(t['video_id'], i, t['title'], t['artist']) for i, t in enumerate(tracks, 1)])
        store.conn.executemany('INSERT INTO catalog_entries(id,recording_id,video_id) VALUES (?,?,?)',
            [(i, i, t['video_id']) for i, t in enumerate(tracks, 1)])
        store.conn.executemany("INSERT INTO library_memberships VALUES (?,'observed',1)",
            [(i,) for i in range(1, len(tracks) + 1)])
    with radio_controller(monkeypatch, store, tracks) as state:
        entries = {t['video_id']: i for i, t in enumerate(tracks, 1)}

        def known_track(conn, video, *args, **kwargs):
            assert conn is store.conn
            return entries[video]

        # Metadata ingestion is below these controller checks; its real atomic
        # publication contracts are covered in repos/test_confirmed_playlist.py.
        monkeypatch.setattr(catalog, 'upsert', Mock(spec=catalog.upsert, side_effect=known_track))

        def candidates(_store, *args, **kwargs):
            assert _store is store
            result = RadioCandidates(intensity_inputs.store, *args, **kwargs)
            # Candidate scoring is the lower-layer policy tested above. These
            # controller checks consume controlled, measured candidate results
            # and keep real direction composition, playback and persistence.
            if result.tuning['energy'] == 0 and result.tuning['theme'] != 1:
                offered = intensity_inputs.pools['9'][12:]
            else:
                offered = [track for track in tracks
                           if track['genre'] == 'Electronic']

            def next_track(history, *, excluded=(), **kwargs):
                used = {track['video_id'] for track in history} | set(excluded)
                track = next((track for track in offered if track['video_id'] not in used), None)
                if track is not None:
                    result.retain([track])
                return track

            result.next_track = Mock(spec=RadioCandidates.next_track, side_effect=next_track)
            result.direction_anchor = Mock(spec=RadioCandidates.direction_anchor,
                side_effect=lambda history, anchors, **kwargs: next_track(history + anchors, **kwargs))
            return result

        monkeypatch.setattr(radio_session, 'RadioCandidates', candidates)
        yield state


def candidate(store, **tuning):
    return RadioCandidates(store, 1000, {**radio_tuning.DEFAULTS, 'theme': 1, 'energy': 0, **tuning})


@pytest.mark.parametrize('theme', [0, .5, 1])
def test_intensity_escapes_styles_within_the_genre_constraint_and_relabels_each_stream(intensity_inputs, monkeypatch, request, theme):
    store = intensity_inputs.store
    pools, quiet = intensity_inputs.pools, intensity_inputs.quiet
    assert len(quiet) == 110 > CLUSTER_POOL_LIMIT
    seed = pools['1'][0]
    c = candidate(store, theme=theme, comfort=1)
    request.addfinalizer(c.close)
    assert '9' not in c.neighbours['1']
    assert all(t['key'] not in {q['key'] for q in quiet} for t in c.pools['9'])
    monkeypatch.setattr(embedding_vectors, 'load_content_vectors', lambda *a, **k: pytest.fail('full vector read'))
    reads, read = [], c._read_vectors
    def bounded(keys):
        reads.append(len(keys))
        assert len(keys) <= CLUSTER_POOL_LIMIT
        return read(keys)
    monkeypatch.setattr(c, '_read_vectors', bounded)
    directions = RadioDirections(c, seed)
    directions.update([seed], [], 0)
    assert len(directions.public([])) == 4
    for branch in directions.slots:
        assert len(branch['tracks']) == 6
        expected_intensity = .96 if theme == 1 else .04
        assert branch['cluster_id'] in ({'1', '2', '3', '4', '5'} if theme == 1 else {'9'})
        assert branch['label'] == ('Electronic' if theme == 1 else 'Ambient')
        assert all(musical_character.intensity(t) == expected_intensity for t in branch['tracks'])
        if theme == 1:
            assert all(t['genre'] == seed['genre'] for t in branch['tracks'])
        else:
            assert c.fits(branch['tracks'][0], seed)
    before = directions.public([])
    assert not directions.update([seed], [], 0)
    assert directions.public([]) == before
    assert max(reads) <= CLUSTER_POOL_LIMIT


def test_intensity_refills_the_suitable_style_beyond_one_lookahead(intensity_inputs, request):
    store = intensity_inputs.store
    pools, quiet = intensity_inputs.pools, intensity_inputs.quiet
    c, history = candidate(store, theme=.5), [pools['1'][0]]
    request.addfinalizer(c.close)
    for _ in quiet:
        track = c.next_track(history, target=history[0])
        assert track and musical_character.intensity(track) == .04
        history.append(track)
    assert {t['key'] for t in history[1:]} == {t['key'] for t in quiet}
    full_reads = [used for mid, limit, used in intensity_inputs.reads
                  if mid == '9' and limit == CLUSTER_POOL_LIMIT]
    assert len(full_reads) >= 2
    assert max(len(used) for used in full_reads) > CLUSTER_POOL_LIMIT


def test_equally_suitable_current_style_is_kept_and_midpoint_still_pins(intensity_inputs, request):
    store = intensity_inputs.store
    pools = intensity_inputs.pools
    seed = pools['1'][0]
    neutral = candidate(store, energy=.5)
    request.addfinalizer(neutral.close)
    assert neutral.cluster_for(neutral.next_track([seed])) == '1'
    local = pools['1'][1]
    intensity_inputs.profiles[local['key']]['dimensions']['intensity']['value'] = .04
    c = candidate(store, theme=.5)
    request.addfinalizer(c.close)
    assert c.next_track([seed])['key'] == local['key']
    # Once the current style has no remaining fit, only that stream changes.
    assert c.cluster_for(c.next_track([seed, local], target=seed)) == '9'


def test_explicit_genre_mute_cannot_be_escaped_for_intensity(intensity_inputs, request):
    store = intensity_inputs.store
    pools, quiet = intensity_inputs.pools, intensity_inputs.quiet
    c = candidate(store)
    request.addfinalizer(c.close)
    intensity_inputs.weights['genre:ambient'] = 0
    pick = c.next_track([pools['1'][0]])
    assert pick and pick['genre'] != 'Ambient'
    assert not c._allowed(quiet[0])


def test_unindexed_fully_themed_genre_pin_allows_style_switch_inside_genre(intensity_inputs, request):
    store, pools = intensity_inputs.store, intensity_inputs.pools
    intensity_inputs.indexed = False
    for mid, rows in pools.items():
        for track in rows:
            intensity_inputs.profiles[track['key']]['dimensions']['intensity']['value'] = (
                .04 if mid in ('5', '9') else .96)
    # Style 9 is also quiet, but its Ambient genre is outside this pinned request.
    c = candidate(store)
    request.addfinalizer(c.close)
    pick = c.next_track([pools['1'][0]])
    assert pick['genre'] == 'Electronic'
    assert c.cluster_for(pick) == '5'


@pytest.mark.parametrize('energy', [0, .5, 1])
@pytest.mark.parametrize('path', ['next', 'alternative', 'continuation'])
def test_indexed_fully_themed_pins_genre_across_all_selection_paths(intensity_inputs, request, energy, path):
    store = intensity_inputs.store
    pools, quiet = intensity_inputs.pools, intensity_inputs.quiet
    seed = pools['1'][0]
    c = candidate(store, energy=energy)
    request.addfinalizer(c.close)
    assert c.cluster_members
    if path == 'next':
        tracks = [c.next_track([seed], target=quiet[0], current=seed)]
    elif path == 'alternative':
        tracks = [c.direction_anchor([seed], [quiet[0]])]
    else:
        tracks = RadioDirections(c, seed).continue_sequence([seed], [quiet[0]], quiet[0], ())
    assert tracks and all(track and track['genre'] == seed['genre'] for track in tracks)
    assert not c.fits(quiet[0], seed)
    view, history = SimpleNamespace(candidates=c), SimpleNamespace(current=seed)
    assert RadioPolicy.genre_mismatch(view, history, quiet[0])
    assert all(not RadioPolicy.genre_mismatch(view, history, track) for track in tracks)


def test_indexed_genre_pin_follows_playback_instead_of_an_old_style_anchor(intensity_inputs, request):
    store = intensity_inputs.store
    pools, quiet = intensity_inputs.pools, intensity_inputs.quiet
    seed, playing = pools['1'][0], pools['7'][0]
    c = candidate(store, energy=.5)
    request.addfinalizer(c.close)
    tracks = [c.next_track([seed, playing, quiet[0]], target=seed, current=playing),
              c.direction_anchor([seed, playing], [seed])]
    tracks += RadioDirections(c, seed).continue_sequence([seed, playing], [quiet[0]], seed, ())
    assert tracks and all(track and track['genre'] == playing['genre'] for track in tracks)


def test_indexed_intensity_can_switch_styles_inside_the_pinned_genre(intensity_inputs, request):
    store, pools = intensity_inputs.store, intensity_inputs.pools
    for track in pools['5']:
        intensity_inputs.profiles[track['key']]['dimensions']['intensity']['value'] = .04
    c = candidate(store)
    request.addfinalizer(c.close)
    seed = pools['1'][0]
    pick = c.next_track([seed], current=seed)
    assert pick['genre'] == seed['genre']
    assert c.cluster_for(pick) == '5'
    assert musical_character.intensity(pick) == .04


@pytest.mark.parametrize('energy', [0, .5, 1])
def test_indexed_genre_pin_never_spills_on_exhaustion_or_unknown_genre(intensity_inputs, request, energy):
    store, pools = intensity_inputs.store, intensity_inputs.pools
    seed = pools['1'][0]
    c = candidate(store, energy=energy)
    request.addfinalizer(c.close)
    excluded = {track['video_id'] for rows in pools.values() for track in rows if track['genre'] == seed['genre']}
    assert c.next_track([seed], excluded=excluded) is None
    assert c.direction_anchor([seed], [], excluded=excluded) is None
    assert c.next_track([{**seed, 'genre': None}]) is None


@pytest.mark.parametrize('theme', [.5, 1])
def test_live_retune_respects_genre_without_restarting_current_playback(intensity_controller, intensity_inputs, theme):
    controller, remote, frames, _, _ = intensity_controller
    pools = intensity_inputs.pools
    assert controller.start(pools['1'][0]['video_id'])['ok']
    play(controller, remote.ids[0])
    playlist, before_frames = controller.session.playlist, list(frames)
    heard, playing = list(controller.session.heard), controller.session.observed_video
    assert controller.tune({**radio_tuning.DEFAULTS, 'theme': theme, 'energy': 0})['ok']
    # The disabled monitor normally refreshes playback before each advance.
    controller.listening.observe(controller.session)
    controller.advance(controller.session)
    assert not controller.status()['updating']
    assert controller.session.browser['playback_synced']
    assert controller.session.playlist == playlist
    assert controller.session.heard == heard
    assert controller.session.observed_video == playing
    assert frames == before_frames
    future = controller.session.tracks[controller.session.position + 1:]
    assert future and all(musical_character.intensity(t) == (.96 if theme == 1 else .04) for t in future)
    if theme == 1:
        assert all(t['genre'] == pools['1'][0]['genre'] for t in future)
    else:
        assert all(b['cluster_id'] == '9' for b in controller.session.directions.slots)
    play(controller, future[0]['video_id'])
    assert controller.session.observed_video == future[0]['video_id']
    assert not controller.session.rejected


@pytest.mark.parametrize('restored', [False, True])
def test_real_tuning_route_replans_measured_candidates_and_persists_controls(intensity_controller, intensity_inputs, monkeypatch, restored):
    controller, remote, frames, *_ = intensity_controller
    pools = intensity_inputs.pools
    # Controlled earned access, with the production access guard and route.
    controller.ctx.store.rec_proposals.put_proposals(
        'listening_choices', {'home_format': {'vibe_episodes': 1}}, 1234)
    assert controller.start(pools['1'][0]['video_id'])['ok']
    play(controller, remote.ids[0])
    if restored:
        controller.suspend()
        controller._persist(controller.session)
        controller = radio_session.PlaylistRadio(controller.ctx)
        controller.ctx.radio = controller
        assert controller.join()['active']
    history, playing = list(controller.session.heard), dict(controller.ctx.bridge.now_playing)
    navigations = list(frames)
    store = controller.ctx.store
    taste = (store.rec_model.recent_liked_with_ts(limit=200), store.rec_model.disliked_with_ts(),
             store.rec_model.get_weights(now=1234))
    app = FastAPI()
    app.include_router(radio_routes.build(controller.ctx))
    app.add_middleware(RequestPriorityMiddleware, priority=store.priority)
    planning_owners = []
    update = RadioDirections.update

    def update_with_request_priority(*args, **kwargs):
        planning_owners.append(store.priority._foreground)
        return update(*args, **kwargs)

    monkeypatch.setattr(RadioDirections, 'update', update_with_request_priority)
    menus = []
    with TestClient(app) as client:
        for energy in (0., 1.):
            values = {'comfort': 1., 'theme': .5, 'energy': energy}
            result = client.post('/radio/tune', json=values)
            assert result.status_code == 200, result.text
            result = result.json()
            assert result['ok'] and result['tuning'] == values
            future = [track for branch in result['directions'] for track in branch['tracks']]
            assert future and all(musical_character.intensity(t) == (.04 if energy == 0 else .96)
                                  for t in future)
            menus.append({t['video_id'] for t in future})
            assert client.get('/radio/status').json()['tuning'] == values
            assert store.priority._foreground == 0
            assert controller.persistence.load()['tuning'] == values
            controller.listening.observe(controller.session)
            controller.advance(controller.session)
            assert not controller.status()['updating']
            assert controller.session.heard == history
            assert controller.ctx.bridge.now_playing == playing
            assert frames == navigations
        assert menus[0].isdisjoint(menus[1])
    assert planning_owners and set(planning_owners) == {0}, 'Apply must not delay its own planner'
    assert (store.rec_model.recent_liked_with_ts(limit=200), store.rec_model.disliked_with_ts(),
            store.rec_model.get_weights(now=1234)) == taste
