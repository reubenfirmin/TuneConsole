"""Replay captured playlists into an empty library, without provider requests.

Run through scripts/test-library.py against its immutable actual-library baseline.
This measures local acquisition work; it does not simulate provider latency.
Optional saved-song stress input is synthesized from playable library entries;
the baseline does not establish their native saved-song or account ownership.
"""
import argparse
import cProfile
import json
from pathlib import Path
import pstats
import sqlite3
from tempfile import TemporaryDirectory
import time

from tests.conftest import FakeClient
from tests.store_fixture_support import consumer_schema, initialize_consumer
from yt_playlist.core.album_snapshot import PlaylistSnapshot
from yt_playlist.core.store import Store
from yt_playlist.library.sync import sync_all
from yt_playlist.util.music_catalog import song


def provider_track(row):
    return {'videoId': row['video_id'], 'title': row['title'],
        'artists': [{'name': row['artist'], 'id': row['artist_browse_id']}],
        'album': {'name': row['album'], 'id': row['album_browse_id']},
        'duration_seconds': row['duration_s'], 'isAvailable': row['available'] != 0,
        'videoType': row['video_type'], 'likeStatus': 'INDIFFERENT',
        'thumbnails': [{'url': row['thumbnail']}] if row['thumbnail'] else []}


def run(source, profile, saved_song_stress):
    with sqlite3.connect(f'file:{source}?mode=ro', uri=True) as original:
        original.row_factory = sqlite3.Row
        playlists = [dict(row) for row in original.execute('SELECT * FROM playlists ORDER BY id')]
        entries = {row['id']: provider_track(row) for row in original.execute('SELECT * FROM catalog_tracks WHERE video_id IS NOT NULL')}
        contents = {row['id']: [entries[item[0]] for item in original.execute(
            'SELECT track_id FROM playlist_tracks WHERE playlist_id=? ORDER BY position', (row['id'],))
            if item[0] in entries] for row in playlists}
        owned = [entries[row[0]] for row in original.execute('SELECT entry_id FROM library_memberships') if row[0] in entries]
        playable = [row for row in owned if song(row) is not None]
        songs = playable if saved_song_stress else []
    with TemporaryDirectory(prefix='onboarding-import-', dir='/reports') as workspace, consumer_schema() as schema:
        store = Store(Path(workspace) / 'fresh.db')
        initialize_consumer(store, schema)
        try:
            if profile:
                rows = playable[:profile]
                profiler = cProfile.Profile()
                start = time.monotonic()
                profiler.enable()
                for raw in rows:
                    store.tracks.upsert_track(raw['videoId'], raw['title'], raw['artists'][0]['name'],
                        raw['album']['name'], raw['duration_seconds'], available=raw['isAvailable'],
                        video_type=raw['videoType'], artist_browse_id=raw['artists'][0]['id'],
                        album_browse_id=raw['album']['id'],
                        thumbnail=(raw['thumbnails'] or [{}])[0].get('url'), created_at=1)
                profiler.disable()
                with open('/reports/import-profile.txt', 'w') as output:
                    pstats.Stats(profiler, stream=output).sort_stats('cumtime').print_stats(50)
                print(json.dumps(dict(profiled_tracks=len(rows), seconds=time.monotonic() - start)))
                return
            clients = {}
            for account in sorted({row['account_id'] for row in playlists}):
                identity = store.accounts.upsert_account(f'Account {account}', 'fixture', None, not clients)
                listing = [{'playlistId': row['provider_playlist_id'], 'title': row['title'], 'count': len(contents[row['id']])}
                           for row in playlists if row['account_id'] == account]
                tracks = {row['provider_playlist_id']: contents[row['id']] for row in playlists if row['account_id'] == account}
                class CapturedClient(FakeClient):
                    def get_library_songs(self, limit=None):
                        return songs

                    def get_library_albums(self, limit=None):
                        return PlaylistSnapshot()
                clients[identity] = CapturedClient(playlists=listing, tracks=tracks)
            first = None
            start = time.monotonic()
            def progress(event):
                nonlocal first
                if first is None and event.get('completed'):
                    first = time.monotonic() - start
                if event.get('completed') and event['completed'] % 10 == 0:
                    print(json.dumps(dict(stage=event.get('stage'), completed=event['completed'], total=event['total'],
                                          seconds=time.monotonic() - start)), flush=True)
            result = sync_all(store, clients, now=1, on_progress=progress, automatic=True)
            report = dict(source_playlists=len(playlists), playlist_occurrences=sum(map(len, contents.values())),
                source_owned_entries=len(owned), playable_owned_entries=len(playable),
                saved_song_stress=saved_song_stress, synthesized_saved_song_occurrences=len(songs) * len(clients),
                accounts=len(clients), status=result['status'], first_playlist_seconds=first,
                seconds=time.monotonic() - start,
                imported_songs=store.conn.execute('SELECT count(*) FROM library_memberships').fetchone()[0],
                foreign_key_failures=[tuple(row) for row in store.conn.execute('PRAGMA foreign_key_check')],
                account_resources={account: {name: resource['status'] for name, resource in value['resources'].items()}
                                   for account, value in result['accounts'].items()})
            Path('/reports/import.json').write_text(json.dumps(report, indent=2) + '\n')
            print(json.dumps(report), flush=True)
            assert not report['foreign_key_failures']
            assert result['status'] == 'complete', report
        finally:
            store.conn.close()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', default='/library/state.db')
    parser.add_argument('--profile', type=int, default=0)
    parser.add_argument('--saved-song-stress', action='store_true')
    arguments = parser.parse_args()
    run(arguments.source, arguments.profile, arguments.saved_song_stress)
