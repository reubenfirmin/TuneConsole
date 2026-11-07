"""Coordinate collection workflows and compose album and artist views."""
from yt_playlist.core import collection_views as views, collection_policy as policy
from yt_playlist.core.metadata_contract import clock


class Collection:
    def __init__(self, repository, albums):
        self.repository, self.albums = repository, albums

    def taste_album_contexts(self, *, include_browse=()):
        return views.taste_album_contexts(self.repository.taste_album_inputs(), include_browse)

    def playback_source(self, playlist_ytm_id=None, video_id=None, album_browse_id=None):
        inputs = self.repository.playback_source_inputs(playlist_ytm_id, video_id, album_browse_id)
        return views.playback_source(inputs, playlist_ytm_id, album_browse_id)

    def album_alias_candidates(self, titles):
        titles = {policy.album_title(title) for title in titles}
        return [row for row in self.repository.album_title_rows() if policy.album_title(row['title']) in titles] if titles else []

    def claim_album_alias_check(self, first, second, now):
        first, second = sorted((first, second))
        return first != second and self.repository.claim_album_alias_check(first, second, now,
            expire_before=now - policy.ALIAS_RETENTION_SECONDS, retry_before=now - policy.ALIAS_RETRY_SECONDS)

    def album_listening_counts(self, browse_ids):
        return {browse: summary['plays'] for browse, summary in self.album_listening_summary(browse_ids).items()}

    def album_listening_summary(self, browse_ids):
        return self.repository.album_listening_summary(browse_ids)

    def collection_albums(self):
        return views.collection_albums(self.repository.collection_album_inputs())

    def collection_artists(self):
        return views.collection_artists(self.repository.collection_artist_inputs())

    def artist_albums(self, artist, browse_id=None):
        return views.artist_albums(self.repository.artist_album_inputs(artist, browse_id))

    def cache_album_tracklist(self, browse_id, title, artist, thumbnail, keys, *, video_ids=None,
                              entries=None, credits=None, year=None, album_type=None):
        keys, entries = list(keys), list(entries) if entries is not None else None
        now = clock(None)
        album_id = self.albums.observe(browse_id, title=title, artist=artist, thumbnail=thumbnail,
            credits=credits, year=year, type=album_type, rank=3)
        with self.repository.membership_transaction():
            inputs = self.repository.tracklist_inputs(album_id, keys if entries is None else [])
            rows = policy.tracklist_rows(keys, entries, inputs)
            self.repository.store_album_tracklist(album_id, browse_id, rows, occurrences=inputs['occurrences'],
                ordered=entries is not None, complete=bool(rows), expected_count=len(rows), now=now)
            if not inputs['occurrences'] or entries is None and video_ids:
                self.repository.store_album_share(album_id, browse_id, title, video_ids,
                    occurrences=inputs['occurrences'], now=now)
            if inputs['occurrences']:
                self._collect_tracklists(browse_id)

    def cache_partial_album(self, browse_id, title, artist, thumbnail, entries, *, expected_count=None):
        if not self.repository.uses_occurrences():
            return
        entries = list(entries)
        album_id = self.albums.observe(browse_id, title=title, artist=artist, thumbnail=thumbnail, rank=1)
        with self.repository.membership_transaction():
            rows = policy.tracklist_rows([], entries, {'occurrences': True})
            self.repository.store_album_tracklist(album_id, browse_id, rows, occurrences=True,
                ordered=True, complete=False, expected_count=expected_count, now=clock(None))
            self._collect_tracklists(browse_id)

    def _collect_tracklists(self, browse_id):
        self.repository.delete_tracklists(policy.retired_tracklists(self.repository.unreferenced_tracklists(browse_id)))

    def cached_album_share(self, browse_id):
        return views.album_share(self.repository.album_share_inputs(browse_id))

    def cached_album_tracklist_ids(self):
        return self.repository.cached_album_tracklist_ids()

    def complete_album_tracklist_ids(self):
        return self.repository.complete_album_tracklist_ids()

    def is_album_tracklist_complete(self, browse_id):
        return self.repository.is_album_tracklist_complete(browse_id)

    def has_published_album_tracklist(self, browse_id):
        return self.repository.has_published_album_tracklist(browse_id)

    def saved_album_revision(self, account_id):
        return self.repository.saved_album_revision(account_id)

    def replace_saved_albums(self, albums):
        ids = [self._observe_saved_album(album) for album in albums if album.get('browse')]
        with self.repository.membership_transaction():
            self.repository.advance_album_revision(None)
            self.repository.delete_album_memberships(None)
            self.repository.reset_legacy_cohort(replace=True)
            for identity in ids:
                self.repository.save_album_membership(identity, None)

    def replace_saved_album_snapshots(self, snapshots, *, expected_revisions=None, now=None):
        at = clock(now)
        accepted = policy.accepted_snapshots(snapshots, self.repository.snapshot_inputs(snapshots), expected_revisions)
        prepared = {identity: [self._observe_saved_album(album) for album in snapshots[identity]] for identity in accepted}
        with self.repository.membership_transaction():
            accepted = policy.accepted_snapshots(prepared, self.repository.snapshot_inputs(prepared), expected_revisions)
            for identity in accepted:
                self.repository.delete_album_memberships(identity)
                for album_id in prepared[identity]:
                    self.repository.save_album_membership(album_id, identity)
                self.repository.store_snapshot_observation(identity, at)
            if accepted and policy.can_retire_legacy(self.repository.legacy_cohort_inputs()):
                self.repository.delete_album_memberships(None)
            return set(accepted)

    def saved_album_ids(self, *, browse_ids=None):
        return self.repository.saved_album_ids(browse_ids=browse_ids)

    def album_id(self, browse_id):
        return self.repository.album_id(browse_id)

    def ensure_album_identity(self, browse_id):
        return self.repository.ensure_album_identity(browse_id)

    def canonical_album_ids(self, browse_ids):
        return self.repository.canonical_album_ids(browse_ids)

    def preferred_album_ids(self, browse_ids):
        return self.repository.preferred_album_ids(browse_ids)

    def album_alias_revision(self):
        return self.repository.album_alias_revision()

    def observe_album_representation(self, browse_id, album, *, now=None):
        return self.repository.observe_album_representation(browse_id, album, now=now)

    def add_saved_album(self, album, *, account_id=None):
        identity = self._observe_saved_album(album)
        with self.repository.membership_transaction():
            self.repository.advance_album_revision(account_id)
            if account_id is None:
                self.repository.reset_legacy_cohort(replace=not self.repository.legacy_cohort_inputs()['has_legacy'])
            self.repository.save_album_membership(identity, account_id)

    def _observe_saved_album(self, data):
        return self.albums.observe(data['browse'], title=data.get('title'), artist=data.get('artist'),
            year=data.get('year'), type=data.get('type'), thumbnail=data.get('thumbnail'), credits=data.get('credits'), rank=2)

    def remove_saved_album(self, browse_id, *, account_id=None):
        return self.repository.remove_saved_album(browse_id, account_id=account_id)

    def get_saved_albums(self, *, account_id=None):
        return self.repository.get_saved_albums(account_id=account_id)

    def saved_album_owners(self, browse_id=None):
        return self.repository.saved_album_owners(browse_id)

    def saved_albums_recency(self, *, include_generated=False, browse_ids=None):
        return self.repository.saved_albums_recency(include_generated=include_generated, browse_ids=browse_ids)

    def artist_browse_id(self, artist):
        return self.repository.artist_browse_id(artist)
