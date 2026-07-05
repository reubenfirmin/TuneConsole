"""Shared request-handling context for the route modules.

Every route in :mod:`yt_playlist.web.routes` is a closure over the same handful of
collaborators (the store, the per-identity client provider, a clock, the Jinja
templates, the sync-job registry, and the optional setup runtime). Bundling them
into one object lets each router be built with ``build(ctx)`` instead of threading
six arguments through every module.
"""
from __future__ import annotations

import json
import logging
import threading
from dataclasses import dataclass, field

from fastapi import HTTPException
from fastapi.templating import Jinja2Templates

from yt_playlist.library.ops import PlaylistOps
from yt_playlist.web.jobs import SyncJobs

logger = logging.getLogger("yt_playlist.web")

# Settings key under which the expired-session banner state is persisted, so the banner survives a
# server restart (e.g. `uvicorn --reload` re-spawning the worker on every code change).
AUTH_EXPIRED_KEY = "auth_expired_identities"


def form_float(value):
    """Parse a form field to float, or None when it is missing or non-numeric. Lets a handler treat
    a malformed POST (a garbage slider value) as a no-op instead of raising and returning a 500."""
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


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
    # a successful re-sync or re-auth. Drives the "session expired, re-authenticate" banner. Seeded
    # from (and written through to) the store so the banner survives a restart; mutate ONLY via the
    # flag_/clear_ helpers below so the persisted copy stays in sync.
    auth_expired: dict = field(default_factory=dict)
    rec_worker: object | None = None   # decoupled recommendation worker (set in create_app)
    enrich_worker: object | None = None  # background enrichment worker (set in create_app)
    bridge: object | None = None       # shared Bridge instance the WS route and runtime read from
    radio: object | None = None        # #93 in-process RadioSession (dynamic radio), set in create_app
    # Guards library sync so the background sync daemon and a manual POST /sync never run at once.
    sync_lock: object = field(default_factory=threading.Lock)

    def __post_init__(self):
        raw = self.store.get_setting(AUTH_EXPIRED_KEY)
        if raw:
            try:                                    # keys are ints (identity ids) so on_auth_ok's pop matches
                self.auth_expired.update({int(k): v for k, v in json.loads(raw).items()})
            except (ValueError, TypeError):         # corrupt/legacy value: ignore, the next sync rebuilds it
                self.logger.warning("ignoring unreadable %s setting", AUTH_EXPIRED_KEY)

    def _persist_auth_expired(self):
        if self.auth_expired:
            self.store.set_setting(AUTH_EXPIRED_KEY,
                                   json.dumps({str(k): v for k, v in self.auth_expired.items()}))
        else:
            self.store.delete_setting(AUTH_EXPIRED_KEY)

    def flag_auth_expired(self, identity_id, label):
        """Record (and persist) that an identity's session expired. Drives the re-auth banner."""
        self.auth_expired[identity_id] = label or str(identity_id)
        self._persist_auth_expired()

    def clear_auth_expired(self, identity_id):
        """Clear one identity's expired flag (a successful re-sync of that account)."""
        if self.auth_expired.pop(identity_id, None) is not None:
            self._persist_auth_expired()

    def clear_all_auth_expired(self):
        """Clear every expired flag (a re-auth / sign-out resolves the whole banner)."""
        if self.auth_expired:
            self.auth_expired.clear()
            self._persist_auth_expired()

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
