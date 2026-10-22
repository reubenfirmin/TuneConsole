"""SSE progress replay shared by sync and enrichment jobs."""
import asyncio
import json


async def stream_job(job, request, *, transform=lambda event: event, terminal=None):
    try:
        cursor = max(0, int(request.headers.get("last-event-id", "0")))
    except ValueError:
        cursor = 0
    cursor = min(cursor, job.events.position)
    while True:
        # Read done first: any final event appended before completion must be drained before end.
        done = job.done
        skipped, batch = job.events.since(cursor)
        if skipped:
            yield "data: " + json.dumps({"type": "info", "truncated": True, "skipped": skipped,
                "text": "Earlier progress has expired. Showing recent progress."}) + "\n\n"
        for event_id, event in batch:
            # Data first also keeps line-oriented progress readers compatible.
            yield f"data: {json.dumps(transform(event))}\nid: {event_id}\n\n"
            cursor = event_id
        if done:
            event = terminal() if terminal else {"type": "end", "error": job.error}
            yield f"data: {json.dumps(event)}\n\n"
            return
        if job.done:
            continue  # completion during delivery may have appended one last event
        if await request.is_disconnected():
            return
        await asyncio.sleep(.1)
