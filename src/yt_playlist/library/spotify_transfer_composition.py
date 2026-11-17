"""Wire transfer commands to installed settings, providers and activity admission."""
import threading
import logging
from yt_playlist.controllers.spotify_transfer import SpotifyTransfer, TransferReports, TransferRejected
from yt_playlist.core.accounts import client_for
from yt_playlist.core.activity import activity_for
from yt_playlist.core.transfer_previews import TransferPreviews
from yt_playlist.library import spotify_library


def reports(ctx):
    if not hasattr(ctx, 'spotify_import_lock'):
        ctx.spotify_import_lock = threading.Lock()
    def publish(job):
        while not ctx.store.activity_paused:
            if ctx.store._lock.acquire(timeout=.025):
                try:
                    if not ctx.store.activity_paused:
                        owner.save(job)
                finally:
                    ctx.store._lock.release()
                break
    owner = TransferReports(ctx.store.settings, ctx.spotify_import_lock,
        now=lambda: ctx.now_fn(), publish_recovery=publish)
    return owner


def commands(ctx):
    if not hasattr(ctx, '_spotify_transfer'):
        store = ctx.store
        def destination(value):
            try:
                account_id = int(value) if value is not None else None
            except (ValueError, TypeError):
                raise TransferRejected('invalid', 'Choose a destination identity') from None
            try:
                account_id, client = client_for(store, ctx.client_provider(), account_id=account_id)
            except ValueError as exc:
                raise TransferRejected('unavailable', 'Connect the selected YouTube Music identity') from exc
            identity = next((i for i in store.accounts.get_accounts() if i.id == account_id), None)
            if identity is None:
                raise TransferRejected('unavailable', 'Connect the selected YouTube Music identity')
            return identity, client
        ctx._spotify_transfer = SpotifyTransfer(reports(ctx), TransferPreviews(),
            store.accounts.get_accounts, activity_for(ctx), destination=destination,
            parse=spotify_library.load_spotify_library,
            prepare=lambda selected, client, account_id: spotify_library.prepare_spotify_library(
                selected, client, store=store, account_id=account_id),
            transfer=lambda prepared, client, account_id, now: spotify_library.import_spotify_library(
                store, None, client, account_id, now, prepared=prepared),
            candidates=lambda client, item: spotify_library.find_album_candidates(client, item),
            save_album=lambda client, item, browse_id, account_id: spotify_library.save_selected_album(
                store, client, item, browse_id, account_id=account_id),
            imported=lambda now: store.settings.set_setting('spotify_library_imported_at', str(now)),
            now=lambda: ctx.now_fn(), logger=getattr(ctx, 'logger', logging.getLogger(__name__)),
            report_schema=spotify_library.SPOTIFY_REPORT_SCHEMA)
    return ctx._spotify_transfer
