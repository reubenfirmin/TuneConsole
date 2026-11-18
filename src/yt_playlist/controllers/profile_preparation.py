"""Bounded installed-format conversion over explicit SQL and file capabilities."""
from yt_playlist.core import fao


class ProfilePreparation:
    def __init__(self, repository, files):
        self.repository, self.files = repository, files

    def prepare(self, kind, limit=128):
        if type(limit) is not int or not 1 <= limit <= 128:
            raise ValueError('Profile preparation batch must be 1 to 128')
        after = self.files.cursor(kind)
        rows = self.repository.page(kind, after, limit)
        if not rows and after:
            rows = self.repository.page(kind, '', limit)
        references = self.files.write_legacy(self.files.describe(kind, rows))
        if self.files.originals(kind, references) != [row[2] for row in rows]:
            raise fao.CorruptFile('Profile preparation changed original bytes')
        self.repository.publish(kind, references)
        # The cursor is a hint. A rollback leaves nonempty bodies behind it;
        # wrapping picks those and continuing legacy writes up on the next pass.
        self.files.advance(kind, rows[-1][0] if len(rows) == limit else '')
        return len(rows)
