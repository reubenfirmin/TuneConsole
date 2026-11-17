"""Names of the current discovery owners used within a pinned read."""


class ReadOwners:
    typed = True

    def relation(self, name):
        return name if name in ('discovered_tracks', 'discovered_albums', 'discovered_artists') else 'main.' + name

    def installed_artist_ids(self, canonical_ids):
        return set(canonical_ids)


def read_owners(conn):
    return ReadOwners()
