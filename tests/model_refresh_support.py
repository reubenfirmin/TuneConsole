"""Completed catalog inputs shared by model and projection tests."""
from tests.recommendation_support import seed_publication_subjects
from yt_playlist.core.db_writes import atomic_write
from yt_playlist.rec import rec_params


def seed(store, *, distinct_albums=False, track_count=96, mode_count=3):
    # Keep the original 96 subjects, three genres, eight overlapping baskets
    # and audio. Catalog ingestion is below model publication and invalidation.
    items = [dict(key=f'song {i:03}|artist {i}', video_id=f'v{i}', title=f'Song {i:03}',
                  artist=f'Artist {i}', album=f'Album {i}' if distinct_albums else 'Album',
                  genre=('Techno', 'Jazz', 'Rock')[i * 3 // track_count]) for i in range(track_count)]
    seed_publication_subjects(store, items)
    with atomic_write(store):
        iid = store.accounts.upsert_account('Listener', 'fixture', None, True)
        tids = [row[0] for row in store.conn.execute('SELECT id FROM catalog_entries ORDER BY id')]
        first_artist = store.conn.execute('SELECT COALESCE(MAX(id),0)+1 FROM artists').fetchone()[0]
        credits = [(first_artist + i, tid, item['artist']) for i, (tid, item) in enumerate(zip(tids, items))]
        store.conn.executemany('INSERT INTO artists(id,name,provisional) VALUES (?,?,1)',
                               [(artist, name) for artist, _, name in credits])
        store.conn.executemany("INSERT INTO artist_aliases VALUES (?,?,'fixture')",
                               [(artist, name.lower()) for artist, _, name in credits])
        store.conn.executemany("INSERT INTO entry_artists VALUES (?,0,?,?,'fixture')",
                               [(tid, artist, name) for artist, tid, name in credits])
        store.conn.executemany('UPDATE recordings SET bpm=?,energy=? WHERE id=?',
                               [(80 + i, (i % 10) / 10, tid) for i, tid in enumerate(tids)])
        for i in range(8):
            size, offset = track_count // 4, i * (track_count // 8)
            pid = store.playlists.upsert_playlist(iid, f'p{i}', f'Playlist {i}', size, '', 1)
            store.playlists.set_playlist_tracks(pid, (tids + tids)[offset:offset + size])
        store.settings.set_setting('rec_dim', '16')
        rec_params.set_param(store, 'modes_k', mode_count)
        rec_params.set_param(store, 'modes_min_members', 8)
    # History commits after the completed catalog input, through its own owner.
    store.history.import_play_events(iid, [(items[i]['key'], f'v{i}', 1000 + i)
                                         for i in range(8)])
    return tids
