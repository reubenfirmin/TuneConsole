"""RecRepo: backwards-compatible facade over the focused recommendation DAOs.

The recommendation persistence was one 40-method class; it's now split by responsibility into three
cohesive DAOs, each on the shared Repo base (+ @synchronized):

  - RecModelRepo   (repos/rec_model.py)   : learned model: weights, feedback, embedding vectors
  - RecSurfaceRepo (repos/rec_surface.py) : serving surfaces: impressions, proposals, similar cache
  - RecQueryRepo   (repos/rec_query.py)   : read-only library queries + candidate generators

RecRepo composes the three and delegates by attribute, so existing `RecDao(store).X()` and
`store.rec.X()` call sites keep working unchanged; reach the parts directly via `store.rec.model`,
`.surface`, `.query` in new code.
"""
from yt_playlist.repos.base import Repo
from yt_playlist.repos.rec_model import RecModelRepo
from yt_playlist.repos.rec_query import (  # noqa: F401  (re-exported for existing importers)
    GENERATED_GROUP, RecQueryRepo)
from yt_playlist.repos.rec_surface import RecSurfaceRepo


class RecRepo(Repo):
    def __init__(self, db):
        super().__init__(db)
        self.model = RecModelRepo(db)
        self.surface = RecSurfaceRepo(db)
        self.query = RecQueryRepo(db)
        self._parts = (self.model, self.surface, self.query)

    def __getattr__(self, name):
        # Delegate any method this facade doesn't define to the sub-DAO that owns it. Only hit on a
        # miss; __dict__.get avoids recursion before _parts is set during __init__.
        for part in self.__dict__.get("_parts", ()):
            attr = getattr(part, name, None)
            if attr is not None:
                return attr
        raise AttributeError(f"{type(self).__name__!r} has no attribute {name!r}")
