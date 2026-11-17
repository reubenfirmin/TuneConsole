"""Library sync: kick off a background sync and stream its progress over SSE."""
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse

from yt_playlist.web.jobs import start_library_sync
from yt_playlist.web.job_events import stream_job
from yt_playlist.library.sync_composition import commands


def build(ctx) -> APIRouter:
    router = APIRouter()
    jobs = ctx.jobs
    sync = commands(ctx)

    @router.get("/sync/active")
    def sync_active():
        # The dashboard console polls this to attach to whatever library sync is running (the
        # automatic background one included), then streams it via /sync/events/{job_id}.
        job = sync.current()
        return {"job_id": job.id if job else None}

    @router.get("/sync/recent")
    def sync_recent():
        """Retain access to the last import's detail stream after it finishes."""
        return sync.recent()

    @router.post("/sync")
    def do_sync():
        # Run the (slow, network-bound) sync in a background thread and stream progress over SSE.
        job = start_library_sync(ctx)
        return JSONResponse({"job_id": job.id})

    @router.get("/sync/status/{job_id}")
    def sync_status(job_id: int):
        """Retained terminal observation when a progress stream cannot reconnect."""
        job = jobs.get(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="Sync job is missing or expired.")
        return {"done": job.done, "error": job.error, "outcome": job.outcome}

    @router.get("/sync/events/{job_id}")
    async def sync_events(request: Request, job_id: int):
        job = jobs.get(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="Sync job is missing or expired.")

        return StreamingResponse(stream_job(job, request, terminal=lambda: {
            "type": "end", "error": job.error, "outcome": job.outcome, "result": job.result}),
            media_type="text/event-stream")

    return router
