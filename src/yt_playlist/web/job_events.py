"""SSE progress replay shared by sync and enrichment jobs."""
import asyncio
import json


def enrichment_progress(event):
    """Keep progress facts while excluding provider/internal diagnostic fields."""
    kind = event.get("type")
    kind = kind if kind in {"track", "activity", "done", "err"} else "info"
    result = {"type": kind}
    for key in ("i", "n", "total", "remaining", "conflicts"):
        value = event.get(key)
        if type(value) is int and value >= 0:
            result[key] = value
    for key in ("warning", "waiting", "blocked", "deferred"):
        if event.get(key):
            result[key] = True
    if kind == "activity":
        # Song identity and provider attribution remain useful to progress readers.
        for key in ("provider", "title", "artist"):
            if isinstance(event.get(key), str):
                result[key] = event[key]
    if kind == "track" and isinstance(event.get("video_id"), str):
        result["video_id"] = event["video_id"]
    if kind == "err":
        text = "Enrichment did not finish. Updated details are kept."
    elif kind == "done":
        if result.get("blocked"):
            text = "No enrichment sources are ready. Your current details are kept."
        else:
            total = result.get('total', 0)
            text = f"Checked {total} song{'' if total == 1 else 's'}."
            if result.get("remaining"):
                remaining = result['remaining']
                text += f" {remaining} song{' remains' if remaining == 1 else 's remain'} for later."
            if result.get("conflicts"):
                conflicts = result['conflicts']
                text += f" {conflicts} disagreement{'' if conflicts == 1 else 's'} to review."
    elif kind == "track" and result.get("n"):
        text = f"{result.get('i', 0)} of {result['n']} songs checked."
    elif result.get("warning") or result.get("deferred"):
        text = "Some details remain for later. Checking the rest…"
    elif result.get("waiting"):
        text = "Waiting for enrichment…"
    else:
        text = "Checking song details…"
    return {**result, "text": text}


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
            yield "data: " + json.dumps(transform({"type": "info", "truncated": True, "skipped": skipped,
                "text": "Earlier progress has expired. Showing recent progress."})) + "\n\n"
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
