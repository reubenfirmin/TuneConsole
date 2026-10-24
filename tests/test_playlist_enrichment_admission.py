"""Playlist waterfalls reserve their scope before concurrent requests can rejoin."""
from concurrent.futures import ThreadPoolExecutor
import threading
from types import SimpleNamespace

import pytest

from yt_playlist.core.activity import Activity, WorkClosing
from yt_playlist.web import jobs


def test_concurrent_playlist_requests_share_a_fully_initialized_job(monkeypatch):
    assigning, release, second_entered = (threading.Event() for _ in range(3))
    workers, waterfalls = [], []
    original = jobs.SyncJob

    class PausedMetadataJob(original):
        def __setattr__(self, name, value):
            if name == 'playlist_id' and value == 7 and not assigning.is_set():
                assigning.set()
                assert release.wait(5)
            super().__setattr__(name, value)

    monkeypatch.setattr(jobs, 'SyncJob', PausedMetadataJob)
    monkeypatch.setattr(jobs.enrichment, 'load_config', lambda store: {})
    monkeypatch.setattr(jobs.waterfall, 'run_waterfall',
                        lambda store, tracks, config, **kw: waterfalls.append(tracks))
    registry = jobs.SyncJobs()
    ctx = SimpleNamespace(jobs=registry, work=SimpleNamespace(start_thread=workers.append),
        store=SimpleNamespace(tracks=SimpleNamespace(tracks_for_waterfall=lambda pid: [pid])))

    def second_request():
        second_entered.set()
        return jobs.start_playlist_enrichment(ctx, 7)

    with ThreadPoolExecutor(max_workers=2) as callers:
        first = callers.submit(jobs.start_playlist_enrichment, ctx, 7)
        try:
            assert assigning.wait(5)
            second = callers.submit(second_request)
            assert second_entered.wait(5)
            with pytest.raises(TimeoutError):
                second.result(timeout=.1)
        finally:
            release.set()
        one, two = first.result(timeout=5), second.result(timeout=5)

    assert one is two is registry.find_active(7)
    assert one.playlist_id == 7 and one.source == 'waterfall'
    assert len(workers) == 1
    other = jobs.start_playlist_enrichment(ctx, 8)
    assert other is not one and registry.find_active(8) is other
    assert len(workers) == 2
    workers[0]()
    assert waterfalls == [[7]] and one.done
    assert registry.get(one.id) is one and registry.find_active(7) is None
    again = jobs.start_playlist_enrichment(ctx, 7)
    assert again is not one and len(workers) == 3
    assert registry.find_active(8) is other


def test_failed_worker_admission_releases_playlist_reservation():
    work = Activity()
    work.close_admission()
    ctx = SimpleNamespace(jobs=jobs.SyncJobs(), work=work)
    with pytest.raises(WorkClosing):
        jobs.start_playlist_enrichment(ctx, 7)
    assert not ctx.jobs.has_active()
    failed = ctx.jobs.get(1)
    assert failed.done
