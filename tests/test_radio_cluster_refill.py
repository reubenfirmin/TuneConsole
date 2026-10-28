"""Continuing style streams are independent of Home's finite card proposals."""
import pytest

from yt_playlist.rec import embedding_vectors, radio_tuning
from yt_playlist.rec.radio_candidates import CLUSTER_POOL_LIMIT, RadioCandidates
from tests.test_radio_catalog import catalog_track, seed
from tests.musical_support import prepare_profiles
from yt_playlist.core import catalog
from yt_playlist.rec import musical_character


def indexed(store, seed, count=110, *, mixed=False):
    tracks = [catalog_track(store, f'cluster-{i}', label='Indie Rock' if mixed and i % 2 else seed['genre'])[0]
              for i in range(count)]
    store.rec_proposals.put_proposals('mode_bundles', {
        '1': {'wheelhouse': [seed]}, '_radio': {'generation': None, 'space': '',
        'members': {'1': [seed['key'], *[track['key'] for track in tracks]]}}})
    return tracks


@pytest.mark.parametrize('theme', [0, .5, 1])
def test_every_dial_position_refills_beyond_home_and_catalog_batch(store, seed, monkeypatch, theme):
    tracks = indexed(store, seed)
    c = RadioCandidates(store, 1000, {**radio_tuning.DEFAULTS, 'theme': theme})
    monkeypatch.setattr(embedding_vectors, 'load_content_vectors', lambda *a, **kw: pytest.fail('request loaded full vectors'))
    sizes, read = [], c._read_vectors
    def bounded(keys):
        sizes.append(len(keys))
        assert len(keys) <= CLUSTER_POOL_LIMIT
        return read(keys)
    monkeypatch.setattr(c, '_read_vectors', bounded)
    history = [seed]
    for _ in tracks:
        track = c.next_track(history)
        assert track is not None
        assert track['key'] not in {row['key'] for row in history}
        history.append(track)
    assert {row['key'] for row in history[1:]} == {row['key'] for row in tracks}
    assert c.next_track(history) is None
    assert max(sizes) <= CLUSTER_POOL_LIMIT


def test_same_cluster_directions_use_distinct_positions_and_preserve_existing_plans(store, seed):
    indexed(store, seed, 30)
    c = RadioCandidates(store, 1000, {**radio_tuning.DEFAULTS, 'theme': 1})
    anchors = []
    for _ in range(4):
        anchor = c.direction_anchor([seed], anchors)
        assert anchor is not None and anchor['video_id'] not in {row['video_id'] for row in anchors}
        anchors.append(anchor)
    snapshot = [dict(row) for row in anchors]
    c.next_track([seed], excluded=[row['video_id'] for row in anchors], target=anchors[0])
    assert anchors == snapshot


def test_refill_rechecks_feedback_and_survives_an_unrelated_model_publication(store, seed):
    tracks = indexed(store, seed, 5)
    c = RadioCandidates(store, 1000, {**radio_tuning.DEFAULTS, 'theme': 1})
    store.rec_model.record_dislike(tracks[0]['key'], 1001, 1)
    store.settings.set_setting('rec_content_generation', 'new-generation')
    # An active session retains its published membership and directions. A new
    # incompatible basis is never decoded against the old saved centroids.
    store.settings.set_setting('rec_content_model', '{"new":"basis"}')
    history = [seed]
    for _ in range(4):
        track = c.next_track(history)
        assert track is not None and track['key'] != tracks[0]['key']
        history.append(track)
    assert c.next_track(history) is None


def test_indexed_cluster_without_a_home_card_is_still_available(store, seed):
    tracks = indexed(store, seed, 3)
    payload = store.rec_proposals.get_proposals('mode_bundles')
    payload['1'] = {}
    store.rec_proposals.put_proposals('mode_bundles', payload)
    c = RadioCandidates(store, 1000, {**radio_tuning.DEFAULTS, 'theme': 1})
    assert c.next_track([seed])['key'] in {row['key'] for row in tracks}
    assert c.seed_offer()


def test_continuing_refill_uses_shared_intensity_for_unprepared_recordings(store, seed):
    tracks = indexed(store, seed, 3)
    for i, track in enumerate(tracks):
        tid = store.tracks.track_ids_for_videos([track['video_id']])[track['video_id']]
        with store.conn:
            catalog.choose_field(store.conn, tid, 'energy', .99 if i == 2 else .01)
    prepare_profiles(store)
    c = RadioCandidates(store, 1000, {**radio_tuning.DEFAULTS, 'theme': 1, 'energy': 1})
    track = c.next_track([seed])
    assert track['key'] == tracks[2]['key']
    assert musical_character.intensity(track) == .99
    assert track['musical']['version'] == musical_character.VERSION


def test_correcting_feedback_readmits_a_recording_in_the_same_session(store, seed):
    track = indexed(store, seed, 1)[0]
    c = RadioCandidates(store, 1000, {**radio_tuning.DEFAULTS, 'theme': 1})
    store.rec_model.record_dislike(track['key'], 1001, 1)
    assert c.next_track([seed]) is None
    store.rec_model.clear_dislike(track['key'])
    assert c.next_track([seed])['key'] == track['key']


def test_themed_streams_pin_the_playing_genre_inside_mixed_clusters(store, seed):
    from yt_playlist.rec.radio_directions import RadioDirections
    indexed(store, seed, 110, mixed=True)
    c = RadioCandidates(store, 1000, {**radio_tuning.DEFAULTS, 'theme': 1})
    directions = RadioDirections(c, seed)
    directions.update([seed], [], 0)
    assert len(directions.public([])) == 4
    assert all(len(branch['tracks']) == 6 for branch in directions.slots)
    assert all(t['genre'] == seed['genre'] for branch in directions.slots for t in branch['tracks'])
    first = [dict(branch) for branch in directions.slots]
    # Polling alone preserves the already valid genre-constrained plans.
    assert not directions.update([seed], [], 0)
    assert directions.slots == first


def test_pinned_refill_reads_past_a_full_page_of_other_genres(store, seed):
    wrong = [catalog_track(store, f'wrong-{i}', label='Indie Rock')[0] for i in range(CLUSTER_POOL_LIMIT)]
    matching = [catalog_track(store, f'matching-{i}', label=seed['genre'])[0] for i in range(30)]
    store.rec_proposals.put_proposals('mode_bundles', {
        '1': {'wheelhouse': [seed, *wrong]}, '_radio': {'generation': None, 'space': '',
        'members': {'1': [t['key'] for t in [seed, *wrong, *matching]]}}})
    c = RadioCandidates(store, 1000, {**radio_tuning.DEFAULTS, 'theme': 1})
    history = [seed]
    for _ in matching:
        track = c.next_track(history)
        assert track is not None and track['genre'] == seed['genre']
        history.append(track)
    assert {t['key'] for t in history[1:]} == {t['key'] for t in matching}
    assert c.next_track(history) is None
