"""Reconcile verified provider releases through their independent owners."""
import json

from yt_playlist.core import album_aliases as policy
from yt_playlist.core.metadata_contract import clock


class AlbumAliases:
    def __init__(self, repository, albums, originals, discovery, merge_feedback, guard):
        self.repository, self.albums = repository, albums
        self.originals, self.discovery = originals, discovery
        self.merge_feedback, self.guard = merge_feedback, guard

    def observe(self, browse, album, *, now=None):
        with self.guard():
            existing = self.repository.fingerprint_inputs(browse)
            comparison = policy.fingerprint(album)
            if comparison is None:
                return not existing['shared']
            digest, videos = comparison
            if existing['shared'] and existing['digest'] != digest:
                return False
            at = clock(now)
            identity = self.albums.observe(browse, title=album['title'],
                artist=', '.join(artist['name'] for artist in album['artists']), year=album['year'], rank=1)
            candidates = self.repository.remember_fingerprint(browse, identity, digest, at)
            for candidate in candidates:
                target, retired = sorted((identity, candidate))
                inputs = self.repository.merge_inputs(target, retired)
                if inputs['conflict'] or not all(policy.compatible(inputs[key], videos) for key in ('target', 'retired')):
                    continue
                self.originals.retain('album', 'album_alias_reconciliation', str(retired),
                    json.dumps(inputs['original']),
                    f'complete ordered provider responses matched {digest}; canonical album {target}')
                self.discovery.merge_albums(retired, target)
                self.merge_feedback(target, retired, at)
                # Publish identity last. Earlier owners drain the retired rows atomically,
                # so retry after interruption does not duplicate counts or responses.
                self.repository.apply_merge(target, retired, digest, at)
                identity = target
            return True
