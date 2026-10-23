"""Minimal identity-setup collaborator shared by route and browser tests."""


class _FakeRuntime:
    """Minimal setup collaborator for create_app(setup=...)."""
    def __init__(self, store, configured=False, credentials_present=False):
        self.store = store
        self._configured = configured
        self.credentials_present = credentials_present
        self.applied = None
        self.raise_value_error = None
    @property
    def configured(self):
        return self._configured
    def clients(self):
        return {}
    def apply_setup(self, identities):
        if self.raise_value_error:
            raise ValueError(self.raise_value_error)
        self.applied = identities
        self._configured = True
