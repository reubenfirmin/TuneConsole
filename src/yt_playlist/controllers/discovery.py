"""Coordinate discovery selection, scheduling, provenance and pool maintenance."""
import json
from uuid import uuid4

from yt_playlist.core import discovery_policy as policy
from yt_playlist.core import discovery_cache_policy
from yt_playlist.core import artist_reconciliation
from yt_playlist.core import discovery_origin_policy
from yt_playlist.core.discovery_sources import source_identity
from yt_playlist.core.activity import WorkBudgetExceeded, work_budget
from yt_playlist.core.db_diagnostics import database_operation


class Discovery:
    def prepare_track_reassociation(self, video, previous, target, association, now):
        inputs = self.repository.track_reassociation_inputs(video, previous, target)
        for row in inputs['originals']:
            key = ([association, row['id']] if inputs['typed'] else
                   [association, row['recording_id'], row['source_token']])
            self.originals.retain('discovery', 'discovery_reassociation', json.dumps(key), json.dumps(row),
                'exact upload reassociation; retained source contribution' if inputs['typed'] else
                'exact upload reassociation; retained pre-merge evidence')
        reason = 'recording reassociation ' + str(association)
        for row in inputs['impressions']:
            self.originals.retain('discovery', 'discovery_impression_merge',
                json.dumps(['track', previous, target, row['id']]), json.dumps(row), reason)
        if inputs['legacy']:
            self.originals.retain('discovery', 'discovery_legacy_offer_merge',
                json.dumps(['track', previous, target]), json.dumps(inputs['legacy']), reason)
        member, remaining, sources = inputs['member'], inputs['remaining'], inputs['sources']
        membership_found = None
        retain_member = bool(member and (remaining is None or sources))
        if retain_member:
            membership_found = member['found_at'] if inputs['typed'] or remaining is None else min(
                (row['found_at'] for row in sources if row['found_at'] is not None), default=now)
        identities = [None if inputs['typed'] else source_identity(row['source_token']) for row in sources]
        return inputs, retain_member, membership_found, identities

    def finish_track_reassociation(self, prepared, *, association=None):
        inputs, retain_member, found, identities = prepared
        self.repository.finish_track_reassociation(inputs, retain_member=retain_member,
            membership_found=found, source_identities=identities, association=association)

    def __init__(self, repository, artists, eligibility, files, *, originals, tracks, albums, genres,
                 metadata, keys, proposals=None, settings=None):
        self.repository, self.artists = repository, artists
        self.eligibility, self.files = eligibility, files
        self.proposals, self.settings = proposals, settings
        self.originals = originals
        self.tracks, self.albums, self.genres = tracks, albums, genres
        self.metadata, self.keys = metadata, keys

    def _origin(self, token, *, account_id=None, import_key=None):
        if not isinstance(token, str) or not token:
            raise ValueError('Discovery needs its source input')
        kind, _, value = token.partition(':')
        captured_account = None
        if kind == 'spotify_export':
            try:
                captured_account = int(value)
            except ValueError:
                pass
        facts = self.repository.origin_inputs(value if ':' in token else token, captured_account)
        genre = self.genres.resolve(value)[0] if kind == 'genre' else None
        return discovery_origin_policy.fields(token, facts, genre=genre, account_id=account_id, import_key=import_key)

    def _retain(self, kind, identity, row, reason):
        self.originals.retain('discovery', kind, json.dumps(identity), json.dumps(row), reason)

    def _retain_origins(self, origins, target, reason):
        for item in origins:
            old, current = item['origin']['id'], item['target']
            self._retain('discovery_origin_merge', [old, target], item['origin'], reason)
            if current is not None:
                for row in item['sources']:
                    self._retain('discovery_source_origin_merge', [row['id'], current], row, reason)
                for row in item['artists']:
                    self._retain('discovery_artist_origin_merge', [row['artist_id'], old, current], row, reason)

    def reconcile_artist(self, previous, target, *, source):
        with self.files.retained():
            inputs = self.repository.artist_reconciliation_inputs(previous, target)
            origins = self.repository.origin_merge_inputs('artist_id', previous, target)
            if inputs['typed']:
                origins = artist_reconciliation.origins_after_artist_move(origins, previous, target)
                for row in inputs['sources']:
                    self._retain('discovery_artist_source_merge', [previous, target, row['artist_id'], row['origin_id']], row, source)
                self._retain_origins(origins, target, source)
                for row in inputs['impressions']:
                    self._retain('discovery_impression_merge', ['artist', previous, target, row['id']], row, source)
                if inputs['legacy']:
                    self._retain('discovery_legacy_offer_merge', ['artist', previous, target], inputs['legacy'], source)
            for table in ('artist_scans', 'discovered_artists'):
                for row in inputs[table]:
                    captured = dict(row)
                    if table == 'discovered_artists' and inputs['typed']:
                        reference = tuple(row[field] for field in ('recovery_filename', 'recovery_digest', 'recovery_bytes'))
                        captured['explanation'] = (self.files.read(reference, kind='discovery-candidate',
                            owner=policy.explanation_owner(row)) if reference[0] is not None else {'because': [], 'fits': []})
                    identity = row['artist_id'] if inputs['typed'] else row['artist']
                    self.originals.retain('artist', 'artist_reconcile:'+table,
                        json.dumps([previous, target, identity]), json.dumps(captured), source)
            scan = artist_reconciliation.latest(inputs['artist_scans'], 'scanned_at', target, legacy=not inputs['typed'])
            candidate = artist_reconciliation.candidate(inputs['discovered_artists'], target,
                typed=inputs['typed'], duplicates=inputs['duplicates'])
            self.repository.apply_artist_reconciliation(previous, target, inputs, origins, scan, candidate)

    def select_starter_artist(self, name, browse_id, thumbnail, selected, now):
        identity = self.artists.ensure_artist(name, provider='youtube', provider_id=browse_id)
        if selected and self.repository.starter_count(identity) >= policy.STARTER_LIMIT:
            raise ValueError('Choose up to 12 artists to start.')
        source = 'artist_start:' + browse_id
        origin = self._origin(source) if selected and self.repository.explanation_files() else None
        self.repository.set_starter_artist(identity, browse_id, thumbnail, selected, now,
            score=policy.STARTER_SCORE, maximum=policy.STARTER_LIMIT, source=source, origin=origin)
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
        origins = self.repository.origin_merge_inputs('genre_key', previous, target)
        self._retain_origins(origins, target, 'explicit genre merge')
        self.repository.apply_origin_merge('genre_key', target, origins)

    def fetched_albums(self, browse_ids):
        return self.repository.fetched_albums(browse_ids)

    def remember_album_fetch(self, browse):
        return self.repository.remember_album_fetch(browse)

    def discovered_albums_for_artist(self, artist):
        return self.repository.discovered_albums_for_artist(self.artists.resolve(artist))

    def delete_discovered_albums(self, browse_ids):
        return self.repository.delete_discovered_albums(browse_ids)

    def prune_orphan_discovered_tracks(self, held_keys=()):
        gone = self.repository.orphan_track_keys() - set(held_keys)
        self.repository.delete_pool_entries((), (), gone)
        return len(gone)

    def upsert_discovered_album(self, browse_id, artist, title, year, thumbnail, now, genre=None, *, artist_id=None):
        subject = self.artists.state_id(artist_id if artist_id is not None else artist)
        identity = self.albums.observe(browse_id, title=title, artist=artist, year=year, thumbnail=thumbnail,
            genre=genre, artist_id=subject, rank=1)
        self.repository.add_album_membership(identity, now)

    def upsert_discovered_artist(self, artist, score, because, fits, thumbnail, now, genre=None, *, artist_id=None,
                                 source='discovery'):
        explanation = {'because': because or [], 'fits': fits or []}
        encoded = tuple(json.dumps(explanation[field], allow_nan=False) for field in ('because', 'fits'))
        genre_key = self.genres.resolve(genre)[0] if genre is not None else None
        identity = self.artists.state_id(artist_id if artist_id is not None else artist)
        with self.files.retained():
            native = self.repository.explanation_files()
            origin = self._origin(source) if native else None
            reference = self.files.publish('discovery-candidate', identity, explanation) if native else None
            self.repository.store_discovered_artist(identity, score, thumbnail, now, genre, source=source,
                                                    reference=reference, encoded=encoded, origin=origin, genre_key=genre_key)

    def retain_discovered_artist(self, artist, score):
        identity = self.artists.resolve_exact(artist)
        if identity is not None:
            origin = self._origin('discovery') if self.repository.explanation_files() else None
            self.repository.retain_discovered_artist(identity, score, source='discovery', origin=origin,
                excluded_kinds=('spotify_export', 'spotify_model'))

    def seed_spotify_artists(self, account_id, counts, now, *, min_plays=policy.SPOTIFY_MIN_PLAYS, import_key=None):
        rows = [(self.artists.state_id(name), float(plays)) for name, plays in counts.items() if plays >= min_plays]
        source = f'spotify_export:{account_id}'
        origin = self._origin(source, account_id=account_id, import_key=import_key) if self.repository.explanation_files() else None
        return self.repository.seed_spotify_artists(account_id, rows, now, source=source, origin=origin,
            excluded_kinds=('spotify_export',))

    def get_discovered_albums(self):
        return self.repository.get_discovered_albums()

    def present_home_tiles(self, album_ids, artist_ids):
        return self.repository.present_home_tiles(album_ids, artist_ids)

    def upsert_discovered_track(self, identity_key, video_id, title, artist, album, thumbnail,
                                genre, year, source_browse_id, now, audio=None, *, credits=None, duration_s=None):
        entry = self.tracks.upsert_track(video_id, title, artist, album, thumbnail=thumbnail, created_at=now,
            library=False, preferred_key=identity_key, credits=credits, duration_s=duration_s)
        subject = self.tracks.catalog_subject(entry)['recording_id']
        self.keys.add_alias(identity_key, subject, 'discovery')
        self.repository.add_track_membership(subject, entry, now)
        if source_browse_id:
            kind, identifier = source_identity(source_browse_id)
            origin = self._origin(source_browse_id) if self.repository.explanation_files() else None
            self.repository.retain_track_source(subject, source_browse_id, kind, identifier, video_id or None, now, origin)
            if self.repository.pooled_album(source_browse_id):
                self.albums.link(entry, source_browse_id, source='discovery')
        fields = {'genre': genre, 'mb_year': year, **{field: value for field, value in (audio or {}).items() if field in policy.TRACK_AUDIO_FIELDS}}
        self.metadata.fill_import(entry, fields, 'discovery_import', hint_source=source_browse_id or 'discovery', now=now)
        return self.tracks.catalog_subject(entry)['identity_key']

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
