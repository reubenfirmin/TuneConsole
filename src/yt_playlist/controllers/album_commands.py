"""Album acquisition, complete landing queries and playback actions."""
from urllib.parse import quote

from yt_playlist.library.album_fetch import is_upload_album, shareable_album_tracks
from yt_playlist.util.duration import parse_duration
from yt_playlist.util.thumbnails import best_thumb, detail_thumb, is_placeholder_art


def album_rows(album, known, browse):
    return [{**known.get(t.get('video_id'), {}),
        'video_id': t.get('video_id') if t.get('title') else '',
        'title': t.get('title') or 'Unavailable track',
        'available': known.get(t.get('video_id'), {}).get('available') if t.get('title') else 0,
        'artist': t.get('artist') or album.get('artist') or '', 'album': album['title'], 'album_browse': browse,
        'artists': t.get('artists') or album.get('artists') or [],
        'thumbnail': t.get('thumbnail') or album.get('thumbnail'), 'duration': parse_duration(t.get('duration')),
        'genre': known.get(t.get('video_id'), {}).get('genre', ''),
        'year': known.get(t.get('video_id'), {}).get('year') or album.get('year') or '',
        'liked': known.get(t.get('video_id'), {}).get('liked', False),
        'plays': known.get(t.get('video_id'), {}).get('plays')} for t in album.get('tracks') or []]


class AlbumCommands:
    def __init__(self, collection, tracks, charts, genres, reports, notes, ledger, jobs, *,
                 client, fetch, remember, now, paused, respond, present, feedback, feedback_state,
                 prepared, read_notes, refresh, player, cover, logger):
        self.collection, self.tracks, self.charts, self.genres = collection, tracks, charts, genres
        self.reports, self.notes, self.ledger, self.jobs = reports, notes, ledger, jobs
        self.client, self.fetch, self.remember = client, fetch, remember
        self.now, self.paused, self.respond, self.present = now, paused, respond, present
        self.feedback, self.feedback_state, self.prepared = feedback, feedback_state, prepared
        self.read_notes, self.refresh, self.player, self.cover, self.logger = read_notes, refresh, player, cover, logger

    def acquire(self, browse):
        client = self.client(browse)
        return self.fetch(client, browse) if client and browse else {}

    def live(self, browse):
        a = self.acquire(browse)
        if not a or not a.get('title'):
            return None
        if not self.paused():
            self.remember(browse, a)
        return dict(title=a.get('title'), artist=', '.join(x.get('name', '') for x in a.get('artists') or []),
            artists=a.get('artists') or [],
            audio_playlist_id=a.get('audioPlaylistId'), year=a.get('year'), thumbnail=best_thumb(a.get('thumbnails')),
            tracks=[dict(title=t.get('title'), video_id=t.get('videoId'),
                artist=', '.join(x.get('name', '') for x in t.get('artists') or []),
                artists=t.get('artists') or a.get('artists') or [],
                duration=t.get('duration')) for t in a.get('tracks') or []])

    def context(self, browse, opportunity, *, inline=False, opened=False):
        if opened and opportunity:
            self.respond(opportunity, 'open', self.now(), option_id=browse)
        album = None
        try:
            album = self.live(browse)
        except Exception:
            self.logger.info('album fetch failed for %r (non-fatal)', browse, exc_info=True)
        saved = browse in self.collection.saved_album_ids()
        upload = is_upload_album(browse)
        owned = saved or upload
        if not self.paused() and owned and album:
            for t in album.get('tracks') or []:
                if t.get('video_id') and t.get('title'):
                    self.tracks.upsert_track(t['video_id'], t['title'], t.get('artist') or album.get('artist') or '',
                        album.get('title') or '', parse_duration(t.get('duration')), album_browse_id=browse,
                        thumbnail=album.get('thumbnail'))
        library = self.charts.album_tracks_detail(browse) if browse else []
        tracks = library if owned else []
        complete = self.collection.has_published_album_tracklist(browse)
        fallback = library if complete else tracks if not inline else []
        if fallback and not (album and album.get('title')):
            meta = next((a for a in self.collection.get_saved_albums() if a['browse'] == browse), {})
            album = dict(title=meta.get('title') or fallback[0]['album'] or 'Album',
                artist=meta.get('artist') or fallback[0]['artist'], year=meta.get('year'),
                thumbnail=meta.get('thumbnail') or fallback[0]['thumbnail'], tracks=fallback)
        table = (library if complete else tracks) if album else []
        if album and not complete and (not owned or self.paused()):
            known = {t['video_id']: t for t in self.charts.video_tracks_detail(
                [t['video_id'] for t in album.get('tracks') or [] if t.get('video_id')])}
            table = album_rows(album, known, browse)
        if album and complete:
            album = {**album, 'tracks': table}
        cover, mosaic = self.cover(album, tracks)
        editorial = dict(browse_id=browse, title=(album or {}).get('title'), artist=(album or {}).get('artist'))
        if album and not inline and not self.paused():
            self.notes.request([editorial], self.now(), visible=True)
            self.refresh()
        prepared = self.prepared() or {}
        anchor = next((a for a in prepared.get('album_choices', []) if a['browse_id'] == browse), None)
        companion = anchor.get('companion') or [] if not inline and anchor else []
        recipe = None
        if companion:
            oid = None
            try:
                oid = self.present('mixes', [dict(id='companion', tracks=companion, intent='album_companion',
                    album_browse_id=browse)], self.now(), context=prepared.get('choice_context', {}),
                    window=[browse, opportunity, int(self.now() // 86400)])
            except Exception:
                self.logger.warning('Companion mix measurement failed', exc_info=True)
            recipe = dict(lane='album_companion', anchor_album=browse, opportunity_id=oid, option_id='companion')
        return dict(album=album, browse_id=browse, tracks=tracks, saved=saved, table_tracks=table,
            saved_owners=self.collection.saved_album_owners(browse).get(browse, []), editorial=editorial,
            notes=self.read_notes(editorial), opportunity_id=opportunity,
            album_feedback=self.feedback_state([browse]).get(browse, {}).get('kind'),
            companion=companion, companion_recipe=recipe, is_upload=upload, in_library=owned,
            library_track_count=len({t['track_id'] for t in library if t.get('track_id') is not None}),
            cover=cover, mosaic=mosaic,
            genres=sorted(set(self.genres.get_genre_whitelist()) | set(self.genres.all_genres()), key=str.lower),
            active_job=self.jobs.find_active_album(browse), conflict_count=self.reports.conflict_count_for_album(browse))

    def plays(self, videos):
        known = {t['video_id']: t['plays'] for t in self.charts.video_tracks_detail(videos)}
        return {video: known.get(video, 0) for video in videos}

    def set_feedback(self, browse, kind, opportunity):
        self.feedback(browse, kind, self.now(), opportunity)
        self.refresh()

    def admit_play(self):
        return self.player().begin_playback_intent()

    def play(self, browse, opportunity, action_id, intent):
        player = self.player()
        if not player.connected:
            raise ValueError('Connect the TuneConsole extension to play this album.')
        album = self.acquire(browse)
        playlist, tracks = album.get('audioPlaylistId'), album.get('tracks') or []
        video = next((t.get('videoId') for t in tracks if t.get('videoId')), None)
        if not playlist or not video:
            raise ValueError('The complete album queue is unavailable. Open its details and try again.')
        if not self.paused():
            self.remember(browse, album)
            if opportunity:
                self.respond(opportunity, 'choice', self.now(), option_id=browse, action_id=action_id,
                    details=dict(playlist=playlist, video_id=video, tracks=[dict(video_id=t['videoId'],
                        key=self.tracks.identity_key_for_video(t['videoId'])) for t in tracks if t.get('videoId')]))
                self.ledger.remember_album_playback_context(dict(opportunity_id=opportunity, browse=browse,
                    playlist=playlist, at=self.now(), action_id=action_id, videos=[t['videoId'] for t in tracks if t.get('videoId')]))
        player.send_control(dict(type='navigate', url='https://music.youtube.com/watch?v=' +
            quote(video, safe='') + '&list=' + quote(playlist, safe='')), intent=intent)
        thumbnail = best_thumb(album.get('thumbnails'))
        return dict(browse=browse, playlist=playlist, title=album.get('title') or '',
            artist=', '.join(a.get('name', '') for a in album.get('artists') or []),
            thumbnail=detail_thumb(thumbnail) if not is_placeholder_art(thumbnail) else None)

    def share(self, browse):
        cached = self.collection.cached_album_share(browse)
        if cached and cached['video_ids']:
            return cached['title'], cached['video_ids']
        album = self.acquire(browse)
        tracks = shareable_album_tracks(album)
        if not self.paused():
            self.remember(browse, album)
        return album.get('title'), [t['videoId'] for t in tracks]
