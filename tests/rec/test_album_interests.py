import numpy as np
import pytest

from tests import model_support
from yt_playlist.rec import album_choices, embedding_vectors, rec_params, scoring, taste_viz
from yt_playlist.rec.taste_context import PlaylistTaste


def album(store, browse, title, genre, size, year='2020'):
    store.collection.add_saved_album({"browse": browse, "title": title, "artist": title, "year": year})
    keys = []
    for index in range(size):
        track = store.tracks.upsert_track(f"{browse}-{index}", f"Track {index}", title, title, 100,
                                         album_browse_id=browse)
        store.tracks.set_track_enrichment(track, genre, year)
        keys.append(f"track {index}|{title.lower()}")
    return keys


def test_saved_album_only_library_seeds_both_spaces_without_history(store):
    a = album(store, "release-a", "Techno", "Techno", 6)
    b = album(store, "release-b", "Folk", "Folk", 24)
    store.rec_model.replace_rec_vectors([(key, np.array([1., 0.], dtype=np.float32).tobytes()) for key in a]
                                       + [(key, np.array([0., 1.], dtype=np.float32).tobytes()) for key in b])
    model_support.build_content_model(store)
    for taste in (scoring.playlist_taste(store), scoring.content_taste(store)):
        assert len(taste.titles) == 2
        assert taste.weights == pytest.approx([.5, .5])  # Length is not preference strength.
        assert all(source["kind"] == "album" and source["saved"] for source in taste.sources)
    panel = taste_viz.playlist_influence_panel(store)
    assert panel["basis"] == "interests"
    assert all(context["source"]["return_days"] == 0 for context in panel["contexts"])


def test_context_allocation_does_not_prefer_the_average_of_separated_interests():
    taste = PlaylistTaste(["Dance", "Acoustic"], np.eye(2), np.array([.5, .5]))
    candidates = np.array([[1., 0.], [0., 1.], [1., 1.]])
    assert taste.score_all(candidates).argmax() == 2  # The former blend's failure mode.
    assert list(np.argsort(-taste.allocated_scores(candidates)))[:2] == [0, 1]


def test_album_recommendations_use_interest_ranking_for_new_releases(store):
    album(store, "release-a", "Techno", "Techno", 2)
    album(store, "release-b", "Folk", "Folk", 2)
    model_support.build_content_model(store)
    discoveries = [{"browse_id": "new-techno", "title": "New", "artist": "New artist",
                    "genre": "Techno", "year": "2020", "thumbnail": None},
                   {"browse_id": "other-techno", "title": "Another", "artist": "Techno",
                    "genre": "Techno", "year": "2020", "thumbnail": None},
                   {"browse_id": "no-features", "title": "Unknown", "artist": "Unknown"}]
    result = album_choices.prepare(store, 100, discoveries)
    assert {row["browse_id"] for row in result} == {"release-a", "release-b", "new-techno", "other-techno", "no-features"}
    assert all(row["basis"] and row["feature"] and row["score"] > 0 for row in result[:-1])
    assert result[-1]['browse_id'] == 'no-features' and result[-1]['score'] is None
    assert result[-1]['rationale'] is None
    # Saved membership alone cannot establish a familiar recommendation reference.
    assert next(row for row in result if row['browse_id'] == 'new-techno')['rationale'] is None
    assert {row["classification"] for row in result} == {"discovery", "rediscovery", "familiar_artist"}


def test_album_reason_uses_other_positive_model_contexts_not_its_own_saved_seed():
    taste = PlaylistTaste(['Artist · Record', 'Late nights'], np.eye(2), np.array([.5, .5]))
    taste.sources = [{'kind': 'album', 'browse': 'self'}, {'kind': 'playlist', 'id': 42}]
    reason = album_choices._rationale({'browse_id': 'self'}, taste, np.array([1., .8]), 'content:test')
    assert reason['source'] == {'kind': 'playlist', 'id': 42}
    assert reason['context'] == 'Late nights' and reason['similarity'] == .8
    assert reason['text'] == 'A possible fit for your “Late nights” playlist.'
    assert album_choices._rationale({'browse_id': 'self'}, taste, np.array([1., 0.]), 'content:test') is None


def test_album_reason_excludes_unheard_records_and_other_editions_of_itself():
    artist = 'Patrice Bäumel'
    taste = PlaylistTaste([
        artist + ' · Global Underground #42: Patrice Bäumel - Berlin/Unmixed',
        'Kenobix · Hypnotic Reviere', 'Known artist · Known record'], np.eye(3), np.ones(3) / 3)
    taste.sources = [
        {'kind': 'album', 'browse': 'unmixed', 'return_days': 20},
        {'kind': 'album', 'browse': 'unheard', 'saved': True, 'return_days': 0},
        {'kind': 'album', 'browse': 'known', 'return_days': 4}]
    album = {'browse_id': 'mixed', 'artist': artist,
             'title': 'Global Underground #42: Patrice Bäumel - Berlin (DJ Mix)'}
    reason = album_choices._rationale(album, taste, np.array([1., .9, .7]), 'content:test')
    assert reason['source']['browse'] == 'known'
    assert reason['text'] == 'A possible fit with your repeat listening to “Known artist · Known record”.'
    assert album_choices._rationale(album, taste, np.array([1., .9, 0.]), 'content:test') is None


def test_album_feedback_changes_only_the_declared_release_and_is_reversible(store):
    album(store, "release-a", "Techno", "Techno", 2)
    album(store, "release-b", "Folk", "Folk", 2)
    model_support.build_content_model(store)
    before = scoring.content_taste(store)
    album_choices.set_feedback(store, "release-a", "more", 1)
    weighted = scoring.content_taste(store)
    assert weighted.weights[0] > before.weights[0]
    album_choices.set_feedback(store, "release-a", "reject", 2)
    assert len(scoring.content_taste(store).titles) == 1
    assert all(a["browse_id"] != "release-a" for a in album_choices.prepare(store, 3, []))
    album_choices.set_feedback(store, "release-a", "clear", 4)
    assert scoring.content_taste(store).weights == pytest.approx(before.weights)
    assert store.collection.saved_album_ids() == {"release-a", "release-b"}


def test_sparse_saved_albums_use_collaborative_support_and_keep_unknown_choices(store):
    a = album(store, 'a', 'One', None, 2, year=None)
    b = album(store, 'b', 'Two', None, 2, year=None)
    store.collection.add_saved_album({'browse': 'unmodeled', 'title': 'Not yet fetched', 'artist': 'Third'})
    store.rec_model.replace_rec_vectors([(key, np.array([1., 0.], dtype=np.float32).tobytes()) for key in a]
        + [(key, np.array([0., 1.], dtype=np.float32).tobytes()) for key in b])
    result = album_choices.prepare(store, 100, [])
    assert len(result) == 3
    assert all(row['basis'] == 'collaborative' and row['score'] is not None for row in result[:2])
    assert result[-1]['browse_id'] == 'unmodeled' and result[-1]['score'] is None
    assert not store.playlists.get_playlists() and not store.history.play_events_since(0)
    store.rec_model.replace_rec_vectors([])
    assert len(album_choices.prepare(store, 101, [])) == 3
    assert all(row['score'] is None for row in album_choices.prepare(store, 101, []))


def test_album_picker_applies_shared_nonzero_genre_and_era_preferences(store):
    album(store, 'a', 'Techno', 'Techno', 2, year='1995')
    album(store, 'b', 'Folk', 'Folk', 2, year='2020')
    model_support.build_content_model(store)
    discoveries = [{'browse_id': 'c', 'title': 'Another', 'artist': 'Guest', 'genre': 'Techno', 'year': '1995'},
                   {'browse_id': 'd', 'title': 'Also', 'artist': 'Other', 'genre': 'Folk', 'year': '2020'}]
    def scores():
        return {row['browse_id']: row['score'] for row in album_choices.prepare(store, 100, discoveries)}
    baseline = scores()
    store.rec_model.set_weight('genre:folk', 2, now=100)
    preferred = scores()
    assert preferred['b'] > baseline['b'] and preferred['b'] > preferred['a']
    store.rec_model.set_weight('genre:folk', 1, now=100)
    store.rec_model.set_weight('era:2020', 2, now=100)
    assert scores()['b'] > baseline['b']


def test_organic_current_and_session_layers_affect_album_picker_and_expire(store):
    album(store, 'a', 'Techno', 'Techno', 2)
    folk = album(store, 'b', 'Folk', 'Folk', 2)
    model_support.build_content_model(store)
    _, matrix, index = embedding_vectors.load_content_vectors(store)
    space = embedding_vectors.content_space_id(store)
    store.modes.replace_modes([{'mode_id': i, 'label': browse, 'families': [], 'size': 2,
        'centroid': matrix[index[key]], 'rep_keys': [key], 'space': space}
        for i, (browse, key) in enumerate([('Techno', 'track 0|techno'), ('Folk', folk[0])])], [], 1)
    iid = store.identities.upsert_identity('main', 'cred', None, True)
    rec_params.set_param(store, 'now_min_events', 2)
    rec_params.set_param(store, 'play_transient_w', 0)
    rec_params.set_param(store, 'audio_transient_w', 0)
    now = 1_000_000
    def score(at):
        return next(row['score'] for row in album_choices.prepare(store, at, []) if row['browse_id'] == 'b')
    baseline = score(now)
    store.history.import_play_events(iid, [(key, f'b-{i}', now) for i, key in enumerate(folk)])
    current = score(now + 1)
    assert current > baseline
    rec_params.set_param(store, 'now_gain', 0)
    session = score(now + 1)
    assert baseline < session < current
    assert baseline < score(now + 8 * 3600) < session
    assert score(now + 25 * 3600) == pytest.approx(baseline)


def test_prepared_albums_reuse_unchanged_snapshot_and_refresh_actual_preferences(store, monkeypatch):
    album(store, 'a', 'Techno', 'Techno', 2)
    album(store, 'b', 'Folk', 'Folk', 2)
    model_support.build_content_model(store)
    original, calls = album_choices.prepare, []

    def counted(*args):
        calls.append(True)
        return original(*args)

    monkeypatch.setattr(album_choices, 'prepare', counted)
    before = album_choices.prepare_cached(store, 100)
    assert album_choices.prepare_cached(store, 101) == before
    assert len(calls) == 1
    store.rec_model.set_weight('genre:folk', 2, now=102)
    after = album_choices.prepare_cached(store, 102)
    assert len(calls) == 2
    scores = lambda payload: {row['browse_id']: row['score'] for row in payload['items']}
    assert scores(after)['b'] > scores(before)['b']
    album_choices.prepare_cached(store, 100 + album_choices.MAX_AGE_S)
    assert len(calls) == 3
