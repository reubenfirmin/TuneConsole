"""Resolve recording matches, rating transitions and catalog operations."""
import json
from uuid import uuid4

from yt_playlist.core import track_policy
from yt_playlist.core.catalog_identity import unresolved_ref
from yt_playlist.core.metadata_contract import clock
from yt_playlist.util.matching import identity_key, normalize


class Tracks:
    def __init__(self, repository, metadata_choices, artists, albums, keys, associate_item):
        self.repository, self.metadata_choices = repository, metadata_choices
        self.artists = artists
        self.albums, self.keys = albums, keys
        self.associate_item = associate_item

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

    def associate_youtube_item(self, video_id, recording_id, *, source, evidence, now, same_recording=False):
        return self.associate_item(video_id, recording_id, source=source, evidence=evidence, now=now, same_recording=same_recording)

    def resolve_identity_key(self, key, video_id=None):
        return self.repository.resolve_identity_key(key, video_id)

    def catalog_subject(self, track_id):
        return self.repository.catalog_subject(track_id)

    def observe_track(self, video_id, title, artist, **options):
        return self.repository.entry_key(self.observe_track_entry(video_id, title, artist, **options))

    def observe_track_entry(self, video_id, title, artist, *, now=None, **options):
        return self.upsert_track(video_id, title, artist, created_at=now, library=False, **options)

    def retained_rating_statuses(self, account_id, video_ids, **options):
        return self.repository.retained_rating_statuses(account_id, video_ids, **options)

    def item_rating(self, account_id, video_id):
        return self.repository.item_rating(account_id, video_id)

    def upsert_track(self, video_id, title, artist, album=None, duration_s=None, available=None,
                     video_type=None, artist_browse_id=None, album_browse_id=None, thumbnail=None,
                     created_at=None, *, library=True, source_ref=None, preferred_key=None, credits=None):
        if available is not None and (type(available) not in (bool, int) or available not in (0, 1)):
            raise ValueError('Availability must be a boolean or integer 0/1')
        video_id = video_id if video_id and video_id.strip() else None
        source_ref = None if video_id else source_ref or unresolved_ref(album_browse_id or album, title, artist)
        now = clock(created_at)
        alias = identity_key(title, artist)
        preferred = preferred_key or alias
        with self.repository.catalog_transaction():
            current = self.repository.entry_for_source(video_id, source_ref)
            if current is None:
                recording = self.repository.create_recording('@new:' + uuid4().hex, title, artist, now)
                key = f'@recording:{recording}|' + normalize(artist or '') if self.repository.alias_exists(preferred) else preferred
                entry = self.repository.create_entry(recording, key, video_id, source_ref, title, artist)
            else:
                entry, recording = current['id'], current['recording_id']
        source = 'youtube' if video_id else 'source_entry'
        if preferred != alias and (current is None or self.repository.alias_binding_exists(preferred, recording)):
            self.keys.add_alias(preferred, recording, source)
        self.keys.add_alias(alias, recording, source)
        self.repository.reconcile_admission(recording, video_id, now)
        values = dict(album=album, duration_s=duration_s, available=None if available is None else int(available),
            video_type=video_type, artist_browse_id=artist_browse_id, album_browse_id=album_browse_id)
        originals = dict(title=title, artist=artist, orig_title=title, orig_artist=artist)
        inputs = self.repository.catalog_metadata_inputs(entry, [*values, *originals])
        delivery, evidence = None, {}
        if inputs['evidence']:
            fields = {**values, **originals, 'thumbnail': thumbnail.strip() if isinstance(thumbnail, str) else thumbnail}
            delivery, evidence = self.metadata_choices.capture_fields(entry, 'youtube', fields,
                purpose='catalog-input', body={'format': 'youtube-catalog-input-1', 'fields': fields, 'credits': credits}, now=now)
        for field, value in values.items():
            if value is not None and inputs['sources'][field] in (None, 'youtube', 'legacy_unknown'):
                self.metadata_choices.select(entry, field, value, 'youtube', now=now, observation_id=evidence.get(field))
        for field, value in originals.items():
            if inputs['sources'][field] is None:
                self.metadata_choices.select(entry, field, value, 'youtube', now=now, observation_id=evidence.get(field))
        self.artists.observe(entry, artist, credits, browse_id=artist_browse_id, now=now, metadata_delivery_id=delivery)
        self.set_track_art(entry, thumbnail, source='youtube', now=now, observation_id=evidence.get('thumbnail'))
        if library:
            self.repository.add_library_membership(entry, now)
        if album_browse_id or album:
            self.albums.link(entry, album_browse_id, title=album, artist=artist)
        self.keys.resolve_listening(recording)
        return entry

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

    def set_track_art(self, track_id, url, *, source='provider', now=None, observation_id=None):
        current = self.repository.artwork_inputs(track_id)
        if current is not None:
            selected = track_policy.artwork_update(current['thumbnail'], url,
                                                   source=source, chosen=current['chosen'])
            if selected is not None:
                options = {} if now is None and observation_id is None else dict(now=now, observation_id=observation_id)
                self.metadata_choices.select(current['id'], 'thumbnail', selected, source, **options)

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
        edits = list(track_policy.metadata_edits(current, values))
        self.metadata_choices.edit(current['track_id'], edits)
        artist = values.get('artist')
        if artist and artist != current['orig_artist']:
            self.artists.record_credit_choice(current['track_id'], artist)

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
