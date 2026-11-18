"""Confirm a durable SQL-owned capture before admitting destructive work."""


class PlaylistBackups:
    def __init__(self, repository, files):
        self.repository, self.files = repository, files

    def capture(self, payload, *, files=None):
        files = files or self.files
        integrity = files.describe(payload)
        identity = self.repository.reserve(payload, integrity)
        # Keep the prepared file's lease until its owning SQL receipt is ready.
        with files.write_reserved(identity, payload, expected=integrity) as reference:
            self.repository.complete(identity, reference)
            return reference[0]
