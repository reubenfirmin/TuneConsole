"""Cleanup shares detached inputs, pair work and the page's Home summary."""
from itertools import combinations as original_combinations

from fastapi.testclient import TestClient
import pytest

from yt_playlist.core.store import Store
from yt_playlist.library import analysis
from yt_playlist.repos.models import CleanupPreferences, Playlist, PlaylistMemberships
from yt_playlist.web.app import create_app


@pytest.mark.parametrize('size', [12, 48, 120])
def test_categories_and_summary_compare_each_candidate_once(size, monkeypatch):
    playlists = [Playlist(i, 1, f'PL{i}', f'Playlist {i}', 8, '', 1, 1, 1)
                 for i in range(size)]
    keys = {p.id: frozenset(str(k) for k in range(p.id % 10, p.id % 10 + 8))
            for p in playlists}
    state = analysis.CleanupAnalysis(PlaylistMemberships(playlists, keys, {}, set()),
                                    CleanupPreferences({}, [], set(), set(), []))
    comparisons = 0
    original = analysis.CleanupAnalysis._candidate_pairs

    def counted(*args):
        nonlocal comparisons
        for pair in original(*args):
            comparisons += 1
            yield pair

    monkeypatch.setattr(analysis.CleanupAnalysis, '_candidate_pairs', counted)
    result = analysis.analyze_cleanup(state)
    payload = result.summary().as_payload()
    expected = sum(bool(keys[a.id] & keys[b.id]) for a, b in original_combinations(playlists, 2))
    assert comparisons == expected
    assert analysis.cleanup_summary(result).as_payload() == payload
    assert analysis.analyze_cleanup(state).summary().as_payload() == payload
    assert comparisons == expected


@pytest.mark.parametrize('threshold', [-1, 0, 0.5, 1, 1.1])
@pytest.mark.parametrize('members', [
    [[], [], ['a'], ['b'], ['a', 'c'], ['a', 'c'], ['c', 'd']],
    [[str(k) for k in range(i, i + 20)] for i in range(12)],
])
def test_indexed_relationships_match_all_pairs_including_ties(members, threshold):
    playlists = [Playlist(i, 1, f'P{i}', f'Playlist {i}', len(keys), '', 1, 1, 1)
                 for i, keys in enumerate(members)]
    keys = {i: frozenset(values) for i, values in enumerate(members)}
    state = analysis.CleanupAnalysis(PlaylistMemberships(playlists, keys, {}, set()),
                                    CleanupPreferences({}, [], set(), set(), []))
    expected_dupes, expected_overlaps = [], []
    for a, b in original_combinations(playlists, 2):
        ka, kb = keys[a.id], keys[b.id]
        shared = ka & kb
        similarity = len(shared) / len(ka | kb) if ka | kb else 0.0
        if similarity >= threshold:
            expected_dupes.append((a.id, b.id, similarity, shared, ka - kb, kb - ka))
        elif shared:
            expected_overlaps.append((a.id, b.id, shared))
    expected_dupes.sort(key=lambda pair: -pair[2])
    expected_overlaps.sort(key=lambda pair: -len(pair[2]))
    dupes, overlaps = state.relationships(threshold)
    assert [(f.playlist_a.id, f.playlist_b.id, f.similarity, f.shared, f.only_a, f.only_b)
            for f in dupes] == expected_dupes
    assert [(f.playlist_a.id, f.playlist_b.id, f.shared) for f in overlaps] == expected_overlaps
    for finding in [*dupes, *overlaps]:
        assert finding.keys_a is keys[finding.playlist_a.id]
        assert finding.keys_b is keys[finding.playlist_b.id]
        assert finding.shared_count == len(finding.shared)


def test_sparse_disjoint_memberships_have_no_candidate_pairs():
    playlists = [Playlist(i, 1, f'P{i}', f'Playlist {i}', 1, '', 1, 1, 1) for i in range(200)]
    keys = {p.id: frozenset([str(p.id)]) for p in playlists}
    state = analysis.CleanupAnalysis(PlaylistMemberships(playlists, keys, {}, set()),
                                    CleanupPreferences({}, [], set(), set(), []))
    assert list(state._candidate_pairs(0.5)) == []
    assert state.relationships(0.5) == ([], [])


def test_memberships_preferences_and_upload_kinds_share_a_snapshot(tmp_path, monkeypatch):
    store = Store(tmp_path / 'cleanup.db')
    store.init_schema()
    try:
        iid = store.identities.upsert_identity('main', 'cred', None, True)
        a = store.playlists.upsert_playlist(iid, 'A', 'A', 1, '', 1)
        b = store.playlists.upsert_playlist(iid, 'B', 'B', 1, '', 1)
        track = store.tracks.upsert_track('v', 'Song', 'Artist', None, 100)
        store.playlists.set_playlist_tracks(a, [track])
        store.playlists.set_playlist_tracks(b, [track])
        # Two uploads of one recording: unique key counts, mixed upload kinds.
        other = store.tracks.upsert_track('v-other', 'Song', 'Artist', None, 100)
        with store.conn:
            recording = store.conn.execute('SELECT recording_id FROM catalog_entries WHERE id=?', (track,)).fetchone()[0]
            store.conn.execute('UPDATE catalog_entries SET recording_id=? WHERE id=?', (recording, other))
            store.conn.execute('UPDATE youtube_items SET recording_id=? WHERE video_id=?', (recording, 'v-other'))
            store.conn.execute("UPDATE catalog_tracks SET video_type='MUSIC_VIDEO_TYPE_ATV' WHERE id=?", (track,))
            store.conn.execute("UPDATE catalog_tracks SET video_type='MUSIC_VIDEO_TYPE_OMV' WHERE id=?", (other,))
        store.playlists.set_playlist_tracks(a, [track, other])
        original = type(store.overlaps).cleanup_preferences

        def change_after_memberships(repo):
            store.playlists.set_playlist_tracks(b, [])
            store.overlaps.ignore_cleanup('B', 'empty', 2)
            return original(repo)

        with monkeypatch.context() as patch:
            patch.setattr(type(store.overlaps), 'cleanup_preferences', change_after_memberships)
            result = analysis.analyze_cleanup(store)
        assert len(result.groups) == 1
        assert result.groups[0].track_count == 1
        assert result.analysis.memberships.kinds['A'] == 'mixed'
        assert result.analysis.preferences.ignored == {}
        assert analysis.analyze_cleanup(store).summary().count == 1  # A is tiny; B dismissed
    finally:
        store.conn.close()


def test_page_and_home_summary_share_one_analysis(store, monkeypatch):
    iid = store.identities.upsert_identity('main', 'cred', None, True)
    track = store.tracks.upsert_track('v', 'Song', 'Artist', None, 100)
    for i in range(10):
        pid = store.playlists.upsert_playlist(iid, f'P{i}', f'Playlist {i}', 1, '', 1)
        store.playlists.set_playlist_tracks(pid, [track])
    memberships = type(store.playlists).cleanup_memberships
    calls = 0

    def bulk(repo):
        nonlocal calls
        calls += 1
        return memberships(repo)

    def no_individual_reads(*args):
        pytest.fail('Cleanup must reuse bulk membership and upload kinds')

    monkeypatch.setattr(type(store.playlists), 'cleanup_memberships', bulk)
    monkeypatch.setattr(type(store.playlists), 'get_playlist_track_keys', no_individual_reads)
    monkeypatch.setattr(type(store.playlists), 'playlist_kind', no_individual_reads)
    client = TestClient(create_app(store, lambda: {}, now_fn=lambda: 1000))
    response = client.get('/cleanup')
    assert response.status_code == 200
    assert calls == 1
    assert store.rec_proposals.get_proposals('cleanup')['count'] == 10
