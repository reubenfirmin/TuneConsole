"""Shared request-handling context for the route modules.

Every route in :mod:`yt_playlist.web.routes` is a closure over the same handful of
collaborators (the store, the per-identity client provider, a clock, the Jinja
templates, the sync-job registry, and the optional setup runtime). Bundling them
into one object lets each router be built with ``build(ctx)`` instead of threading
six arguments through every module.
"""
from __future__ import annotations

import logging
import threading
from dataclasses import dataclass, field

from fastapi import Request
from fastapi.templating import Jinja2Templates

from yt_playlist.library.ops import PlaylistOps
from yt_playlist.core.activity import Activity
from yt_playlist.web.jobs import SyncJobs

logger = logging.getLogger("yt_playlist.web")

# Settings key under which the expired-session banner state is persisted, so the banner survives a
# server restart (e.g. `uvicorn --reload` re-spawning the worker on every code change).
AUTH_EXPIRED_KEY = "auth_expired_identities"


async def request_form(request: Request):
    """Parse the body before dispatching a synchronous form handler to the request pool."""
    return await request.form()


def form_float(value):
    """Parse a form field to float, or None when it is missing or non-numeric. Lets a handler treat
    a malformed POST (a garbage slider value) as a no-op instead of raising and returning a 500."""
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def refresh_playlist_view(ctx, provider_playlist_id):
    """Refresh a connected extension's playlist after a confirmed mutation."""
    try:
        if provider_playlist_id:
            ctx.bridge.send_control({"type": "refresh-view", "playlist": provider_playlist_id})
    except Exception:  # noqa: BLE001 - notification failure must not change the mutation result
        ctx.logger.debug("refresh-view control not sent for %s", provider_playlist_id, exc_info=True)


@dataclass
class Ctx:
    store: object
    client_provider: object
    now_fn: object
    templates: Jinja2Templates
    jobs: SyncJobs
    work: Activity = field(default_factory=Activity)
    setup: object | None = None
    logger: logging.Logger = field(default=logger)
    # identities whose YouTube session has expired (id -> label), set during sync, cleared on
    # a successful re-sync or re-auth. Drives the "session expired, re-authenticate" banner. Seeded
    # from (and written through to) the store so the banner survives a restart; mutate ONLY via the
    # flag_/clear_ helpers below so the persisted copy stays in sync.
    auth_expired: dict = field(default_factory=dict)
    rec_worker: object | None = None   # decoupled recommendation worker (set in create_app)
    vibe_requests: object | None = None
    enrich_worker: object | None = None  # background enrichment worker (set in create_app)
    bridge: object | None = None       # shared Bridge instance the WS route and runtime read from
    radio: object | None = None        # saved-playlist Radio Session, set in create_app
    updater: object | None = None         # platform updater (Flatpak portal / macOS Sparkle)
    # Guards library sync so the background sync daemon and a manual POST /sync never run at once.
    sync_lock: object = field(default_factory=threading.Lock)
    # An extension connection can start a due sync without waiting for the next periodic check.
    sync_wake: object = field(default_factory=threading.Event)
    # A Spotify Account Data import may spend hours resolving tracks through the extension. This
    # both prevents duplicate jobs and lets packaged-app idle shutdown keep the process alive.
    spotify_import_lock: object = field(default_factory=threading.Lock)

    def __post_init__(self):
        try:
            self.auth_expired.update(self.store.accounts.auth_expirations())
        except (ValueError, TypeError):
            self.logger.warning("ignoring unreadable %s setting", AUTH_EXPIRED_KEY)

    def _persist_auth_expired(self, *, account_id=None, label=None, clear_all=False):
        result = self.store.accounts.update_auth_expiration(
            account_id=account_id, label=label, clear_all=clear_all)
        if result is None:
            return
        current, invalid = result
        if invalid:
            self.logger.warning('ignoring unreadable %s setting', AUTH_EXPIRED_KEY)
        self.auth_expired.clear()
        self.auth_expired.update(current)

    def flag_auth_expired(self, account_id, label):
        """Record (and persist) that an identity's session expired. Drives the re-auth banner."""
        self.auth_expired[account_id] = label or str(account_id)
        self._persist_auth_expired(account_id=account_id, label=self.auth_expired[account_id])

    def clear_auth_expired(self, account_id):
        """Clear one identity's expired flag (a successful re-sync of that account)."""
        self.auth_expired.pop(account_id, None)
        self._persist_auth_expired(account_id=account_id)

    def clear_all_auth_expired(self):
        """Clear every expired flag (a re-auth / sign-out resolves the whole banner)."""
        self.auth_expired.clear()
        # A healthy bridge can clear RAM while repair discards writes. A fresh
        # confirmation after repair must also remove the stale persisted flag.
        self._persist_auth_expired(clear_all=True)

    def now(self):
        return self.now_fn()

    def clients(self):
        return self.client_provider()

    def ops(self) -> PlaylistOps:
        """A PlaylistOps bound to this context's store, client provider, and clock."""
        return PlaylistOps(self.store, self.client_provider, self.now_fn,
                           rating_updated=self.rec_worker.trigger_rating_update if self.rec_worker else None)
