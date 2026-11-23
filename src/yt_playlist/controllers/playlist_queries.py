"""Coherent saved-playlist queries and detached table projections."""
import hashlib
import json
import re
from datetime import datetime

from yt_playlist.library.analysis import SYSTEM_PLAYLIST_IDS
from yt_playlist.integrations.youtube_music_access import YouTubeMusicAccess
from yt_playlist.repos.base import GENERATED_GROUP
from yt_playlist.util.navigation import artist_credit_links


def feedback_version(tracks):
    fields = sorted((t['identity_key'], t.get('genre') or '', t.get('year') or '') for t in tracks)
    return hashlib.sha256(json.dumps(fields).encode()).hexdigest()[:20]


def playlist_rows(playlists, *, labels, groups, recipe_dates, stats, thumbnails, hidden,
                  quarantined, kinds, recipes, now):
    rows = []
    for p in playlists:
        scope = p.playlist_scope_id
        if scope in hidden:
            continue
        last, listens = stats.get(p.id, (None, 0))
        generated = scope in quarantined
        created = p.created_at
        if generated and created is None:
            created = recipe_dates.get(scope)
            if created is None:
                dated = re.search(r' - ([A-Za-z]+ \d{1,2} \d{4})(?: #\d+)?$', p.title or '')
                if dated:
                    try:
                        created = datetime.strptime(dated[1], '%B %d %Y').timestamp()
                    except ValueError:
                        pass
        rows.append(dict(id=p.id, ytm=p.provider_playlist_id, title=p.title,
            identity=labels.get(p.account_id, '?'), thumbnail=thumbnails.get(p.id), count=p.track_count,
            kind=kinds[p.id], is_video=(recipes.get(scope) or {}).get('media') == 'video',
            group=groups.get(scope, ''), is_generated=generated, last=last, listens=listens,
            created=p.created_at, generated_created=created,
            is_new=not generated and p.provider_playlist_id not in SYSTEM_PLAYLIST_IDS and
                p.created_at is not None and now - 7 * 86400 <= p.created_at <= now))
    return dict(rows=rows, group_names=sorted({g for g in groups.values() if g}, key=str.lower),
                has_groups=any(r['group'] and r['group'] != GENERATED_GROUP for r in rows))


class PlaylistQueries:
    def __init__(self, read_view, access, jobs, *, now, measure, facets, mood, moods,
                 pending, initial_setup, gc_days):
        self.read_view, self.access, self.jobs, self.now = read_view, access, jobs, now
        self.measure, self.facets, self.mood, self.moods = measure, facets, mood, moods
        self.pending, self.initial_setup, self.gc_days = pending, initial_setup, gc_days

    def listing(self, result_id=''):
        with self.read_view() as reader:
            playlists = reader.playlists.get_playlists()
            result = playlist_rows(playlists,
                labels={a.id: a.label for a in reader.accounts.get_accounts()},
                groups=reader.playlists.get_playlist_groups(),
                recipe_dates=reader.playlist_recipes.get_recipe_created_ats(),
                stats=reader.charts.get_playlist_playback_stats(), thumbnails=reader.playlists.get_playlist_thumbnails(),
                hidden=reader.playlists.get_hidden_playlists(),
                quarantined={p.playlist_scope_id for p in playlists
                             if reader.playlists.is_quarantined_playlist(p.playlist_scope_id)},
                kinds={p.id: reader.playlists.playlist_kind(p.id) for p in playlists},
                recipes={p.playlist_scope_id: reader.playlist_recipes.get_recipe(p.playlist_scope_id) for p in playlists},
                now=self.now())
            result['gc_days'] = self.gc_days(reader)
        job = self.jobs.get(int(result_id)) if str(result_id).isdigit() else None
        result['action_result'] = job.result if job and job.kind == 'playlist_action_result' else None
        return result

    def detail(self, pid):
        with self.read_view() as reader:
            pl = reader.playlists.get_playlist(pid)
            if pl is None:
                return None
            tracks = reader.charts.playlist_tracks_detail(pid)
            generated = reader.playlists.is_quarantined_playlist(pl.playlist_scope_id)
            recipe = reader.playlist_recipes.get_recipe(pl.playlist_scope_id)
            result = dict(pl=pl, tracks=tracks, is_generated=generated, build_recipe=recipe,
                recipe=recipe if generated else None,
                show_playlist_radio=not generated or not self.initial_setup(reader, self.now()),
                facets=self.facets(reader, pid) if generated else None,
                feedback_version=feedback_version(tracks) if generated else None,
                mood_state=self.mood(reader, pid, self.now()) if generated else 0,
                mood_states=self.moods(reader, self.now()) if generated else {},
                is_video=(recipe or {}).get('media') == 'video',
                has_cluster_canvas=bool(reader.playlist_recipes.get_cluster_canvas(pl.playlist_scope_id)) if generated else False,
                kind=reader.playlists.playlist_kind(pid), total_plays=sum(t['plays'] for t in tracks),
                genres=sorted(set(reader.genres.get_genre_whitelist()) | set(reader.genres.all_genres()), key=str.lower),
                conflict_count=reader.enrichment_reports.conflict_count_for_playlist(pid),
                position_pending=len(self.pending(reader, pid)), active_job=self.jobs.find_active(pid))
        self.measure('open', self.now(), playlist_id=pid)
        return result

    def cells(self, pid):
        with self.read_view() as reader:
            if reader.playlists.get_playlist(pid) is None:
                return None
            tracks = reader.charts.playlist_tracks_detail(pid)
        return dict(cells=[dict(video_id=t['video_id'], genre=t.get('genre') or '', year=t.get('year') or '') for t in tracks],
            pending=sum(not (t.get('genre') and t.get('year')) for t in tracks), feedback_version=feedback_version(tracks))

    def metadata(self, video):
        with self.read_view() as reader:
            track = reader.tracks.metadata_for_video(video)
            provenance = reader.enrichment_reports.genre_provenance(track['track_id']) if track else None
            if track:
                track = {**track, 'artist_links': artist_credit_links(
                    reader.artists.navigation(name=track.get('artist') or '', video=video))}
            return dict(metadata=track, genre_options=provenance['options'] if provenance else [],
                genres=sorted(set(reader.genres.get_genre_whitelist()) | set(reader.genres.all_genres()), key=str.casefold))

    def row(self, video, *, pid=None, album=None):
        with self.read_view() as reader:
            pl = reader.playlists.get_playlist(pid) if pid is not None else None
            generated = bool(pl and reader.playlists.is_quarantined_playlist(pl.playlist_scope_id))
            tracks = reader.charts.album_tracks_detail(album) if album is not None else reader.charts.playlist_tracks_detail(pid)
            for index, track in enumerate(tracks, 1):
                if track['video_id'] == video:
                    return dict(t=track, idx=index, pl=pl, is_generated=generated,
                        mood_states=self.moods(reader, self.now()) if generated else {})

    def feedback(self, pid):
        with self.read_view() as reader:
            pl = reader.playlists.get_playlist(pid)
            if not pl or not reader.playlists.is_quarantined_playlist(pl.playlist_scope_id):
                return None
            return dict(pid=pid, facets=self.facets(reader, pid),
                        feedback_version=feedback_version(reader.charts.playlist_tracks_detail(pid)))

    def share(self, pid):
        with self.read_view() as reader:
            pl = reader.playlists.get_playlist(pid)
            return (pl, reader.charts.playlist_tracks_detail(pid)) if pl else (None, [])

    def heading(self, pid):
        with self.read_view() as reader:
            pl = reader.playlists.get_playlist(pid)
            if pl is None:
                return None
            return dict(pl=pl, is_generated=reader.playlists.is_quarantined_playlist(pl.playlist_scope_id),
                        recipe=reader.playlist_recipes.get_recipe(pl.playlist_scope_id),
                        tracks=reader.charts.playlist_tracks_detail(pid))

    def snapshot(self, *, pid=None, account=None):
        if pid is not None:
            with self.read_view() as reader:
                playlist = reader.playlists.get_playlist(pid)
            if playlist is None:
                return None
            account, client = self.access.resolve(playlist_id=pid)
            remote = playlist.provider_playlist_id
        else:
            account, client = self.access.resolve(account_id=account)
            remote = 'LM'
        return dict(account_id=account, **({'playlist_id': pid} if pid is not None else {}),
                    reads=YouTubeMusicAccess.for_client(client).playlist_read_report(remote))
