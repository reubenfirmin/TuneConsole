"""Requested intensity can change a stream's style without widening genre intent."""
import pytest

from tests.musical_support import prepare_vibes
from tests.test_radio_catalog import catalog_track
from tests.test_radio_genres import genre_pools
from tests.radio_session_support import poc, play
from yt_playlist.core import catalog
from yt_playlist.rec import embedding_vectors, musical_character, radio_tuning
from yt_playlist.rec.radio_candidates import CLUSTER_POOL_LIMIT, RadioCandidates
from yt_playlist.rec.radio_directions import RadioDirections
from yt_playlist.web import radio_session


@pytest.fixture
def distant_chill(store, genre_pools):
    pools = genre_pools
    # A complete ordinary pool of unsuitable music hides the useful tail.
    for i in range(110):
        pools['9'].append(catalog_track(store, f'tail-{i}', label='Ambient')[0])
    quiet = pools['9'][-110:]
    quiet_keys = {track['key'] for track in quiet}
    for rows in pools.values():
        for track in rows:
            tid = store.tracks.track_ids_for_videos([track['video_id']])[track['video_id']]
            with store.conn:
                catalog.choose_field(store.conn, tid, 'energy', .04 if track['key'] in quiet_keys else .96)
    bundles = store.rec_proposals.get_proposals('mode_bundles')
    bundles['9']['wheelhouse'] = pools['9'][:12]
    bundles['all'] = {'wheelhouse': [t for rows in pools.values() for t in rows]}
    bundles['_radio'] = {'generation': None, 'space': '',
                         'members': {mid: [t['key'] for t in rows] for mid, rows in pools.items()}}
    store.rec_proposals.put_proposals('mode_bundles', bundles)
    prepare_vibes(store)
    return pools, quiet


def candidate(store, **tuning):
    return RadioCandidates(store, 1000, {**radio_tuning.DEFAULTS, 'theme': 1, 'energy': 0, **tuning})


@pytest.mark.parametrize('theme', [0, .5, 1])
def test_intensity_escapes_nearby_and_pinned_styles_and_relabels_each_stream(store, distant_chill, monkeypatch, theme):
    pools, quiet = distant_chill
    seed = pools['1'][0]
    c = candidate(store, theme=theme, comfort=1)
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
        assert branch['cluster_id'] == '9' and branch['label'] == 'Ambient'
        assert all(musical_character.intensity(t) == .04 for t in branch['tracks'])
        assert c.fits(branch['tracks'][0], seed)
    before = directions.public([])
    assert not directions.update([seed], [], 0)
    assert directions.public([]) == before
    assert max(reads) <= CLUSTER_POOL_LIMIT


def test_intensity_refills_the_suitable_style_beyond_one_lookahead(store, distant_chill):
    pools, quiet = distant_chill
    c, history = candidate(store), [pools['1'][0]]
    for _ in quiet:
        track = c.next_track(history, target=history[0])
        assert track and musical_character.intensity(track) == .04
        history.append(track)
    assert {t['key'] for t in history[1:]} == {t['key'] for t in quiet}


def test_equally_suitable_current_style_is_kept_and_midpoint_still_pins(store, distant_chill):
    pools, quiet = distant_chill
    seed = pools['1'][0]
    neutral = candidate(store, energy=.5)
    assert neutral.cluster_for(neutral.next_track([seed])) == '1'
    local = pools['1'][1]
    tid = store.tracks.track_ids_for_videos([local['video_id']])[local['video_id']]
    with store.conn:
        catalog.choose_field(store.conn, tid, 'energy', .04)
    prepare_vibes(store)
    c = candidate(store)
    assert c.next_track([seed])['key'] == local['key']
    # Once the current style has no remaining fit, only that stream changes.
    assert c.cluster_for(c.next_track([seed, local], target=seed)) == '9'


def test_explicit_genre_mute_cannot_be_escaped_for_intensity(store, distant_chill):
    pools, quiet = distant_chill
    c = candidate(store)
    store.rec_model.set_weight('genre:ambient', 0, lo=0, now=1000)
    pick = c.next_track([pools['1'][0]])
    assert pick and pick['genre'] != 'Ambient'
    assert not c._allowed(quiet[0])


def test_legacy_explicit_genre_pin_allows_style_switch_inside_genre(store, genre_pools):
    pools = genre_pools
    for mid, rows in pools.items():
        for track in rows:
            tid = store.tracks.track_ids_for_videos([track['video_id']])[track['video_id']]
            with store.conn:
                catalog.choose_field(store.conn, tid, 'energy', .04 if mid in ('5', '9') else .96)
    # Style 9 is also quiet, but its Ambient genre is outside this pinned request.
    prepare_vibes(store)
    c = candidate(store)
    pick = c.next_track([pools['1'][0]])
    assert pick['genre'] == 'Electronic'
    assert c.cluster_for(pick) == '5'


def test_live_retune_switches_styles_without_restarting_current_playback(poc, distant_chill, monkeypatch):
    controller, remote, frames, _, _ = poc
    pools, quiet = distant_chill
    monkeypatch.setattr(radio_session, 'RadioCandidates', RadioCandidates)
    assert controller.start(pools['1'][0]['video_id'])['ok']
    play(controller, remote.ids[0])
    playlist, before_frames = controller.session.playlist, list(frames)
    heard, playing = list(controller.session.heard), controller.session.observed_video
    assert controller.tune({**radio_tuning.DEFAULTS, 'theme': 1, 'energy': 0})['ok']
    controller.advance(controller.session)
    assert not controller.status()['updating']
    assert controller.session.playlist == playlist
    assert controller.session.heard == heard
    assert controller.session.observed_video == playing
    assert frames == before_frames
    future = controller.session.tracks[controller.session.position + 1:]
    assert future and all(musical_character.intensity(t) == .04 for t in future)
    assert all(b['cluster_id'] == '9' for b in controller.session.directions.slots)
    play(controller, future[0]['video_id'])
    assert controller.session.observed_video == future[0]['video_id']
    assert not controller.session.rejected
