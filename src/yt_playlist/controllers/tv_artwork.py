"""Optional TV photography acquisition, separate from playback and image policy."""
from yt_playlist.util.matching import search_squash


def pending_names(requests, names, cache, now):
    return [name for name in dict.fromkeys([*requests.values(), *names])
            if now >= cache.get(search_squash(name), {}).get('retry_at', 0)]


def search_images(name, results):
    return [image for artist in results
            if search_squash(artist.get('artist') or artist.get('title') or '') == search_squash(name)
            if (image := artist.get('thumbnail'))]


def artwork_value(photos, now, failed, retry_at=0):
    return dict(images=list(dict.fromkeys(photos))[:6],
                retry_at=max(now + (60 if failed else 86400), retry_at), at=now)


class TVArtwork:
    def __init__(self, proposals, observations, *, images, search, vendor):
        self.proposals, self.observations = proposals, observations
        self.images, self.search, self.vendor = images, search, vendor

    def prepare(self, names, now):
        pending = pending_names(self.proposals.get_proposals('tv_artist_artwork_requests') or {},
            names, self.proposals.get_proposals('tv_artist_artwork') or {}, now)
        if not pending:
            return False
        name, key = pending[0], search_squash(pending[0])
        observation = self.observations.artwork_observation(key)
        photos, _ = self.images(name)
        found, failed = self.search(name)
        vendor, vendor_failed, retry = self.vendor(name, now)
        value = artwork_value([*photos, *search_images(name, found), *vendor], now, failed or vendor_failed, retry)
        published = self.observations.publish_artwork(key, value, now, observation, name=name,
                                                     complete=not (failed or vendor_failed))
        return len(pending) > 1 or not published
