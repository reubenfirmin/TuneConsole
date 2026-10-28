import numpy as np
import pytest

from tests import model_support
from yt_playlist.rec import album_choices, choice_evidence, embedding_vectors, rec_params, scoring, taste_viz
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


def test_album_day_counts_keep_lifetime_cross_account_deduplication(store):
    keys = album(store, 'release', 'Record', 'Jazz', 2)
    store.collection.cache_album_tracklist('release', 'Record', 'Record', None, keys)
    first = store.identities.upsert_identity('first', 'cred', None, True)
    second = store.identities.upsert_identity('second', 'cred', None, False)
    unrelated = store.tracks.upsert_track('other', 'Unrelated', 'Elsewhere', None, 100)
    other_key = store.conn.execute('SELECT identity_key FROM tracks WHERE id=?', (unrelated,)).fetchone()[0]
    at = 1_000_000_000
    store.history.import_listening_events(first,
        [(keys[0], None, at), (keys[0], None, at + 86400)]
        + [(other_key, None, at + day * 86400) for day in range(300)], source_timezone='UTC')
    store.history.import_listening_events(second, [(keys[0], None, at)], source_timezone='UTC')
    context, = store.collection.taste_album_contexts()
    assert context['track_days'] == {keys[0]: 2, keys[1]: 0}
    assert context['return_days'] == 2 and context['return_coverage'] == .5
    assert context['tracklist_complete']


def test_album_preparation_reuses_one_day_aggregate_across_both_spaces(store):
    keys = album(store, 'release', 'Record', 'Jazz', 2)
    store.rec_model.replace_rec_vectors([(key, np.array([1., 0.], dtype=np.float32).tobytes()) for key in keys])
    model_support.build_content_model(store)
    with store.read_snapshot() as snapshot:
        statements = []
        snapshot.store.conn.set_trace_callback(statements.append)
        prepared = album_choices.prepare(snapshot.store, 100, [])
        assert [row['browse_id'] for row in prepared] == ['release']
        assert sum('COUNT(DISTINCT d.day)' in sql for sql in statements) == 1
        # Inclusion is a consumer filter; it must not poison the shared context memo.
        assert snapshot.store.collection.taste_album_contexts(include_browse={'missing'})
        assert sum('COUNT(DISTINCT d.day)' in sql for sql in statements) == 1


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


def test_companion_work_is_limited_to_the_frontier_with_identical_track_selection(store, monkeypatch):
    keys = []
    for i in range(40):
        keys.extend(album(store, f'saved-{i}', f'Artist {i}', 'Jazz' if i % 2 else 'Techno', 1))
    model_support.build_content_model(store)
    discoveries = [{'browse_id': f'new-{i}', 'artist': f'New artist {i}', 'title': f'New record {i}',
                    'genre': 'Jazz' if i % 2 else 'Techno', 'year': '2020'} for i in range(40)]
    pool = [{'key': key, 'video_id': f'video-{i}', 'artist': f'Track artist {i // 3}'}
            for i, key in enumerate(keys)]
    store.rec_proposals.put_proposals('mode_bundles', {'all': {'wheelhouse': pool}}, 1)
    # The reference evaluates all 80 candidate rows using the existing companion algorithm.
    from yt_playlist.rec import content_model
    import json
    snapshot = store.rec_model.content_generation()
    _, matrix, index = embedding_vectors.load_content_vectors(store, snapshot=snapshot)
    model = json.loads(snapshot['model'])
    all_albums = [{**a, 'browse_id': a['browse']} for a in store.collection.taste_album_contexts()] + discoveries
    features = []
    for item in all_albums:
        rows = [index[key] for key in item.get('keys', []) if key in index]
        vector = matrix[rows].mean(0) if rows else content_model.encode_content(model, item['genre'], item['year'], {})
        features.append(vector / np.linalg.norm(vector))
    original = album_choices._companions
    expected = dict(zip([a['browse_id'] for a in all_albums],
                        original(store, all_albums, np.asarray(features), matrix, index)))
    calls = []

    def bounded(store, albums, features, matrix, index):
        assert len(albums) == len(features) <= 24
        calls.extend(a['browse_id'] for a in albums)
        return original(store, albums, features, matrix, index)

    monkeypatch.setattr(album_choices, '_companions', bounded)
    prepared = album_choices.prepare(store, 100, discoveries)
    assert len(prepared) == len(calls) == 24
    assert set(calls) == {a['browse_id'] for a in prepared}
    assert sum(a['classification'] == 'rediscovery' for a in prepared) == 12
    for item in prepared:
        assert item['companion'] == expected[item['browse_id']]
        assert not set(item.get('keys') or []) & {track['key'] for track in item['companion']}


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
        {'kind': 'album', 'browse': 'unmixed', 'return_days': 20, 'return_weight': 1., 'return_coverage': 1.},
        {'kind': 'album', 'browse': 'unheard', 'saved': True, 'return_days': 0},
        {'kind': 'album', 'browse': 'known', 'return_days': 4, 'return_weight': 1., 'return_coverage': 1.}]
    album = {'browse_id': 'mixed', 'artist': artist,
             'title': 'Global Underground #42: Patrice Bäumel - Berlin (DJ Mix)'}
    reason = album_choices._rationale(album, taste, np.array([1., .9, .7]), 'content:test')
    assert reason['source']['browse'] == 'known'
    assert reason['text'] == 'A possible fit with your repeat listening to “Known artist · Known record”.'
    assert album_choices._rationale(album, taste, np.array([1., .9, 0.]), 'content:test') is None


def test_album_returns_scale_from_one_favorite_to_several_to_the_whole_record_in_both_spaces(store):
    iid = store.identities.upsert_identity('main', 'cred', None, True)
    cases = [('one', 12, 1), ('five', 12, 5), ('whole', 12, 12), ('short', 6, 6), ('long', 24, 24)]
    vectors = []
    for browse, size, played in cases:
        keys = album(store, browse, browse.title(), 'Techno', size)
        store.collection.cache_album_tracklist(browse, browse.title(), browse.title(), None, keys)
        vectors.extend((key, np.array([1., 0.], dtype=np.float32).tobytes()) for key in keys)
        store.history.import_listening_events(iid, [
            (key, None, 1_700_000_000 + day * 86400) for day in range(3) for key in keys[:played]],
            source_timezone='UTC')
    anchor = album(store, 'anchor', 'Anchor', 'Folk', 2)
    vectors.extend((key, np.array([0., 1.], dtype=np.float32).tobytes()) for key in anchor)
    store.rec_model.replace_rec_vectors(vectors)
    model_support.build_content_model(store)
    for taste in (scoring.playlist_taste(store), scoring.content_taste(store)):
        weights = {source['browse']: weight for source, weight in zip(taste.sources, taste.weights)}
        boosts = {browse: weights[browse] / weights['anchor'] - 1 for browse, _, _ in cases}
        assert 0 < boosts['one'] < boosts['five'] < boosts['whole']
        assert boosts['five'] == pytest.approx(5 * boosts['one'])
        assert boosts['whole'] == pytest.approx(12 * boosts['one'])
        assert boosts['short'] == pytest.approx(boosts['whole'])
        assert boosts['long'] == pytest.approx(boosts['whole'])
    # Corrections remove their return support, keeping the separate saved seeds.
    store.history.remove_listening_source('takeout', iid)
    for taste in (scoring.playlist_taste(store), scoring.content_taste(store)):
        assert taste.weights == pytest.approx(np.full(6, 1 / 6))
        assert all(source['return_weight'] == 0 for source in taste.sources)


def test_partial_album_returns_focus_on_heard_tracks_without_shrinking_the_denominator(store):
    iid = store.identities.upsert_identity('main', 'cred', None, True)
    keys = album(store, 'record', 'Record', None, 12)
    store.collection.cache_album_tracklist('record', 'Record', 'Record', None, keys)
    store.collection.remove_saved_album('record')
    # Ten tracks have no model features; they still belong in the denominator.
    store.rec_model.replace_rec_vectors([
        (keys[0], np.array([1., 0.], dtype=np.float32).tobytes()),
        (keys[1], np.array([0., 1.], dtype=np.float32).tobytes())])
    store.history.import_listening_events(iid, [
        (keys[0], None, 1_700_000_000 + day * 86400) for day in range(3)], source_timezone='UTC')
    taste = scoring.playlist_taste(store)
    source = taste.sources[0]
    assert source['return_coverage'] == pytest.approx(1 / 12)
    assert source['return_weight'] == pytest.approx(np.log(3) / 12)
    assert (source['modeled_tracks'], source['known_tracks']) == (2, 12)
    assert taste.centroids[0] == pytest.approx([1., 0.])
    assert taste.score_all(np.eye(2))[0] > taste.score_all(np.eye(2))[1]
    reason = album_choices._rationale({'browse_id': 'different'}, taste, np.array([1.]), 'collaborative')
    assert reason['text'] == 'A possible fit with tracks you keep returning to on “Record · Record”.'
    # Explicit release feedback can deliberately support the full record.
    album_choices.set_feedback(store, 'record', 'more', 1_700_300_000)
    assert scoring.playlist_taste(store).centroids[0] == pytest.approx(np.ones(2) / np.sqrt(2))


@pytest.mark.parametrize('tracklist', ['missing', 'truncated', 'unresolved', 'empty'])
def test_unknown_album_coverage_keeps_saved_support_without_guessing_a_return_boost(store, tracklist):
    iid = store.identities.upsert_identity('main', 'cred', None, True)
    keys = album(store, 'record', 'Record', 'Techno', 6)
    if tracklist != 'missing':
        store.collection.cache_album_tracklist('record', 'Record', 'Record', None,
                                               [] if tracklist == 'empty' else keys)
        aid = store.collection.album_id('record')
        if tracklist == 'truncated':
            store.conn.execute('DELETE FROM album_tracks WHERE album_id=? AND id=(SELECT MAX(id) '
                               'FROM album_tracks WHERE album_id=?)', (aid, aid))
        elif tracklist == 'unresolved':
            store.conn.execute('UPDATE album_tracks SET entry_id=NULL,recording_id=NULL,legacy_track_key=? '
                               'WHERE album_id=? AND id=(SELECT MAX(id) FROM album_tracks WHERE album_id=?)',
                               ('', aid, aid))
        store.conn.commit()
    store.history.import_listening_events(iid, [
        (key, None, 1_700_000_000 + day * 86400) for day in range(3) for key in keys], source_timezone='UTC')
    store.rec_model.replace_rec_vectors([
        (key, np.array([1., 0.], dtype=np.float32).tobytes()) for key in keys])
    model_support.build_content_model(store)
    context = store.collection.taste_album_contexts()[0]
    assert context['tracklist_complete'] is False and context['return_coverage'] is None
    for taste in (scoring.playlist_taste(store), scoring.content_taste(store)):
        assert taste.sources[0]['saved'] and taste.sources[0]['return_weight'] == 0
        assert album_choices._rationale({'browse_id': 'different'}, taste, np.array([1.]), 'test') is None


def test_album_returns_do_not_pool_different_songs_days_or_count_generated_and_duplicate_plays(store):
    iid = store.identities.upsert_identity('main', 'cred', None, True)
    other = store.identities.upsert_identity('other', 'cred', None, False)
    keys = album(store, 'record', 'Record', 'Techno', 6)
    # Repeating a recording within the tracklist cannot enlarge its coverage share.
    entries = [{'entry_id': row[0]} for row in store.conn.execute(
        'SELECT entry_id FROM album_tracks WHERE album_id=? ORDER BY id', (store.collection.album_id('record'),))]
    store.collection.cache_album_tracklist('record', 'Record', 'Record', None, [], entries=entries + entries[:1])
    start = 1_700_000_000
    store.history.import_listening_events(iid, [
        (key, None, start + day * 86400) for day, key in enumerate(keys)], source_timezone='UTC')
    context = store.collection.taste_album_contexts()[0]
    assert context['return_days'] == 1 and context['return_coverage'] == 0
    for account in (iid, other):
        store.history.import_listening_events(account, [
            (keys[0], None, start + 7 * 86400 + replay * 60) for replay in range(20)], source_timezone='UTC')
    for key in keys[1:]:
        store.history.record_play_event(iid, key, None, start + 8 * 86400,
                                        context_choice=True, source_timezone='UTC')
    context = store.collection.taste_album_contexts()[0]
    assert context['tracklist_complete'] and len(context['keys']) == 6
    assert context['return_days'] == 2
    assert context['track_days'] == {key: 2 if key == keys[0] else 1 for key in keys}
    assert context['return_coverage'] == pytest.approx(1 / 6)


@pytest.mark.parametrize('played', [1, 5, 12])
def test_confirmed_album_queue_returns_use_track_coverage_and_corrections_without_organic_leakage(store, played):
    iid = store.identities.upsert_identity('main', 'cred', None, True)
    keys = album(store, 'record', 'Record', 'Techno', 12)
    anchor = album(store, 'anchor', 'Anchor', 'Folk', 2)
    store.collection.cache_album_tracklist('record', 'Record', 'Record', None, keys)
    store.rec_model.replace_rec_vectors([
        (key, np.array([1., 0.], dtype=np.float32).tobytes()) for key in keys]
        + [(key, np.array([0., 1.], dtype=np.float32).tobytes()) for key in anchor])
    model_support.build_content_model(store)
    start = 1_700_000_000
    tracks = [{'key': key, 'video_id': f'record-{index}'} for index, key in enumerate(keys)]
    unchosen = choice_evidence.issue(store, 'albums', [{'id': 'record', 'tracks': tracks}], start - 100,
                                     context={}, opportunity_id='unchosen')
    choice_evidence.respond(store, unchosen, 'start', start - 90, option_id='record',
                            details={'video_id': tracks[0]['video_id']})
    for day in range(2):
        at = start + day * 86400
        oid = choice_evidence.issue(store, 'albums', [{'id': 'record', 'tracks': tracks}], at, context={},
                                    opportunity_id=f'return-{day}')
        choice_evidence.respond(store, oid, 'choice', at + 1, option_id='record')
        for index, track in enumerate(tracks[:played]):
            now = at + 10 + index
            store.history.record_play_event(iid, track['key'], track['video_id'], now,
                                            context_choice=True, source_timezone='UTC')
            choice_evidence.respond(store, oid, 'start', now, option_id='record',
                                    action_id=f'start-{day}-{index}', details={'video_id': track['video_id']})
        choice_evidence.respond(store, oid, 'start', at + 30, option_id='record',
                                action_id=f'wrong-{day}', details={'video_id': 'unrelated'})
    # Continuing the old queue on another day is not a fresh deliberate return.
    choice_evidence.respond(store, oid, 'start', start + 2 * 86400, option_id='record',
                            action_id='continuation', details={'video_id': tracks[0]['video_id']})
    failed = choice_evidence.issue(store, 'albums', [{'id': 'record', 'tracks': tracks}], start + 2 * 86400,
                                   context={}, opportunity_id='failed')
    choice_evidence.respond(store, failed, 'choice', start + 2 * 86400 + 1, option_id='record')
    choice_evidence.respond(store, failed, 'start', start + 2 * 86400 + 2, option_id='record',
                            details={'video_id': 'unrelated'})
    now = start + 3 * 86400
    choice_evidence.compile_report(store, now)
    assert not any(store.collection.taste_album_contexts()[0]['track_days'].values())
    def check():
        for taste in (scoring.playlist_taste(store), scoring.content_taste(store)):
            source = next(row for row in taste.sources if row.get('browse') == 'record')
            assert source['return_coverage'] == pytest.approx(played / 12)
            assert source['return_weight'] == pytest.approx(played / 12 * np.log(2))
    check()
    # Independent history on the same days must not double the album signal.
    store.history.import_listening_events(iid, [(key, None, start + day * 86400 + 3600)
        for day in range(2) for key in keys[:played]], source_timezone='UTC')
    check()
    store.history.remove_listening_source('takeout', iid)
    choice_evidence.respond(store, oid, 'withdraw', now + 1,
                            details={'action_id': f'{oid}:choice:record'})
    choice_evidence.compile_report(store, now + 2)
    for taste in (scoring.playlist_taste(store), scoring.content_taste(store)):
        source = next(row for row in taste.sources if row.get('browse') == 'record')
        assert source['return_coverage'] == 0 and source['return_weight'] == 0


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
