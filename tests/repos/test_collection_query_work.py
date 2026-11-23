"""Bound SQLite work as the library grows; wall-clock limits hide quadratic joins."""
import datetime as dt

import pytest

from tests.store_fixture_support import bounded_consumer_schema, without_query_timing
from yt_playlist.core.activity import interrupt_query
from yt_playlist.core.albums import observe
from yt_playlist.core.artists import ensure
from yt_playlist.core import listening


pytestmark = pytest.mark.usefixtures('without_query_timing')


@pytest.fixture(autouse=True)
def disable_periodic_library_sync():
    # SQL work bounds do not need an HTTP application or background workers.
    pass


@pytest.fixture(scope='module')
def _pristine_current_schema():
    with bounded_consumer_schema(()) as schema:
        yield schema


def _seed_library(store, keys):
    songs = [(i, key, f'Song {i:04}', 'Artist') for i, key in enumerate(keys)]
    store.conn.executemany('INSERT INTO recordings(id,identity_key,title,artist,unresolved_genre) '
        "VALUES (?,?,?,?,'House')", songs)
    store.conn.executemany(
        'INSERT INTO track_sources(id,recording_id,video_id,title,artist,album,duration_s,thumbnail,mb_year,library_origin) '
        "VALUES (?,?,?,?,?,'Album',180,'cover','1991','fixture')",
        [(i, i, f'v{i}', title, artist) for i, _, title, artist in songs])


def test_library_reads_do_not_rescan_history_or_containers_per_song(store):
    account = store.accounts.upsert_account('main', None, True)
    playlists = [store.playlists.upsert_playlist(account, f'PL{i}', f'Playlist {i}', 0, 'h', 0)
                 for i in range(300)]
    entries = list(range(1, 601))
    keys = [f'song {entry}|artist' for entry in entries]
    with store.conn:
        artist = ensure(store.conn, 'Artist')
        store.conn.executemany('INSERT INTO recordings(id,identity_key,title,artist) VALUES (?,?,?,?)',
            [(entry, key, f'Song {entry}', 'Artist') for entry, key in zip(entries, keys)])
        store.conn.executemany('INSERT INTO track_sources(id,recording_id,video_id,title,artist) VALUES (?,?,?,?,?)',
            [(entry, entry, f'v{entry}', f'Song {entry}', 'Artist') for entry in entries])
        store.conn.executemany("UPDATE track_sources SET library_origin='fixture' WHERE id=?",
            [(entry,) for entry in entries])
        store.conn.executemany("INSERT INTO entry_artists VALUES (?,0,?,'Artist','fixture')",
            [(entry, artist) for entry in entries])
        album = observe(store.conn, 'B', title='Release', artist='Artist', rank=1)
        store.conn.executemany("INSERT INTO album_item_observations(album_id,entry_id,provider) VALUES (?,?,'youtube')",
            [(album, entry) for entry in entries])
    store.playlists.set_playlist_tracks(playlists[0], entries)
    store.collection.add_saved_album({'browse': 'B', 'title': 'Release', 'artist': 'Artist'})
    store.history.record_history_plays(account, 86400, keys, source_timezone='UTC')
    for i in range(300):
        store.player_events.record_player_event(account, 'tick', 'v1', 12, 100, f'PL{i}', '{}', 100000 + i)

    operations = [
        ('playlist sessions', store.charts.get_playlist_playback_stats,
         lambda rows: rows[playlists[0]] == (100000, 1) and len(rows) == 300),
        ('album recency', store.collection.saved_albums_recency,
         lambda rows: rows == {'B': 86400 + 43200}),
        ('artist songs', lambda: store.charts.artist_songs('Artist'),
         lambda rows: len(rows) == 600 and all(row['plays'] == 1 for row in rows)),
        ('playlist songs', lambda: store.charts.playlist_tracks_detail(playlists[0]),
         lambda rows: len(rows) == 600 and all(row['plays'] == 1 for row in rows)),
        ('album songs', lambda: store.charts.album_tracks_detail('B'),
         lambda rows: len(rows) == 600 and all(row['plays'] == 1 for row in rows)),
        ('story songs', lambda: store.charts.story_tracks(since=0, artist='Artist'),
         lambda rows: len(rows) == 3 and all(row['recent_plays'] == 1 for row in rows)),
        ('enrichment priorities', lambda: store.enrichment_queue._select_enrich_batch(40, now=200000),
         lambda rows: len(rows) == 40 and [row['recording_id'] for row in rows] == entries[:40]),
    ]
    for name, operation, valid in operations:
        steps = 0

        def bounded():
            nonlocal steps
            steps += 1000
            return int(steps > 3_000_000)

        # Reuse the owned connection so this counter covers the full SQL call.
        # Reader admission and cleanup have separate file-backed coverage.
        with store._lock:
            store.conn.set_progress_handler(bounded, 1000)
            try:
                result = operation()
            finally:
                store.conn.set_progress_handler(interrupt_query, 1000)
        assert valid(result), name


def test_provenance_reads_do_not_rescan_independent_history_per_generated_song(store):
    account = store.accounts.upsert_account('main', None, True)
    store.history.record_history_plays(account, 86400,
        [f'independent {i}' for i in range(600)], source_timezone='UTC')
    with store.conn:
        for i in range(600):
            event = listening.append_event(store.conn, account, f'generated {i}',
                100000, is_generated=1)
            listening.project_event(store.conn, event, account, 100000, timezone='UTC', source='live')
    keys = {f'generated {i}' for i in range(600)}
    steps = 0

    def bounded():
        nonlocal steps
        steps += 1000
        return int(steps > 3_000_000)

    with store._lock:
        store.conn.set_progress_handler(bounded, 1000)
        try:
            assert store.rec_evidence.generated_only_keys_between(86400, 2 * 86400) == keys
        finally:
            store.conn.set_progress_handler(interrupt_query, 1000)


def test_weighting_indexes_computed_history_keys(store):
    # Model the computed keys in installed history projections.
    with store.conn:
        _seed_library(store, (f'key-{i}' for i in range(1200)))
        store.conn.execute('CREATE TEMP TABLE observations(identity_key TEXT,at INTEGER)')
        # A second upload cannot multiply the genre/year weight of its song.
        store.conn.execute('INSERT INTO track_sources(recording_id,video_id,title,artist,mb_year) '
            "VALUES (0,'duplicate','Song 0000','Artist','1991')")
        store.conn.executemany('INSERT INTO observations VALUES (?,?)',
            [(f'key-{i}', day) for i in range(1000) for day in (86400, 172800)])
        store.conn.execute("CREATE TEMP VIEW listening_days AS SELECT identity_key||'' identity_key FROM observations")
        store.conn.execute('CREATE TEMP VIEW listening_day_counts AS '
            'SELECT identity_key,COUNT(*) plays FROM listening_days GROUP BY identity_key')
    operations = [
        (store.rec_query.genre_play_distribution, {'House': 3200}),
        (store.rec_query.era_play_distribution, {'1990': 3200}),
    ]
    for operation, expected in operations:
        steps = 0
        def bounded():
            nonlocal steps
            steps += 1000
            return int(steps > 500_000)
        # TEMP projections and this work counter belong to the owned connection.
        with store._lock:
            store.conn.set_progress_handler(bounded, 1000)
            try:
                assert operation() == expected
            finally:
                store.conn.set_progress_handler(interrupt_query, 1000)


def test_charts_and_candidates_index_computed_history_keys(store):
    # Keep the history key untyped, as in installed compatibility projections.
    # A modest cohort must stay cheap even when many songs share one playlist.
    days = [('1970-01-02', 86400 + 43200), ('1970-01-03', 2 * 86400 + 43200)]
    revisit_last_played = dt.datetime(1970, 1, 4, 12, tzinfo=dt.timezone.utc).timestamp()
    with store.conn:
        _seed_library(store, (f'key-{i:04}' for i in range(1200)))
        store.conn.execute('CREATE TEMP TABLE observations(identity_key TEXT,day TEXT,at INTEGER)')
        store.conn.executemany('INSERT INTO observations VALUES (?,?,?)',
            [(f'key-{i:04}', day, at) for i in range(1000) for day, at in days])
        store.conn.execute("CREATE TEMP VIEW listening_days AS SELECT identity_key||'' identity_key, "
            'at day_ts,day,1 account_id FROM observations')
        store.conn.execute("CREATE TEMP VIEW listening_activity AS SELECT identity_key||'' identity_key,at FROM observations")
        store.conn.execute('CREATE TEMP TABLE playlist_tracks(playlist_id INTEGER,track_id INTEGER, '
            'PRIMARY KEY(playlist_id,track_id))')
        store.conn.execute('CREATE INDEX temp.playlist_entry ON playlist_tracks(track_id)')
        store.conn.executemany('INSERT INTO playlist_tracks VALUES (999,?)', [(i,) for i in range(1200)])
    operations = [
        ('playlist recency', store.charts.get_playlist_track_recency,
         lambda rows: rows == {999: [days[-1][1]] * 1000 + [None] * 200}),
        ('genre representative', lambda: store.charts.genre_representative(['House']),
         lambda row: row == {'artist': 'Artist', 'thumbnail': 'cover'}),
        ('chart tracks', store.charts.top_tracks,
         lambda rows: [row['title'] for row in rows] == [f'Song {i:04}' for i in range(100)]
             and all(row['plays'] == 2 for row in rows)),
        ('revisit', lambda: store.rec_candidates.revisit_candidates(100 * 86400),
         lambda rows: [row['key'] for row in rows] == [f'key-{i:04}' for i in range(90)]
             and all(row['plays'] == 2 and row['last_played'] == revisit_last_played for row in rows)),
        ('rotation', store.rec_candidates.more_like_rotation,
         lambda rows: [row['key'] for row in rows] == [f'key-{i:04}' for i in range(1000, 1040)]
             and all(row['plays'] == 0 and row['shared_playlists'] == 1 for row in rows)),
        ('deep cuts', store.rec_candidates.deep_cuts,
         lambda rows: len(rows) == 1 and rows[0]['key'] == 'key-1000'
             and rows[0]['plays'] == 0 and rows[0]['artist_plays'] == 2000),
    ]
    for name, operation, valid in operations:
        steps = 0
        def bounded():
            nonlocal steps
            steps += 1000
            return int(steps > 3_000_000)
        with store._lock:
            store.conn.set_progress_handler(bounded, 1000)
            try:
                result = operation()
            finally:
                store.conn.set_progress_handler(interrupt_query, 1000)
        assert valid(result), name
        assert steps <= 500_000, (name, steps)


def test_album_familiarity_does_not_scan_all_events_per_imported_day(store):
    account = store.accounts.upsert_account('main', None, True)
    entry = store.tracks.upsert_track('v', 'Song', 'Artist', 'Release', 180, album_browse_id='B')
    subject = store.tracks.catalog_subject(entry)
    key = subject['identity_key']
    # The query consumes 600 completed calendar days. Parsing and recounting a
    # separate controller import for every day is outside this SQL work bound.
    days = [dt.datetime.fromtimestamp(day * 86400, dt.timezone.utc).date().isoformat()
            for day in range(1, 601)]
    with store.conn:
        listening.calendar(store.conn, account, 'UTC')
    store.history.repository.record_days(account, 600 * 86400,
        [(key, key, subject['recording_id'], day, day, 'independent') for day in days], 'UTC')
    with store.conn:
        for i in range(600):
            # These unrelated exact events cannot erase the album's imports.
            event = listening.append_event(store.conn, account, 'unrelated', 86400 + i)
            listening.project_event(store.conn, event, account, 86400 + i, timezone='UTC', source='live')
    steps = 0
    def bounded():
        nonlocal steps
        steps += 1000
        return int(steps > 500_000)
    with store._lock:
        store.conn.set_progress_handler(bounded, 1000)
        try:
            assert store.collection.album_listening_summary(['B']) == {
                'B': {'plays': 600, 'tracks': 1, 'complete': False}}
        finally:
            store.conn.set_progress_handler(interrupt_query, 1000)


def test_prepared_revisit_checks_bound_computed_exact_day_history(store):
    # Compatibility projections compute identity keys. A small prepared pool
    # must not repeatedly scan every exact event to reconcile its imported days.
    with store.conn:
        _seed_library(store, (f'revisit-{i:04}' for i in range(120)))
        store.conn.execute('CREATE TEMP TABLE exact_history(id INTEGER PRIMARY KEY,identity_key TEXT, '
            'account_id INTEGER,played_at INTEGER,day TEXT)')
        selected = [f'revisit-{i:04}' for i in range(12)]
        first_day, second_day = '1970-01-02', '1970-01-03'
        events = [(key, 1, 86400 + offset, first_day) for key in selected for offset in (1, 2)]
        events += [(f'revisit-{12 + i % 108:04}', 1, 86400 + i, first_day) for i in range(10000)]
        store.conn.executemany('INSERT INTO exact_history(identity_key,account_id,played_at,day) '
            'VALUES (?,?,?,?)', events)
        store.conn.execute("CREATE TEMP VIEW play_event_subjects AS SELECT id,identity_key||'' identity_key, "
            'account_id,played_at FROM exact_history')
        store.conn.execute('CREATE TEMP VIEW play_event_calendar AS SELECT id event_id,day FROM exact_history')
        store.conn.execute('CREATE TEMP TABLE imported_history(identity_key TEXT,account_id INTEGER,day TEXT, '
            'day_ts INTEGER)')
        imports = [(key, 1, first_day, 86400 + 43200) for key in selected]
        imports += [(key, 2, first_day, 86400 + 43200) for key in selected[1:]]
        imports += [(key, 1, second_day, 2 * 86400 + 43200) for key in selected[2:]]
        store.conn.executemany('INSERT INTO imported_history VALUES (?,?,?,?)', imports)
        store.conn.execute("CREATE TEMP VIEW listening_days AS SELECT identity_key||'' identity_key, "
            'account_id,day,day_ts FROM imported_history')
    steps = 0

    def bounded():
        nonlocal steps
        steps += 1000
        return int(steps > 250_000)

    first_cutoff = dt.datetime(1970, 1, 3, 12, tzinfo=dt.timezone.utc).timestamp()
    second_cutoff = dt.datetime(1970, 1, 4, 12, tzinfo=dt.timezone.utc).timestamp()
    now_at_cutoff = second_cutoff + 30 * 86400
    with store._lock:
        store.conn.set_progress_handler(bounded, 1000)
        try:
            rows = store.rec_candidates.revisit_candidates(100 * 86400, keys=selected)
        finally:
            store.conn.set_progress_handler(interrupt_query, 1000)
        before_cutoff = store.rec_candidates.revisit_candidates(now_at_cutoff - 1, keys=selected)
        at_cutoff = store.rec_candidates.revisit_candidates(now_at_cutoff, keys=selected)
    assert [row['key'] for row in rows] == selected
    assert [(row['plays'], row['last_played']) for row in rows] == [
        (2, 86402), (3, first_cutoff), *[(4, second_cutoff)] * (len(selected) - 2),
    ]
    assert [row['key'] for row in before_cutoff] == selected[:2]
    assert [row['key'] for row in at_cutoff] == selected
