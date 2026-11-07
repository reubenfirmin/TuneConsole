"""Playlist policy and ordered catalog ingestion above SQL membership storage."""
from yt_playlist.library.playlist_metadata import (
    GENERATED_DESCRIPTION, PROMOTED_DESCRIPTION, content_hash, is_generated_description)
from yt_playlist.repos.models import PlaylistMemberships
from yt_playlist.repos.base import GENERATED_GROUP


def media_kind(video_types, *, populated):
    kinds = {'audio' if value == 'MUSIC_VIDEO_TYPE_ATV' else 'video'
             for value in video_types if value is not None}
    if len(kinds) > 1:
        return 'mixed'
    return next(iter(kinds)) if kinds else 'mix' if populated else ''


class Playlists:
    def __init__(self, repository, tracks, radio, guard):
        self.repository, self.tracks, self.radio, self.guard = repository, tracks, radio, guard

    def radio_playlist_ids(self):
        return self.radio.playlist_ids()

    def mark_generated_playlist(self, ytm, description=None):
        self.repository.set_playlist_provenance(ytm, origin='tuneconsole', adopted=False,
                                                description=description or GENERATED_DESCRIPTION)

    def adopt_generated_playlist(self, ytm):
        self.repository.adopt_generated_playlist(ytm, PROMOTED_DESCRIPTION)

    def reconcile_playlist_provenance(self, ytm, description, *, account_id=None):
        with self.guard():
            scope = self.repository.playlist_reference(ytm, account_id)
            current = self.repository.playlist_provenance(scope)
            promoted = PROMOTED_DESCRIPTION.casefold() in (description or '').casefold()
            if promoted or is_generated_description(description) or current['origin'] == 'tuneconsole':
                # A stale remote marker cannot undo explicit local adoption.
                adopted = promoted or (current['origin'] == 'tuneconsole' and current['adopted'])
                self.repository.set_playlist_provenance(scope, origin='tuneconsole', adopted=adopted,
                    description=description if description is not None else current['description'])

    def playlist_kind(self, playlist_id):
        values = self.repository.playlist_video_types(playlist_id)
        return media_kind(values, populated=bool(values))

    def cleanup_memberships(self):
        facts = self.repository.cleanup_membership_facts(GENERATED_GROUP)
        keys = {identity: frozenset(values) for identity, values in facts['keys'].items()}
        kinds = {playlist.playlist_scope_id: media_kind(facts['types'][playlist.id], populated=bool(keys[playlist.id]))
                 for playlist in facts['playlists']}
        return PlaylistMemberships(facts['playlists'], keys, kinds, facts['excluded'])

    def publish_playlist_snapshot(self, account_id, provider_playlist_id, title, track_ids, now, *,
                                  expected_revision, thumbnail=None, created_at=None):
        with self.guard():
            if self.repository.playlist_revision(account_id, provider_playlist_id) != expected_revision:
                return None
            facts = self.repository.snapshot_track_facts(track_ids)
            entries = {row['id']: row['identity_key'] for row in facts}
            return self.repository.publish_playlist_snapshot(account_id, provider_playlist_id, title, list(entries), now,
                expected_revision=expected_revision, digest=content_hash(entries.values()),
                thumbnail=thumbnail, created_at=created_at)

    def publish_confirmed_tracks(self, playlist_id, title, tracks, now, *, expected_prefix=None, destination=None):
        tracks = list(tracks)
        videos = [track['video_id'] for track in tracks]
        if any(not video for video in videos) or len(set(videos)) != len(videos):
            raise ValueError('Confirmed playlist must contain distinct playable uploads')
        if playlist_id is None and destination is None:
            raise ValueError('A destination account and playlist are required')
        prefix = list(expected_prefix) if expected_prefix is not None else None
        with self.guard():
            facts = self.repository.confirmed_facts(playlist_id, destination)
            if facts is None:
                raise ValueError('Unknown playlist')
            saved = facts['videos']
            incremental = prefix is not None and saved == prefix and videos[:len(saved)] == prefix and not facts['unresolved']
            start = len(saved) if incremental else 0
            entries = []
            for track in tracks[start:]:
                entries.append(self.tracks.upsert_track(track['video_id'], track.get('title') or '',
                    track.get('artist') or '', track.get('album') or '', track.get('duration'),
                    thumbnail=track.get('thumbnail'), library=False))
            # New observations can disambiguate earlier aliases. Hash their final identities.
            keys = self.tracks.identity_keys_for_videos(videos)
            if set(keys) != set(videos):
                raise ValueError('Confirmed playlist contains an unknown upload')
            return self.repository.publish_confirmed(facts['id'], title, videos, content_hash(set(keys.values())), entries, now,
                expected_prefix=prefix, destination=destination, empty_digest=content_hash([]))

    def set_song_liked(self, account_id, video_id, on):
        with self.guard():
            playlist = self.repository.liked_membership_target(account_id, 'LM', video_id)
            if playlist is not None:
                self.repository.set_liked_membership(playlist, video_id, on)

    def playlist_reference(self, ytm, account_id=None):
        return self.repository.playlist_reference(ytm, account_id)

    def playlist_provenance(self, ytm, *, account_id=None):
        return self.repository.playlist_provenance(ytm, account_id=account_id)

    def set_playlist_provenance(self, ytm, *, origin, adopted, description=None, account_id=None):
        return self.repository.set_playlist_provenance(ytm, origin=origin, adopted=adopted,
                                                       description=description, account_id=account_id)

    def is_quarantined_playlist(self, ytm, *, account_id=None):
        return self.repository.is_quarantined_playlist(ytm, account_id=account_id)

    def upsert_playlist(self, account_id, provider_playlist_id, title, track_count, content_hash, now,
                        thumbnail=None, *, created_at=None):
        return self.repository.upsert_playlist(account_id, provider_playlist_id, title, track_count, content_hash, now,
                                               thumbnail, created_at=created_at)

    def set_playlist_tracks(self, playlist_id, track_ids):
        return self.repository.set_playlist_tracks(playlist_id, track_ids)

    def playlist_revision(self, account_id, provider_playlist_id):
        return self.repository.playlist_revision(account_id, provider_playlist_id)

    def playlist_revisions(self, account_id):
        return self.repository.playlist_revisions(account_id)

    def get_playlist_track_ids(self, playlist_id):
        return self.repository.get_playlist_track_ids(playlist_id)

    def playlist_ids_for_video(self, video_id):
        return self.repository.playlist_ids_for_video(video_id)

    def set_playlist_title(self, playlist_id, title, now):
        return self.repository.set_playlist_title(playlist_id, title, now)

    def set_playlist_track_count(self, playlist_id, count, now):
        return self.repository.set_playlist_track_count(playlist_id, count, now)

    def remove_playlist(self, playlist_id, *, require_empty=False, expected_revision=None):
        return self.repository.remove_playlist(playlist_id, require_empty=require_empty, expected_revision=expected_revision)

    def get_playlists(self):
        return self.repository.get_playlists()

    def get_playlist_thumbnails(self):
        return self.repository.get_playlist_thumbnails()

    def ensure_playlist_thumbnail(self, playlist_id):
        return self.repository.ensure_playlist_thumbnail(playlist_id)

    def home_preview(self, playlist_id):
        return self.repository.home_preview(playlist_id)

    def get_playlist(self, playlist_id):
        return self.repository.get_playlist(playlist_id)

    def get_playlist_for_account(self, account_id, provider_playlist_id):
        return self.repository.get_playlist_for_account(account_id, provider_playlist_id)

    def get_playlist_track_keys(self, playlist_id):
        return self.repository.get_playlist_track_keys(playlist_id)

    def get_playlist_tracks_with_meta(self, playlist_id):
        return self.repository.get_playlist_tracks_with_meta(playlist_id)

    def set_playlist_group(self, ytm, name, *, account_id=None):
        return self.repository.set_playlist_group(ytm, (name or '').strip(), account_id=account_id)

    def hide_playlist(self, ytm, *, account_id=None):
        return self.repository.hide_playlist(ytm, account_id=account_id)

    def unhide_playlist(self, ytm, *, account_id=None):
        return self.repository.unhide_playlist(ytm, account_id=account_id)

    def get_hidden_playlists(self):
        return self.repository.get_hidden_playlists()

    def get_playlist_groups(self):
        return self.repository.get_playlist_groups()
