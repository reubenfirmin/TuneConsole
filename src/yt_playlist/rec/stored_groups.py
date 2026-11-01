"""Adaptive group choices over indexed immutable memberships and bounded pages."""
from collections import Counter
from contextlib import contextmanager
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory

from yt_playlist.core.activity import check_cancellable_work
from yt_playlist.rec import group_roots, home_vibe, mode_surfaces, musical_character, rec_params
from yt_playlist.rec.eligibility import home_exclusions
from yt_playlist.util import genre_map


def pages(reader, generation, version, lane):
    after = -1
    while True:
        check_cancellable_work()
        page = reader.recommendations.candidate_page('home', 'all', lane, generation=generation,
            group_version=version, after=after, limit=128)
        yield page['tracks']
        after = page['after']
        if page['exhausted']:
            break


def preview_frame(reader, generation, version, lane, now, artist_cap, album_cap, muted):
    artists, albums, count, tracks = Counter(), Counter(), 0, []
    for page in pages(reader, generation, version, lane):
        blocked = home_exclusions(reader, [track['key'] for track in page], now)
        for track in page:
            artist, album = track.get('artist') or '', track.get('album') or ''
            if (track['key'] in blocked or artist in muted or artists[artist] >= artist_cap
                    or album and albums[album] >= album_cap):
                continue
            artists[artist] += 1
            if album:
                albums[album] += 1
            count += 1
            if len(tracks) < mode_surfaces.PROTO_SIZE:
                tracks.append({field: track.get(field) for field in ('key', 'title', 'artist')})
    return {'eligible': count, 'full_mix': count >= mode_surfaces.PROTO_SIZE, 'tracks': tracks}


@contextmanager
def selection(store, generation, now):
    close = (None if getattr(store, '_read_only', False) else
             group_roots.pending(store, {'recommendation_generations': [generation]}, now))
    try:
        if not getattr(store, '_read_only', False):
            with store.read_view() as reader, selection_reader(reader, generation, now) as selected:
                yield selected
        else:
            with selection_reader(store, generation, now) as selected:
                yield selected
    finally:
        if close:
            close()


@contextmanager
def selection_reader(reader, generation, now):
    with TemporaryDirectory(prefix='tuneconsole-group-cards-') as directory:
        conn = sqlite3.connect(Path(directory) / 'ordering.db')
        try:
            conn.row_factory = sqlite3.Row
            conn.execute('PRAGMA cache_size=-1024')
            conn.execute('PRAGMA temp_store=FILE')
            conn.execute('PRAGMA mmap_size=0')
            conn.execute('CREATE TABLE ordering(rank INTEGER PRIMARY KEY,key TEXT,artist TEXT,album TEXT,'
                         'bucket INTEGER,taste REAL,fit REAL)')
            yield Selection(reader, generation, now, conn)
        finally:
            conn.close()


class Selection:
    def __init__(self, reader, generation, now, conn):
        self.reader, self.generation, self.now, self.conn = reader, generation, now, conn
        self.preset = home_vibe.current(reader, now)
        self.muted = reader.rec_model.muted_artists()
        self.weights = reader.rec_model.get_weights(now=now)
        self.maximum = 60 * rec_params.get_param(reader, 'max_track_minutes')

    def eligible(self, page, lane, epoch, include, avoid, seen, meta):
        blocked = home_exclusions(self.reader, [track['key'] for track in page], self.now)
        for track in page:
            genre = track.get('genre') or ''
            family, subgenre = genre_map.family(genre,conn=self.reader.conn), genre_map.subgenre(genre,conn=self.reader.conn)
            if (track.get('_radio_fresh') or track['key'] in seen or track['key'] in blocked
                    or track.get('artist') in self.muted
                    or self.weights.get(f'genre:{family}', 1) == 0
                    or self.weights.get(f'genre:{subgenre}', 1) == 0
                    or track.get('duration') and track['duration'] > self.maximum
                    or (include or avoid) and (not genre or family in avoid or include and family not in include)):
                continue
            if lane == 'temporal':
                cuts = meta.get('year_cuts')
                if not cuts or not mode_surfaces._in_band(track.get('year'), epoch % 3, *cuts):
                    continue
            yield track

    def tracks(self, version, lane, epoch, include, avoid, seen, artist_cap, album_cap, meta):
        artists, albums, selected = Counter(), Counter(), []
        limit = mode_surfaces.PROTO_SIZE * 3
        def accept(track):
            artist, album = track.get('artist') or '', track.get('album') or ''
            if artists[artist] >= artist_cap or album and albums[album] >= album_cap:
                return
            selected.append(track)
            artists[artist] += 1
            if album:
                albums[album] += 1

        self.conn.execute('DELETE FROM ordering')
        for page in pages(self.reader, self.generation, version, lane):
            eligible = list(self.eligible(page, lane, epoch, include, avoid, seen, meta))
            if not self.preset:
                for track in eligible:
                    track['serving_context']['vibe_affinity'] = .5
                    accept(track)
                    if len(selected) == limit:
                        return selected
            else:
                # A page owns its profile cache. The full ranking keeps only
                # compact sort keys in SQLite and can spill to disk.
                profiles = musical_character.load(self.reader, [track['key'] for track in eligible])
                rows = []
                for track in eligible:
                    fit = home_vibe.affinity(track, self.preset, profiles)
                    rows.append((track['pool_rank'], track['key'], track.get('artist') or '',
                        track.get('album') or '', -round((fit - .5) / home_vibe.FIT_STEP),
                        -home_vibe.taste_score(track), fit))
                self.conn.executemany('INSERT INTO ordering VALUES (?,?,?,?,?,?,?)', rows)
        if not self.preset:
            return selected
        cursor = self.conn.execute('SELECT * FROM ordering ORDER BY bucket,taste,fit DESC,rank')
        while batch := cursor.fetchmany(128):
            check_cancellable_work()
            for row in batch:
                if artists[row['artist']] >= artist_cap or row['album'] and albums[row['album']] >= album_cap:
                    continue
                track = self.reader.recommendations.candidate_page('home', 'all', lane,
                    generation=self.generation, after=row['rank'] - 1, limit=1)['tracks'][0]
                track['serving_context']['vibe_affinity'] = row['fit']
                accept(track)
                if len(selected) == limit:
                    return selected
        return selected
