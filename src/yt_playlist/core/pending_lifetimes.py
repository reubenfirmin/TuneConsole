"""Cross-process file fences for admitted group captures, including crash recovery."""
from contextlib import contextmanager

from yt_playlist.core import policy_documents
from yt_playlist.core.activity import WorkBudgetExceeded
from yt_playlist.core.evidence_ledger import _mkdir


class Marker:
    def __init__(self, path, stream, unlock):
        self.path, self.stream, self.unlock = path, stream, unlock
        self.closed = False

    def close(self, *, retired=False):
        if not self.closed:
            self.unlock()
            self.stream.close()
            self.closed = True
        if retired:
            self.path.unlink(missing_ok=True)


class PendingLifetimes:
    def __init__(self, directory):
        self.directory = directory

    def reserve(self, identity):
        _mkdir(self.directory)
        path = self.directory / (identity + '.lock')
        stream = path.open('x+b')
        unlock = policy_documents._try_file_lock(stream, exclusive=True)
        if unlock is None:
            stream.close()
            path.unlink(missing_ok=True)
            raise WorkBudgetExceeded('Musical group capture lifetime could not be reserved')
        return Marker(path, stream, unlock)

    @contextmanager
    def claim(self, identity):
        if len(identity) != 32 or any(c not in '0123456789abcdef' for c in identity):
            yield None
            return
        path = self.directory / (identity + '.lock')
        try:
            stream = path.open('r+b')
        except FileNotFoundError:
            yield None
            return
        with stream:
            unlock = policy_documents._try_file_lock(stream, exclusive=True)
            if unlock is None:
                yield None
                return
            marker = Marker(path, stream, unlock)
            try:
                yield marker
            finally:
                marker.close()
