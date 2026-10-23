import asyncio
import json

import pytest
from fastapi.testclient import TestClient
from starlette.requests import Request

from yt_playlist.web.app import create_app
from yt_playlist.web.job_events import stream_job
from yt_playlist.web.jobs import EventLog, SyncJob, SyncJobs


def test_completed_count_is_bounded_without_evicting_active_jobs():
    jobs = SyncJobs(completed_limit=3)
    active, _ = jobs.create_or_rejoin("fresh_pool", {"genre": "jazz"})
    active.playlist_id = 7
    for i in range(20):
        job = jobs.create("library")
        job.result = {"saved": i}
        job.done = True
    assert len(jobs._jobs) == 4
    assert jobs.get(2) is None
    assert jobs.get(job.id).result == {"saved": 19}
    assert jobs.create_or_rejoin("fresh_pool", {"genre": "jazz"}) == (active, False)
    assert jobs.find_active(7) is active
    assert jobs.has_active()
    active.done = True
    assert not jobs.has_active()
    assert len(jobs._jobs) == 3


def test_completed_age_uses_completion_time_and_recent_results_remain_readable():
    now = [0]
    jobs = SyncJobs(completed_ttl=60, clock=lambda: now[0])
    job, _ = jobs.create_or_rejoin("fresh_pool", {"genre": "jazz"})
    now[0] = 1000
    assert jobs.get(job.id) is job  # old active jobs do not expire
    job.done = True
    now[0] += 59
    assert jobs.latest("fresh_pool", {"genre": "jazz"}) is job
    now[0] += 1
    assert jobs.get(job.id) is None
    assert jobs.latest("fresh_pool", {"genre": "jazz"}) is None


def test_event_log_reports_gaps_and_keeps_absolute_cursors():
    log = EventLog(limit=3)
    for n in range(10):
        log.append({"text": str(n)})
    assert len(log) == 3 and log[-1] == {"text": "9"}
    skipped, batch = log.since(2)
    assert skipped == 5
    assert batch == [(8, {"text": "7"}), (9, {"text": "8"}), (10, {"text": "9"})]
    assert log.since(9) == (0, [(10, {"text": "9"})])


def _request(cursor=None):
    headers = [] if cursor is None else [(b"last-event-id", str(cursor).encode())]
    return Request({"type": "http", "headers": headers})


def test_sse_reconnect_replays_only_unseen_events_and_signals_expiry():
    job = SyncJob(1, event_limit=2)
    for n in range(5):
        job.events.append({"type": "info", "text": str(n)})
    job.done = True

    async def read(cursor):
        return [chunk async for chunk in stream_job(job, _request(cursor))]

    recent = asyncio.run(read(4))
    assert len(recent) == 2 and 'id: 5' in recent[0]
    assert '"text": "4"' in recent[0] and '"type": "end"' in recent[1]
    expired = asyncio.run(read(1))
    assert json.loads(expired[0].splitlines()[0][6:])["skipped"] == 2
    assert 'id: 4' in expired[1] and 'id: 5' in expired[2]
    assert '"type": "end"' in expired[-1]


def test_sse_drains_final_event_appended_during_delivery():
    job = SyncJob(1)
    job.events.append({"text": "first"})

    async def read():
        stream = stream_job(job, _request())
        first = await anext(stream)
        job.events.append({"text": "final"})
        job.done = True
        return [first, *[chunk async for chunk in stream]]

    chunks = asyncio.run(read())
    assert '"text": "final"' in chunks[-2]
    assert '"type": "end"' in chunks[-1]


def test_evicted_job_endpoints_explain_missing_or_expired(store):
    app = create_app(store, lambda: {})
    jobs = app.state.ctx.jobs
    jobs._completed_limit = 0
    job = jobs.create()
    job.done = True
    client = TestClient(app, base_url="http://127.0.0.1")
    for path in ("/sync/events/", "/playlist/enrich/events/", "/track/genre-lookup/events/"):
        response = client.get(path + str(job.id))
        assert response.status_code == 404
        assert "expired" in response.json()["detail"]


@pytest.mark.parametrize("kind", ["fresh_pool", "library_pool"])
@pytest.mark.parametrize("missing", ["evicted", "unknown", "wrong_kind"])
def test_missing_pool_poll_returns_terminal_recovery_card(store, kind, missing):
    app = create_app(store, lambda: {})
    jobs = app.state.ctx.jobs
    jobs._completed_limit = 0
    job = jobs.create("library" if missing == "wrong_kind" else kind)
    if missing == "evicted":
        job.done = True
    job_id = job.id if missing != "unknown" else job.id + 100
    client = TestClient(app, base_url="http://127.0.0.1")
    path = f"/home/{kind.replace('_', '-')}/{job_id}"
    response = client.get(path, headers={"HX-Request": "true"})
    assert response.status_code == 200  # HTMX must swap the terminal fragment.
    assert 'aria-busy="false"' in response.text
    assert "Mix build expired" in response.text
    assert 'hx-post="/home/refresh-cards"' in response.text
    assert 'hx-target="#home-mode-cards"' in response.text
    assert "hx-get=" not in response.text and "hx-trigger=" not in response.text
    assert client.get(path).status_code == 404
