"""Choose artist identity evidence while the repository stores its rows."""


class Artists:
    def __init__(self, repository):
        self.repository = repository

    def ensure_artist(self, name, *, provider=None, provider_id=None):
        verified = bool(provider and provider_id)
        with self.repository.transaction():
            identity = (self.repository.provider_id(provider, provider_id) if verified
                        else self.repository.provisional_id(name))
            if identity is None:
                identity = self.repository.create(name, provisional=not verified)
                if verified:
                    self.repository.add_provider(identity, provider, provider_id)
            if name:
                self.repository.add_alias(identity, name, provider or 'unidentified_credit')
            return identity

    def resolve(self, value):
        return self.repository.resolve(value)

    def get_artist(self, value):
        return self.repository.get_artist(value)

    def key(self, value):
        return self.repository.key(value)

    def public_key(self, value):
        return self.repository.public_key(value)

    def public_keys(self, values):
        return self.repository.public_keys(values)

    def name_keys(self):
        return self.repository.name_keys()

    def album_credits(self, browse_id):
        return self.repository.album_credits(browse_id)

    def track_credits(self, entry_id):
        return self.repository.track_credits(entry_id)

    def track_keys(self, *, library=False, keys=None):
        return self.repository.track_keys(library=library, keys=keys)
