"""DAO suite for ChartsRepo (play-history stats for charts / artist / playlist pages)."""


def _seed(store):
    iid = store.identities.upsert_identity("me", "c", None, True)
    a = store.tracks.upsert_track("v1", "Alpha", "Artist A", "Album A", 200)
    b = store.tracks.upsert_track("v2", "Beta", "Artist B", "Album B", 200)
    lm = store.playlists.upsert_playlist(iid, "LM", "Liked Music", 0, "h", 0.0)
    pl = store.playlists.upsert_playlist(iid, "P1", "Mix", 0, "h", 0.0)
    store.playlists.set_playlist_tracks(pl, [a, b])
    store.playlists.set_playlist_tracks(lm, [a])                     # Alpha is liked (in LM)
    ka = store.conn.execute("SELECT identity_key k FROM tracks WHERE id=?", (a,)).fetchone()["k"]
    kb = store.conn.execute("SELECT identity_key k FROM tracks WHERE id=?", (b,)).fetchone()["k"]
    store.history.record_history_plays(iid, 1000.0, [ka, ka, kb], source_timezone="UTC")
    store.history.record_history_plays(iid, 86400+1000.0, [ka], source_timezone="UTC")
    return iid, pl


def test_top_tracks_ranks_by_plays(store):
    _seed(store)
    top = store.charts.top_tracks()
    assert [t["title"] for t in top] == ["Alpha", "Beta"]
    assert top[0]["plays"] == 2 and top[1]["plays"] == 1


def test_top_artists_sums_plays(store):
    _seed(store)
    top = store.charts.top_artists()
    assert top[0]["artist"] == "Artist A" and top[0]["plays"] == 2


def test_ticker_artwork_uses_saved_album_cover_and_ignores_empty_thumbnails(store):
    store.tracks.upsert_track("empty", "Empty", "Artist A", "Album A", 180, thumbnail="")
    store.tracks.upsert_track("v1", "Alpha", "Artist A", "Album A", 180,
                       album_browse_id="MPRE_a", thumbnail="/track-art.png")
    store.tracks.upsert_track("v2", "Beta", "Artist B", "Album B", 180)
    store.collection.add_saved_album({"browse": "MPRE_a", "title": "Album A", "artist": "Artist A",
                           "thumbnail": "/album-art.png"})

    assert store.charts.ticker_thumbnails("artist") == {"Artist A": "/track-art.png"}
    key = next(k for k,v in store.charts.album_browse_ids().items() if v=='MPRE_a')
    assert store.charts.ticker_thumbnails("album") == {key: "/album-art.png"}

    store.collection.remove_saved_album("MPRE_a")
    assert store.charts.ticker_thumbnails("album") == {key: "/album-art.png"}


def test_playlist_ticker_artwork_prefers_cover_and_falls_back_to_track_art(store):
    iid = store.identities.upsert_identity("me", "c", None, True)
    track = store.tracks.upsert_track("v1", "Alpha", "Artist A", "Album A", 180, thumbnail="/track.png")
    cover = store.playlists.upsert_playlist(iid, "P1", "With cover", 1, "h", 0.0, thumbnail="/playlist.png")
    fallback = store.playlists.upsert_playlist(iid, "P2", "Track art", 1, "h", 0.0)
    store.playlists.upsert_playlist(iid, "P3", "No art", 0, "h", 0.0)
    store.playlists.set_playlist_tracks(cover, [track])
    store.playlists.set_playlist_tracks(fallback, [track])

    assert store.charts.ticker_thumbnails("playlist") == {
        f"@playlist:{cover}": "/playlist.png", f"@playlist:{fallback}": "/track.png"}


def test_playlist_tracks_detail_marks_liked(store):
    _, pl = _seed(store)
    detail = store.charts.playlist_tracks_detail(pl)
    by_title = {d["title"]: d for d in detail}
    assert by_title["Alpha"]["liked"] is True and by_title["Beta"]["liked"] is False
    assert by_title["Alpha"]["plays"] == 2


def test_video_details_keep_playback_order_and_metadata_without_playlist_membership(store):
    from yt_playlist.core import catalog

    _, playlist = _seed(store)
    expected = store.charts.playlist_tracks_detail(playlist)
    store.playlists.set_playlist_tracks(playlist, [])
    with store.conn:
        catalog.upsert(store.conn, "outside", "Outside library", "Guest", "", 180, library=False)
    detail = store.charts.video_tracks_detail(["v2", "outside", "v1"])
    assert [t["video_id"] for t in detail] == ["v2", "outside", "v1"]
    assert detail[0] == expected[1] and detail[2] == expected[0]
    assert detail[1]["title"] == "Outside library"
    assert detail[2]["liked"] and detail[2]["plays"] == 2
    assert store.charts.video_tracks_detail([]) == []


def test_listen_stats_and_artist_songs(store):
    _, pl = _seed(store)
    stats = store.charts.get_playlist_listen_stats()
    assert stats[pl][1] == 3                                # 3 history-item hits across the playlist
    songs = store.charts.artist_songs("Artist A")
    assert len(songs) == 1 and songs[0]["title"] == "Alpha" and songs[0]["liked"] is True
    assert [p["ytm"] for p in songs[0]["playlists"]] == sorted(["LM", "P1"], key=str.lower) or \
           {p["ytm"] for p in songs[0]["playlists"]} == {"LM", "P1"}


def test_artist_songs_can_disambiguate_same_name_by_browse_id(store):
    iid = store.identities.upsert_identity("me", "c", None, True)
    first = store.tracks.upsert_track("v1", "First act", "Shared Name", "One", 180,
                               artist_browse_id="UC_first")
    second = store.tracks.upsert_track("v2", "Second act", "Shared Name", "Two", 180,
                                artist_browse_id="UC_second")
    store.tracks.upsert_track("v3", "Unknown act", "Shared Name", "Three", 180)
    playlist = store.playlists.upsert_playlist(iid, "P1", "Mix", 2, "h", 0.0)
    store.playlists.set_playlist_tracks(playlist, [first, second])

    assert [s["title"] for s in store.charts.artist_songs("Shared Name", "UC_first")] == ["First act"]


def test_artist_songs_keeps_unidentified_credit_separate_from_provider_identity(store):
    store.tracks.upsert_track("v1", "One", "Artist", "First", 180, artist_browse_id="UC_artist")
    store.collection.add_saved_album({"browse": "B1", "title": "Second", "artist": "Artist"})
    store.tracks.upsert_track("v2", "Two", "Artist", "Second", 180, album_browse_id="B1")

    songs = store.charts.artist_songs("Artist", "UC_artist")
    assert {s["title"] for s in songs} == {"One"}
    assert all(s["playlists"] == [] for s in songs)
    assert store.charts.artist_songs("Artist", "UC_unrelated") == []


def test_store_binding(store):
    _seed(store)
    assert store.charts.top_tracks()[0]["title"] == "Alpha"
    assert store.charts.artist_songs("Artist B")[0]["title"] == "Beta"


def test_playlist_detail_flags_edited_title_artist(store):
    iid = store.identities.upsert_identity("me", "c", None, True)
    p = store.playlists.upsert_playlist(iid, "P", "P", 0, "h", 0.0)
    t = store.tracks.upsert_track("v1", "Orig Title", "Orig Artist", "Al", 100)
    store.playlists.set_playlist_tracks(p, [t])
    d = store.charts.playlist_tracks_detail(p)[0]
    assert d["title_edited"] is False and d["artist_edited"] is False
    store.tracks.set_track_title(t, "New Title")
    d = store.charts.playlist_tracks_detail(p)[0]
    assert d["title_edited"] is True and d["artist_edited"] is False


def test_story_songs_favor_recent_listens_including_spotify_and_collapse_uploads(store):
    from yt_playlist.core import catalog

    now = 1_800_000_000
    day = 86400
    iid = store.identities.upsert_identity('me', 'c', None, True)
    keys = {}
    for video, title in [('recent', 'Recent'), ('once', 'Once'), ('old', 'Old favorite'),
                         ('unplayed', 'A new song'), ('wrong', 'Wrong genre')]:
        entry = store.tracks.upsert_track(video, title, 'Band', '', 180)
        with store.conn:
            store.conn.execute('UPDATE catalog_tracks SET genre=? WHERE id=?',
                               ('Techno' if video == 'wrong' else 'Shoegaze', entry))
        keys[video] = entry
    store.tracks.upsert_track('second-upload', 'Recent', 'Band', '', 180)
    recording = store.conn.execute('SELECT recording_id FROM catalog_entries WHERE id=?',
                                   (keys['recent'],)).fetchone()[0]
    store.tracks.associate_youtube_item('second-upload', recording, source='verified',
        evidence={'review': 'same recording'}, now=now)
    keys = {video: store.tracks.identity_key_for_video(video) for video in keys}
    store.tracks.upsert_track('unavailable', 'Unavailable', 'Band', '', 180, available=False)
    with store.conn:
        store.conn.execute("UPDATE catalog_tracks SET genre='Shoegaze' WHERE video_id='unavailable'")
        catalog.upsert(store.conn, 'outside', 'Outside library', 'Band', '', 180, library=False)
        store.conn.execute("UPDATE catalog_tracks SET genre='Shoegaze' WHERE video_id='outside'")
    events = [(keys['recent'], None, now - day), (keys['recent'], None, now - 2 * day),
              (keys['once'], None, now - day), (keys['wrong'], None, now - day)]
    events.extend((keys['old'], None, now - (40 + i) * day) for i in range(10))
    store.history.import_listening_events(iid, events, source='spotify_export', source_timezone='UTC')

    songs = store.charts.story_tracks(since=now - 30 * day, genres=['Shoegaze'])
    assert [song['title'] for song in songs] == ['Recent', 'Once', 'Old favorite']
    assert [song['recent_plays'] for song in songs] == [2, 1, 0]
    assert len({song['key'] for song in songs}) == 3
    assert all(song['video_id'] not in ('outside', 'unavailable') for song in songs)


def test_story_artist_songs_follow_structured_collaboration_credits(store):
    store.tracks.upsert_track('collab', 'Together', 'Band & Guest', '', 180,
        credits=[{'name': 'Band', 'id': 'UC_band'}, {'name': 'Guest', 'id': 'UC_guest'}])
    store.tracks.upsert_track('solo', 'Solo', 'Band', '', 180, artist_browse_id='UC_band')
    store.tracks.upsert_track('unrelated', 'Another artist', 'Bandstand', '', 180)
    store.tracks.upsert_track(None, 'Unresolved', 'Band', '', 180,
                             artist_browse_id='UC_band', source_ref='unresolved')

    assert {song['title'] for song in store.charts.story_tracks(since=0, artist='Band')} == {
        'Together', 'Solo'}
    assert store.charts.story_tracks(since=0, artist='Unknown') == []
    assert store.charts.story_tracks(since=0) == []


def test_story_examples_deduplicate_displayed_songs_before_limiting(store):
    iid = store.identities.upsert_identity('me', 'c', None, True)
    for video, title in [('first', 'Wise One'), ('copy', 'Wise One'),
                         ('remaster', 'Wise One (2020 Remaster)'),
                         ('other', 'Giant Steps'), ('live', 'Wise One (Live)')]:
        entry = store.tracks.upsert_track(video, title, 'John Coltrane', '', 180)
        store.tracks.set_track_enrichment(entry, 'Free jazz', None)
    # Repeated exact uploads win the history ranking, yet must not consume every slot.
    for index, video in enumerate(['first', 'copy', 'remaster', 'other', 'live']):
        key = store.tracks.identity_key_for_video(video)
        store.history.import_listening_events(iid, [(key, video, 10 * 86400 - index * 86400)])
    recordings = store.conn.execute('SELECT COUNT(*) FROM recordings').fetchone()[0]
    for selection in ({'artist': 'John Coltrane'}, {'genres': ['Free jazz']}):
        songs = store.charts.story_tracks(since=0, **selection)
        assert [song['video_id'] for song in songs] == ['first', 'other', 'live']
        assert store.charts.story_tracks(since=0, limit=0, **selection) == []
    assert store.conn.execute('SELECT COUNT(*) FROM recordings').fetchone()[0] == recordings == 5
    assert store.conn.execute('SELECT COUNT(*) FROM play_events').fetchone()[0] == 5


def test_story_activity_requires_recent_listening_and_preserves_collaboration_credits(store):
    now, day = 1_800_000_000, 86400
    iid = store.identities.upsert_identity('me', 'c', None, True)
    entry = store.tracks.upsert_track('collab', 'Together', 'Band & Guest', '', 180,
        credits=[{'name': 'Band', 'id': 'UC_band'}, {'name': 'Guest', 'id': 'UC_guest'}])
    store.tracks.upsert_track('old', 'Old', 'Old artist', '', 180)
    store.tracks.upsert_track('unplayed', 'Unplayed', 'Unplayed artist', '', 180)
    with store.conn:
        store.conn.execute("UPDATE catalog_tracks SET genre='Indie Pop' WHERE id=?", (entry,))
    playlist = store.playlists.upsert_playlist(iid, 'PL', 'Mix', 1, 'h', now)
    store.playlists.set_playlist_tracks(playlist, [entry])
    key = store.tracks.identity_key_for_video('collab')
    store.history.import_listening_events(iid, [
        (key, None, now - day), (key, None, now - 2 * day),
        (store.tracks.identity_key_for_video('old'), None, now - 31 * day),
    ], source='spotify_export', source_timezone='UTC')

    activity = store.charts.story_activity(now - 14 * day)
    assert set(activity['artist']) == {'Band & Guest', 'Band', 'Guest'}
    assert {evidence['plays'] for evidence in activity['artist'].values()} == {2}
    assert activity['genre']['Indie Pop']['plays'] == 2
