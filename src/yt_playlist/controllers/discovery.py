"""Coordinate discovery selection, scheduling, provenance and pool maintenance."""
import json
from uuid import uuid4

from yt_playlist.core import discovery_policy as policy
from yt_playlist.core import discovery_cache_policy
from yt_playlist.core.activity import WorkBudgetExceeded, work_budget
from yt_playlist.core.db_diagnostics import database_operation


class Discovery:
    def __init__(self, repository, artists, eligibility, files, *, proposals=None, settings=None):
        self.repository, self.artists = repository, artists
        self.eligibility, self.files = eligibility, files
        self.proposals, self.settings = proposals, settings

    def select_starter_artist(self, name, browse_id, thumbnail, selected, now):
        identity = self.artists.ensure_artist(name, provider='youtube', provider_id=browse_id)
        if selected and self.repository.starter_count(identity) >= policy.STARTER_LIMIT:
            raise ValueError('Choose up to 12 artists to start.')
        self.repository.set_starter_artist(identity, browse_id, thumbnail, selected, now,
            score=policy.STARTER_SCORE, maximum=policy.STARTER_LIMIT)
        return self.starter_artists()

    def starter_artists(self):
        return self.repository.starter_artists()

    def starter_tracks(self):
        rows = self.repository.starter_track_rows(self.eligibility.maximum_duration())
        return policy.starter_tracks(rows)

    def interested_artists(self, limit=None):
        return self.repository.interested_artists(weights=policy.INTEREST_WEIGHTS, limit=limit)

    def artists_due_for_scan(self, now, ttl_days=policy.SCAN_DAYS, budget=policy.SCAN_BUDGET,
                             artist_limit=None, *, with_ids=False):
        artists = self.interested_artists(limit=artist_limit)
        scanned = self.repository.artist_scan_times([artist['artist_id'] for artist in artists])
        due = policy.scans_due(artists, scanned, now, ttl_days, budget)
        return due if with_ids else [artist['artist'] for artist in due]

    def mark_scanned(self, artist, now):
        self.repository.mark_scanned(self.artists.state_id(artist), now)

    def artist_images_due(self, artist_ids, now):
        return self.repository.artist_images_due(list(dict.fromkeys(artist_ids))[:policy.ARTWORK_COHORT], now)

    def record_artist_image(self, artist_id, thumbnail, now, *, retry_after=policy.ARTWORK_RETRY_SECONDS):
        self.repository.record_artist_image(artist_id, thumbnail, retry_at=now + retry_after)

    def get_discovered_artists(self):
        with self.files.retained():
            rows = self.repository.discovered_artist_rows()
            result = []
            for row in rows:
                if 'recovery_filename' in row:
                    explanation = (self.files.read(
                        tuple(row['recovery_' + field] for field in ('filename', 'digest', 'bytes')),
                        kind='discovery-candidate', owner=policy.explanation_owner(row))
                        if row['recovery_filename'] is not None else {'because': [], 'fits': []})
                else:
                    explanation = {field: json.loads(row[field] or '[]') for field in ('because', 'fits')}
                result.append({'artist': row['display_artist'], 'artist_id': row['artist_id'],
                    **{field: row[field] for field in ('score', 'thumbnail', 'found_at', 'last_shown', 'genre',
                                                     'first_shown', 'offered_count')}, **explanation})
            return result

    def discovery_track_view(self, now, gc_days):
        return [{**row, 'gc_at': policy.expiry(row['first_shown'] if row['first_shown'] is not None
                                              else row['found_at'], gc_days)}
                for row in self.repository.discovery_track_rows()]

    def discovery_album_view(self, now, gc_days):
        return [{**row, 'gc_at': policy.expiry(row['first_shown'], gc_days)}
                for row in self.repository.discovery_album_rows()]

    def discovery_artist_view(self, now, gc_days):
        return [{**row, 'gc_at': policy.expiry(row['first_shown'], gc_days)}
                for row in self.repository.discovery_artist_rows()]

    def mark_offered(self, kind, ids, now):
        return self.record_discovery_impressions(kind, ids, 'offer:' + uuid4().hex, now)

    def mark_shown(self, kind, ids, now):
        return self.mark_offered(kind, ids, now)

    def record_discovery_impressions(self, kind, ids, view_id, now):
        return self.repository.record_discovery_impressions(kind, ids, view_id, now)

    def remove_spotify_origins(self):
        return self.repository.remove_spotify_origins()

    def merge_genre_origins(self, previous, target):
        return self.repository.merge_genre_origins(previous, target)

    def fetched_albums(self, browse_ids):
        return self.repository.fetched_albums(browse_ids)

    def remember_album_fetch(self, browse):
        return self.repository.remember_album_fetch(browse)

    def discovered_albums_for_artist(self, artist):
        return self.repository.discovered_albums_for_artist(artist)

    def delete_discovered_albums(self, browse_ids):
        return self.repository.delete_discovered_albums(browse_ids)

    def prune_orphan_discovered_tracks(self, held_keys=()):
        gone = self.repository.orphan_track_keys() - set(held_keys)
        self.repository.delete_pool_entries((), (), gone)
        return len(gone)

    def upsert_discovered_album(self, browse_id, artist, title, year, thumbnail, now, genre=None, **options):
        return self.repository.upsert_discovered_album(browse_id, artist, title, year, thumbnail, now, genre, **options)

    def upsert_discovered_artist(self, artist, score, because, fits, thumbnail, now, genre=None, *, artist_id=None,
                                 source='discovery'):
        explanation = {'because': because or [], 'fits': fits or []}
        encoded = tuple(json.dumps(explanation[field], allow_nan=False) for field in ('because', 'fits'))
        identity = self.artists.state_id(artist_id if artist_id is not None else artist)
        with self.files.retained():
            reference = (self.files.publish('discovery-candidate', identity, explanation)
                         if self.repository.explanation_files() else None)
            self.repository.store_discovered_artist(identity, score, thumbnail, now, genre, source=source,
                                                    reference=reference, encoded=encoded)

    def retain_discovered_artist(self, artist, score):
        identity = self.artists.resolve_exact(artist)
        if identity is not None:
            self.repository.retain_discovered_artist(identity, score)

    def seed_spotify_artists(self, account_id, counts, now, *, min_plays=policy.SPOTIFY_MIN_PLAYS, import_key=None):
        rows = [(self.artists.state_id(name), float(plays)) for name, plays in counts.items() if plays >= min_plays]
        return self.repository.seed_spotify_artists(account_id, rows, now, import_key=import_key)

    def get_discovered_albums(self):
        return self.repository.get_discovered_albums()

    def present_home_tiles(self, album_ids, artist_ids):
        return self.repository.present_home_tiles(album_ids, artist_ids)

    def upsert_discovered_track(self, identity_key, video_id, title, artist, album, thumbnail,
                                genre, year, source_browse_id, now, audio=None, **options):
        return self.repository.upsert_discovered_track(identity_key, video_id, title, artist, album, thumbnail,
            genre, year, source_browse_id, now, audio, **options)

    def discovery_sources_for_track(self, key):
        return self.repository.discovery_sources_for_track(key)

    def discovered_source_tokens(self, source_type=None):
        return self.repository.discovered_source_tokens(source_type)

    def get_discovered_tracks(self):
        return self.repository.get_discovered_tracks()

    def discovered_track_keys(self):
        return self.repository.discovered_track_keys()

    def identity_keys_for_video(self, video_id):
        return self.repository.identity_keys_for_video(video_id)

    def discovered_tracks_by_keys(self, keys, **options):
        return self.repository.discovered_tracks_by_keys(keys, **options)

    def candidate_tracks_by_keys(self, keys):
        return self.repository.candidate_tracks_by_keys(keys)

    def prune_discovered_tracks(self, library_keys, held_keys=()):
        gone = self.repository.discovered_track_keys() & set(library_keys) - set(held_keys or ())
        self.repository.delete_pool_entries((), (), gone)

    def gc_discovery_pool(self, now, gc_days, held_track_keys):
        with (database_operation('Discovery.gc_discovery_pool'), work_budget(20),
              self.repository.cleanup_snapshot() as snapshot):
            reader = snapshot.store
            repository = reader.discovery.repository
            if len(repository.cleanup_head_rows(limit=129)) > 128:
                raise WorkBudgetExceeded('Recommendation head inventory exceeds its read budget')
            expired = repository.expired_pool_rows(now - gc_days * policy.DAY)
            held = set(held_track_keys or ()) | reader.rec_eligibility.generated_track_keys()
            gone = set(expired['tracks']) - held
            details = repository.cleanup_track_details(gone)
            cached = reader.rec_proposals.cache_rows() if gone else []
            snapshot.release_reader()
            prepared = discovery_cache_policy.prepare(cached, gone) if gone else []
            prepared = self.proposals.stage_pruned_cache(prepared)

            def publish():
                # Each owner commits independently. Keep memberships until caches,
                # refill prevention and enrichment bookkeeping have succeeded.
                if gone:
                    self.proposals.publish_pruned_cache(prepared)
                    self.repository.remember_source_tokens(details['sources'])
                    self.settings.replace_keys(['enrich_attempts:' + key for key in details['unowned']], {})
                self.repository.delete_pool_entries(expired['albums'], expired['artist_targets'], gone)

            if not snapshot.publish_ordered(publish):
                raise WorkBudgetExceeded('Discovery pool cleanup inputs changed; retry cleanup')
            return {kind: len(expired[kind]) for kind in ('albums', 'artists')} | {'tracks': len(gone)}

    def prune_discovered(self, owned_albums, saved_browse_ids, owned_artists):
        rows = self.repository.prunable_album_rows()
        albums = [row['browse_id'] for row in rows
                  if row['browse_id'] in saved_browse_ids or row['album_id'] in owned_albums]
        artists = []
        if owned_artists:
            candidates = self.repository.prunable_artist_ids()
            public = self.artists.public_keys(candidates)
            artists = [identity for identity in candidates
                       if f'@artist:{identity}' in owned_artists or public[identity] in owned_artists]
        self.repository.delete_pool_entries(albums, artists, ())
