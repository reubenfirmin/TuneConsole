"""Library sync: kick off a background sync and stream its progress over SSE."""
from yt_playlist.core.activity import activity_for

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse

from yt_playlist.web.jobs import run_library_sync
from yt_playlist.web.job_events import stream_job


def build(ctx) -> APIRouter:
    router = APIRouter()
    jobs = ctx.jobs

    @router.get("/sync/active")
    def sync_active():
        # The dashboard console polls this to attach to whatever library sync is running (the
        # automatic background one included), then streams it via /sync/events/{job_id}.
        job = jobs.find_active_library()
        return {"job_id": job.id if job else None}

    @router.post("/sync")
    def do_sync():
        # Run the (slow, network-bound) sync in a background thread and stream progress over SSE.
        job = jobs.create(kind="library")

        def run():
            with ctx.sync_lock:               # never overlap the background sync daemon
                run_library_sync(ctx, job)

        activity_for(ctx).start_thread(run)
        return JSONResponse({"job_id": job.id})

    @router.get("/sync/events/{job_id}")
    async def sync_events(request: Request, job_id: int):
        job = jobs.get(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="Sync job is missing or expired.")

        return StreamingResponse(stream_job(job, request, terminal=lambda: {
            "type": "end", "error": job.error, "outcome": job.outcome, "result": job.result}),
            media_type="text/event-stream")

    return router
