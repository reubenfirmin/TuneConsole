"""Resolve recording matches, rating transitions and catalog operations."""
import json

from yt_playlist.core import track_policy
from yt_playlist.util.matching import identity_key


class Tracks:
    def __init__(self, repository, metadata_choices, artists):
        self.repository, self.metadata_choices = repository, metadata_choices
        self.artists = artists

    def remember_playback_alternate(self, account_id, requested, accepted, *, evidence, now):
        original, actual = requested['video_id'], accepted['video_id']
        if original != actual:
            self.repository.store_playback_alternate(account_id, original, actual,
                same_recording=track_policy.equivalent_upload(requested, accepted),
                evidence_json=json.dumps(evidence, sort_keys=True), now=now)

    def match_recording(self, title, artist):
        return track_policy.match_recording(self.repository.recording_match_rows(identity_key(title, artist)), title, artist)

    def known_duration(self, title, artist):
        return track_policy.known_duration(self.repository.duration_match_rows(identity_key(title, artist)), title, artist)

    def record_item_rating(self, account_id, video_id, status, *, source, now):
        self.record_item_ratings(account_id, {video_id: status}, source=source, now=now)

    def record_item_ratings(self, account_id, statuses, *, source, now):
        with self.repository.rating_transaction():
            previous, unbanned = self.repository.item_rating_inputs(account_id, statuses)
            observations, actions = track_policy.item_rating_changes(
                account_id, statuses, previous, unbanned, source=source, now=now)
            self.repository.write_item_ratings(observations, actions)

    def recording_ratings(self, keys):
        return track_policy.combined_ratings(self.repository.rating_rows(keys))

    def pending_action_ratings(self, account_id):
        rows = self.repository.action_rating_rows(account_id)
        keys = {row['identity_key'] for row in rows if row['identity_key']}
        return track_policy.pending_rating_projections(
            rows, self.recording_ratings(keys), self.repository.rating_feedback_rows(keys))

    def playback_alternate(self, account_id, video_id):
        return self.repository.playback_alternate(account_id, video_id)

    def playback_alternates(self, account_id, video_ids):
        return self.repository.playback_alternates(account_id, video_ids)

    def track_detail(self, video_id):
        return self.repository.track_detail(video_id)

    def track_play_ledger(self, recording_id, **options):
        return self.repository.track_play_ledger(recording_id, **options)

    def associate_youtube_item(self, video_id, recording_id, **options):
        return self.repository.associate_youtube_item(video_id, recording_id, **options)

    def resolve_identity_key(self, key, video_id=None):
        return self.repository.resolve_identity_key(key, video_id)

    def catalog_subject(self, track_id):
        return self.repository.catalog_subject(track_id)

    def observe_track(self, video_id, title, artist, **options):
        return self.repository.observe_track(video_id, title, artist, **options)

    def observe_track_entry(self, video_id, title, artist, **options):
        return self.repository.observe_track_entry(video_id, title, artist, **options)

    def retained_rating_statuses(self, account_id, video_ids, **options):
        return self.repository.retained_rating_statuses(account_id, video_ids, **options)

    def item_rating(self, account_id, video_id):
        return self.repository.item_rating(account_id, video_id)

    def upsert_track(self, video_id, title, artist, album=None, duration_s=None, available=None,
                     video_type=None, artist_browse_id=None, album_browse_id=None, thumbnail=None,
                     created_at=None, **options):
        return self.repository.upsert_track(video_id, title, artist, album, duration_s, available,
            video_type, artist_browse_id, album_browse_id, thumbnail, created_at, **options)

    def identity_key_for_video(self, video_id):
        return self.repository.identity_key_for_video(video_id)

    def library_identity_key_for_video(self, video_id):
        return self.repository.library_identity_key_for_video(video_id)

    def identity_keys_for_videos(self, video_ids):
        return self.repository.identity_keys_for_videos(video_ids)

    def thumbnail_for_video(self, video_id):
        return self.repository.thumbnail_for_video(video_id)

    def duration_for_video(self, video_id):
        return self.repository.duration_for_video(video_id)

    def tracks_for_waterfall(self, playlist_id):
        return self.repository.tracks_for_waterfall(playlist_id)

    def track_for_waterfall(self, track_id):
        return self.repository.track_for_waterfall(track_id)

    def album_tracks_for_waterfall(self, album_browse_id):
        return self.repository.album_tracks_for_waterfall(album_browse_id)

    def set_track_art(self, track_id, url, *, source='provider'):
        current = self.repository.artwork_inputs(track_id)
        if current is not None:
            selected = track_policy.artwork_update(current['thumbnail'], url,
                                                   source=source, chosen=current['chosen'])
            if selected is not None:
                self.metadata_choices.select(current['id'], 'thumbnail', selected, source)

    def artwork_source(self, track_id):
        return self.repository.artwork_source(track_id)

    def set_track_genre(self, track_id, genre):
        self._choose(track_id, 'genre', genre or '')

    def metadata_for_video(self, video_id):
        return self.repository.metadata_for_video(video_id)

    def update_metadata(self, video_id, values, *, observation):
        current = self.repository.metadata_for_video(video_id)
        if current is None:
            self.upsert_track(video_id, **observation, library=False)
            current = self.repository.metadata_for_video(video_id)
        for field, operation, value in track_policy.metadata_edits(current, values):
            if operation == 'restore':
                self.metadata_choices.restore(current['track_id'], field, value)
            else:
                self._choose(current['track_id'], field, value)

    def set_track_year(self, track_id, year):
        self._choose(track_id, 'mb_year', str(year) if year is not None else '')

    def set_track_title(self, track_id, title):
        title = (title or '').strip()
        if title:
            self._choose(track_id, 'title', title)

    def set_track_artist(self, track_id, artist):
        artist = (artist or '').strip()
        if artist:
            self._choose(track_id, 'artist', artist)

    def _choose(self, track_id, field, value):
        self.metadata_choices.choose(track_id, field, value)
        if field == 'artist' and value:
            self.artists.record_credit_choice(track_id, value)

    def reset_track_title(self, track_id):
        self._reset(track_id, 'title')

    def reset_track_artist(self, track_id):
        self._reset(track_id, 'artist')

    def _reset(self, track_id, field):
        current = self.repository.original_metadata(track_id)
        if current is not None and current['orig_' + field] is not None:
            self.metadata_choices.restore(current['id'], field, current['orig_' + field])
        else:
            self.metadata_choices.choose(track_id, field, withdraw=True)

    def get_track_enrichment(self, track_id):
        return self.repository.get_track_enrichment(track_id)

    def get_track_audio(self, track_id):
        return self.repository.get_track_audio(track_id)

    def track_ids_for_videos(self, video_ids):
        return self.repository.track_ids_for_videos(video_ids)

    def materialized_album_ids(self):
        return self.repository.materialized_album_ids()
