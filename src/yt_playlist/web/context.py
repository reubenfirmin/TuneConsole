"""Shared request-handling context for the route modules.

Every route in :mod:`yt_playlist.web.routes` is a closure over the same handful of
collaborators (the store, the per-identity client provider, a clock, the Jinja
templates, the sync-job registry, and the optional setup runtime). Bundling them
into one object lets each router be built with ``build(ctx)`` instead of threading
six arguments through every module.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field

from fastapi import HTTPException
from fastapi.templating import Jinja2Templates

from yt_playlist.library.ops import PlaylistOps
from yt_playlist.web.jobs import SyncJobs

logger = logging.getLogger("yt_playlist.web")


@dataclass
class Ctx:
    store: object
    client_provider: object
    now_fn: object
    templates: Jinja2Templates
    jobs: SyncJobs
    setup: object | None = None
    logger: logging.Logger = field(default=logger)
    # identities whose YouTube session has expired (id -> label), set during sync, cleared on
    # a successful re-sync. Drives the "session expired, re-authenticate" banner.
    auth_expired: dict = field(default_factory=dict)
    rec_worker: object | None = None   # decoupled recommendation worker (set in create_app)

    def now(self):
        return self.now_fn()

    def clients(self):
        return self.client_provider()

    def ops(self) -> PlaylistOps:
        """A PlaylistOps bound to this context's store, client provider, and clock."""
        return PlaylistOps(self.store, self.client_provider, self.now_fn)

    def playlist_by_id(self, pid):
        pl = self.store.get_playlist(pid)
        if pl is None:
            raise HTTPException(status_code=404, detail="playlist not found")
        return pl
