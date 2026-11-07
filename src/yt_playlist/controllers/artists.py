"""Choose artist identity evidence while the repository stores its rows."""
from yt_playlist.core import artist_credits
from yt_playlist.core.metadata_contract import clock


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

    def record_credit_choice(self, entry_id, value, *, now=None):
        current = self.repository.credit_inputs(entry_id)
        if len(current['artists']) == 1:
            with self.repository.transaction():
                self.repository.add_alias(current['artists'][0], value, 'user_correction')
        elif not current['artists']:
            structured = [{'name': value, 'id': None}]
            credit = artist_credits.normalize_credit(value, structured, 'user_correction')
            identity = self.ensure_artist(value, provider='user_correction')
            credit = artist_credits.bind_credit(credit, [identity])
            self.repository.store_credit(current['entry_id'], credit, structured,
                                         'user_correction', clock(now), typed=current['typed'])

    def resolve(self, value):
        return self.repository.resolve(value)

    def resolve_exact(self, value):
        return self.repository.resolve(value, exact=True)

    def state_id(self, value):
        identity = self.repository.resolve(value, exact=True)
        if identity is not None:
            return identity
        if isinstance(value, int) or str(value).startswith('@artist:'):
            raise ValueError(f'Unknown artist reference: {value}')
        return self.ensure_artist(str(value))

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
