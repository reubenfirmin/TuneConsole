"""Complete library actions over explicit repositories and existing mutation owners.

Mutation owners retain their durable claims, backups and uncertain-write receipts.
Only confirmed membership is allowed to drive downstream evidence and player effects.
"""
import json

from yt_playlist.core.action_errors import ActionInputError
from yt_playlist.library.analysis import SYSTEM_PLAYLIST_IDS
from yt_playlist.library.playlist_metadata import PROMOTED_DESCRIPTION
from yt_playlist.library.playlist_writes import PlaylistAddUncertain
from yt_playlist.repos.base import GENERATED_GROUP


def destinations(playlists, labels, connected, containing):
    eligible = [p for p in playlists if p.provider_playlist_id not in SYSTEM_PLAYLIST_IDS
                and p.account_id in connected]
    return dict(playlists=[dict(id=p.id, title=p.title, identity=labels.get(p.account_id, ''),
                                already_present=p.id in containing)
                            for p in sorted(eligible, key=lambda p: (p.title.casefold(), p.id))],
                multiple_identities=len(connected) > 1)


class LibraryActions:
    def __init__(self, playlists, accounts, charts, tracks, recipes, events, queue, *,
                 ops, clients, now, measure, respond, refresh_player, refresh_cleanup,
                 refresh_content, wake_enrichment, identity, logger):
        self.playlists, self.accounts, self.charts, self.tracks = playlists, accounts, charts, tracks
        self.recipes, self.events, self.queue = recipes, events, queue
        self.ops, self.clients, self.now = ops, clients, now
        self.measure, self.respond, self.refresh_player = measure, respond, refresh_player
        self.refresh_cleanup, self.refresh_content = refresh_cleanup, refresh_content
        self.wake_enrichment, self.identity, self.logger = wake_enrichment, identity, logger

    def destinations(self, video_id):
        return destinations(self.playlists.get_playlists(),
                            {a.id: a.label for a in self.accounts.get_accounts()}, self.clients() or {},
                            self.playlists.playlist_ids_for_video(video_id))

    def generated(self, pid):
        playlist = self.playlists.get_playlist(pid)
        return bool(playlist and self.playlists.is_quarantined_playlist(playlist.playlist_scope_id))

    def album_row(self, browse, video):
        return next((dict(t=track, idx=index) for index, track in
                     enumerate(self.charts.album_tracks_detail(browse), 1) if track['video_id'] == video), None)

    def metadata(self, video_id, changes, observation):
        if changes:
            self.tracks.update_metadata(video_id, changes, observation={**observation, 'created_at': self.now()})
            self.refresh_content(video_ids=[video_id])

    def edit_field(self, video_id, field, value=None, *, reset=False):
        if not video_id:
            raise ValueError('no track given')
        tid = self.tracks.track_ids_for_videos([video_id]).get(video_id)
        if tid is None:
            raise ValueError('track not found')
        if field not in ('title', 'artist', 'genre', 'year') or reset and field not in ('title', 'artist'):
            raise ValueError('bad field')
        method = getattr(self.tracks, ('reset_track_' if reset else 'set_track_') + field)
        method(tid) if reset else method(tid, value)
        if field in ('genre', 'year'):
            self.refresh_content(video_ids=[video_id])

    def _refresh(self, pid):
        playlist = self.playlists.get_playlist(pid)
        if playlist:
            self.refresh_player(playlist.provider_playlist_id)

    def create_song(self, name, track):
        try:
            result = self.ops.create_playlist_from_song(name, track)
        except PlaylistAddUncertain as failure:
            if track['videoId'] in failure.confirmed:
                self.measure('save', self.now(), video_id=track['videoId'],
                             event_id=f"song-create:{getattr(failure, 'local_playlist_id', failure.playlist_id)}")
            raise
        if result['added']:
            self.measure('save', self.now(), video_id=track['videoId'], event_id=f"song-create:{result['db_pid']}")
        return result

    def add_song(self, pid, track):
        try:
            result = self.ops.add_song_to_playlist(pid, track)
        except PlaylistAddUncertain as failure:
            if track['videoId'] in failure.confirmed:
                self.measure('save', self.now(), video_id=track['videoId'], generated=self.generated(pid))
                self._refresh(pid)
            raise
        if not result.get('already_present') and not result['added']:
            raise ActionInputError('song_add_rejected')
        if result['added'] and not result.get('already_present'):
            self.measure('save', self.now(), video_id=track['videoId'], generated=self.generated(pid))
        self._refresh(pid)
        return {**result, 'playlist': self.playlists.get_playlist(pid)}

    def add_tracks(self, pid, tracks, after_video_id=None, *, suggestion=False):
        before = {t['video_id'] for t in self.charts.playlist_tracks_detail(pid)}
        try:
            result = self.ops.add_tracks(pid, tracks, after_video_id)
        except PlaylistAddUncertain as failure:
            # The write owner has already retained the confirmed subset. Publish
            # effects for that subset once, then preserve its recovery receipt.
            self._added_effects(pid, tracks, before, after_video_id, suggestion,
                                confirmed=set(failure.confirmed))
            raise
        return self._added_effects(pid, tracks, before, after_video_id, suggestion, result=result)

    def _added_effects(self, pid, tracks, before, after_video_id, suggestion, *, confirmed=None, result=None):
        self._refresh(pid)
        details = self.charts.playlist_tracks_detail(pid)
        added = {t['video_id'] for t in details} - before
        if confirmed is not None:
            added &= confirmed
        for track in tracks:
            vid = track.get('videoId')
            if vid in added:
                self.measure('save', self.now(), video_id=vid, generated=self.generated(pid))
        if after_video_id:
            try:
                identity = self.identity()
                playlist = self.playlists.get_playlist(pid)
                if identity is not None and playlist:
                    for track in tracks:
                        if track.get('videoId') in added:
                            self.events.record_player_event(identity, 'alt_version', track['videoId'], None,
                                None, playlist.provider_playlist_id,
                                json.dumps({'old': after_video_id, 'new': track['videoId']}), self.now())
            except Exception:
                self.logger.debug('alternate choice evidence unavailable', exc_info=True)
        row = next((t for t in reversed(details) if t['video_id'] == tracks[0].get('videoId')), None) if suggestion else None
        if row and not row.get('genre'):
            self.queue.requeue_enrichment([row['track_id']])
            self.wake_enrichment()
        return dict(result=result, added=row, details=details)

    def mutate(self, command, pid, *args):
        result = getattr(self.ops, command)(pid, *args)
        self._refresh(pid)
        return result

    def copy_into(self, ids, target):
        try:
            result = self.ops.copy_into(ids, target)
        except PlaylistAddUncertain as failure:
            if failure.confirmed:
                self._refresh(target)
            raise
        self._refresh(target)
        return result

    def copy(self, ids, name):
        return self.ops.copy(ids, name)

    def titles(self, ids):
        return {pid: pl.title if (pl := self.playlists.get_playlist(pid)) else f'Playlist #{pid}' for pid in ids}

    def unconfirmed(self, failure):
        account = getattr(failure, 'account_id', None)
        if account is not None:
            return self.playlists.get_playlist_for_account(account, failure.playlist_id)
        # A legacy receipt without ownership cannot select between two saved
        # memberships of the same remote object.
        matches = [p for p in self.playlists.get_playlists() if p.provider_playlist_id == failure.playlist_id]
        return matches[0] if len(matches) == 1 else None

    def group(self, ids, name):
        for pid in ids:
            playlist = self.playlists.get_playlist(pid)
            if playlist:
                self.playlists.set_playlist_group(playlist.playlist_scope_id, name)

    def promote(self, pid):
        playlist = self.playlists.get_playlist(pid)
        if playlist is None:
            return
        generated = self.generated(pid)
        scope = playlist.playlist_scope_id
        self.playlists.adopt_generated_playlist(scope)
        if self.playlists.get_playlist_groups().get(scope) == GENERATED_GROUP:
            self.playlists.set_playlist_group(scope, '')
        if generated:
            event = f'promote:{playlist.provider_playlist_id}'
            self.measure('promotion', self.now(), playlist_id=pid, event_id=event)
            recipe = self.recipes.get_recipe(scope) or {}
            if recipe.get('opportunity_id') and recipe.get('option_id'):
                self.respond(recipe['opportunity_id'], 'promote', self.now(), option_id=recipe['option_id'],
                             action_id=event, details={'playlist_id': pid})
        client = (self.clients() or {}).get(playlist.account_id)
        if client:
            try:
                client.edit_playlist(playlist.provider_playlist_id, description=PROMOTED_DESCRIPTION)
            except Exception:
                self.logger.warning('promotion marker not confirmed: %s', playlist.provider_playlist_id, exc_info=True)

    def delete(self, ids):
        items = []
        for pid in dict.fromkeys(ids):
            playlist = self.playlists.get_playlist(pid)
            item = dict(id=pid, title=playlist.title if playlist else f'Playlist #{pid}')
            if playlist is None:
                items.append({**item, 'status': 'absent', 'detail': 'Already absent from the local library; no delete sent.'})
            elif playlist.provider_playlist_id in SYSTEM_PLAYLIST_IDS:
                self.playlists.hide_playlist(playlist.playlist_scope_id)
                items.append({**item, 'status': 'hidden', 'detail': 'Hidden from TuneConsole; system playlist kept on YouTube.'})
            else:
                try:
                    self.ops.delete(pid)
                except Exception:
                    self.logger.exception('playlist deletion not confirmed: %s', pid)
                    items.append({**item, 'status': 'failed', 'detail': 'Deletion not confirmed.'})
                else:
                    items.append({**item, 'status': 'deleted', 'detail': 'Deleted.'})
                    self.refresh_player(playlist.provider_playlist_id)
        try:
            self.refresh_cleanup()
        except Exception:
            self.logger.exception('cleanup refresh after deletion failed')
        failed = sum(i['status'] == 'failed' for i in items)
        status = 'partial' if failed and failed < len(items) else 'failed' if failed or not items else 'complete'
        return dict(status=status, operation='delete', items=items,
                    message=f'Playlist deletion: {len(items) - failed} completed or already absent; {failed} not confirmed.',
                    guidance=('Check YouTube before retrying failed items; a request may have completed remotely. '
                              'Completed items are listed separately and are no longer selected.' if failed else ''))
