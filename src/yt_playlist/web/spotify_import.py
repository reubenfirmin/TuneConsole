"""Persisted UI state for the background Spotify library import."""
from __future__ import annotations

from yt_playlist.core.transfer_previews import TransferPreviews
from yt_playlist.controllers.spotify_transfer import TransferReports
from yt_playlist.library.spotify_transfer_policy import (
    JOB_KEY, ACTIVE_STATUSES, estimate, retained_report as _retained_report, recovered as _recovered,
)


def load_job(store) -> dict | None:
    return TransferReports(store.settings, None, now=None, publish_recovery=None).load()


def save_job(store, job: dict) -> None:
    return TransferReports(store.settings, None, now=None, publish_recovery=None).save(job)


def current_job(ctx) -> dict | None:
    """Return current state, turning a pre-restart running marker into a retryable failure."""
    from yt_playlist.library.spotify_transfer_composition import reports
    return reports(ctx).current()
