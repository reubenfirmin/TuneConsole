from yt_playlist.core.catalog_query import LIBRARY_RECORDINGS_SQL


def _flags(store, entry):
    recording = store.tracks.catalog_subject(entry)['recording_id']
    row = store.conn.execute(
        'SELECT has_saved_source,has_eligible_listen,in_collection_album FROM recordings WHERE id=?',
        (recording,),
    ).fetchone()
    assert (recording in _existing_members(store)) == any(row)
    return tuple(row)


def _existing_members(store):
    return {row[0] for row in store.conn.execute(LIBRARY_RECORDINGS_SQL)}


def _is_discovery(store, entry):
    recording = store.tracks.catalog_subject(entry)['recording_id']
    return store.conn.execute('SELECT is_discovery FROM recordings WHERE id=?', (recording,)).fetchone()[0]


def test_discovery_flag_clears_when_a_track_is_saved_or_removed_from_discovery(store):
    entry = store.tracks.upsert_track('song', 'Song', 'Artist', library=False)
    assert _is_discovery(store, entry) == 0
    key = store.tracks.identity_key_for_video('song')
    store.discovery.upsert_discovered_track(key, 'song', 'Song', 'Artist', None, None, None, None, None, 1)
    assert _is_discovery(store, entry) == 1

    store.tracks.upsert_track('song', 'Song', 'Artist')
    assert _is_discovery(store, entry) == 0
    assert key in store.discovery.discovered_track_keys()
    store.tracks.remove_song_library_membership('song')
    assert _is_discovery(store, entry) == 1

    store.discovery.repository.delete_pool_entries((), (), (key,))
    assert _is_discovery(store, entry) == 0


def test_saved_source_moves_without_retiring_its_recording(store):
    old = store.tracks.upsert_track('a', 'Old', 'Artist', available=False)
    corrected = store.tracks.upsert_track('b', 'Correct', 'Artist', library=False)
    assert _flags(store, old) == (1, 0, 0)
    assert _flags(store, corrected) == (0, 0, 0)
    previous = store.tracks.catalog_subject(old)['recording_id']
    target = store.tracks.catalog_subject(corrected)['recording_id']
    store.tracks.associate_youtube_item('a', target, source='review', evidence={'verified': True}, now=1)
    assert tuple(store.conn.execute('SELECT has_saved_source FROM recordings WHERE id=?', (previous,)).fetchone()) == (0,)
    assert _flags(store, corrected) == (1, 0, 0)
    store.tracks.remove_song_library_membership('a')
    assert _flags(store, corrected) == (0, 0, 0)


def test_generated_play_can_disqualify_coarse_history_until_removed(store):
    store.accounts.upsert_account('main', None, True)
    entry = store.tracks.upsert_track('song', 'Song', 'Artist', library=False)
    key = store.tracks.identity_key_for_video('song')
    time = 10 * 86400
    store.history.record_history_plays(1, time, [(key, 'Today')], source_timezone='UTC')
    assert _flags(store, entry) == (0, 1, 0)
    store.playlists.upsert_playlist(1, 'generated', 'Generated', 0, 'h', 1)
    store.playlists.mark_generated_playlist('generated')
    store.history.record_play_event(1, key, 'song', time, playlist_ytm_id='generated', source_timezone='UTC')
    assert _flags(store, entry) == (0, 0, 0)
    with store.conn:
        store.conn.execute('DELETE FROM play_events')
    assert _flags(store, entry) == (0, 1, 0)
    with store.conn:
        store.conn.execute('DELETE FROM listening_observations')
    assert _flags(store, entry) == (0, 0, 0)


def test_unresolved_evidence_shadows_and_restores_only_the_matching_account_and_day(store):
    main = store.accounts.upsert_account('main', None, True)
    other = store.accounts.upsert_account('other', None, False)
    entry = store.tracks.upsert_track('song', 'Song', 'Artist', library=False)
    subject = store.tracks.catalog_subject(entry)
    recording, key = subject['recording_id'], subject['identity_key']
    day = '2026-01-02'
    conn = store.conn

    def observe(account, source, resolved):
        return conn.execute('''INSERT INTO listening_observations(account_id,raw_identity_key,recording_id,
            unresolved_subject_key,source_day,day,source,timezone,precision,first_observed_at,last_observed_at)
            VALUES (?,?,?,?,?,?,?,'UTC','calendar_day',1,1)''',
            (account, key, recording if resolved else None, None if resolved else key, day, day, source)).lastrowid

    def play(generated, stamp):
        return conn.execute('''INSERT INTO play_events(account_id,raw_identity_key,unresolved_subject_key,
            played_at,is_generated,day) VALUES (?,?,?,?,?,?)''', (main, key, key, stamp, generated, day)).lastrowid

    def check(eligible):
        assert _flags(store, entry) == (0, eligible, 0)
        evidence = conn.execute('SELECT 1 FROM eligible_listening_evidence WHERE recording_id=? LIMIT 1',
                                (recording,)).fetchone()
        assert bool(evidence) == eligible

    with conn:
        generated = play(1, 1)
        check(0)
        history = observe(main, 'ytm_history', True)
        check(1)
        conn.execute("UPDATE play_events SET sources='[\"live\"]' WHERE id=?", (generated,))
        check(0)

        independent = observe(other, 'independent', False)
        check(0)
        conn.execute('UPDATE listening_observations SET account_id=? WHERE id=?', (main, independent))
        check(1)
        conn.execute("UPDATE listening_observations SET day='2026-01-01' WHERE id=?", (independent,))
        check(0)

        organic = play(None, 2)
        check(0)
        conn.execute("UPDATE play_events SET sources='[\"live\"]' WHERE id=?", (organic,))
        check(1)
        conn.execute('DELETE FROM play_events WHERE id=?', (organic,))
        check(0)
        conn.execute('DELETE FROM play_events WHERE id=?', (generated,))
        check(1)
        conn.execute('DELETE FROM listening_observations WHERE id=?', (history,))
        check(0)


def test_collection_tracks_follow_playlist_evidence_and_album_publication(store):
    store.accounts.upsert_account('main', None, True)
    first = store.tracks.upsert_track('a', 'First', 'Artist', library=False)
    second = store.tracks.upsert_track('b', 'Second', 'Artist', library=False)
    key = store.tracks.identity_key_for_video('b')
    store.discovery.upsert_discovered_track(key, 'b', 'Second', 'Artist', None, None, None, None, None, 1)
    playlist = store.playlists.upsert_playlist(1, 'playlist', 'Playlist', 0, 'h', 1)

    def publish(*entries):
        store.collection.cache_album_tracklist('album', 'Album', 'Artist', None, [],
                                               entries=[{'entry_id': entry} for entry in entries])

    publish(first, second)
    assert _flags(store, second)[2] == 0
    assert _is_discovery(store, second) == 1
    store.playlists.set_playlist_tracks(playlist, [first])
    assert _flags(store, second)[2] == 1
    assert _is_discovery(store, second) == 0
    store.playlists.set_playlist_tracks(playlist, [])
    assert _flags(store, second)[2] == 0
    assert _is_discovery(store, second) == 1
    album = store.collection.album_id('album')
    with store.conn:
        store.conn.execute("INSERT INTO account_albums(account_id,album_id,source) VALUES(1,?,'test')", (album,))
    assert _flags(store, second)[2] == 1
    publish(first)
    assert _flags(store, second)[2] == 0
    assert _flags(store, first)[2] == 1
    with store.conn:
        store.conn.execute('DELETE FROM account_albums WHERE album_id=?', (album,))
    assert _flags(store, first)[2] == 0


