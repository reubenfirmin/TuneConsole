"""Read-only network log (`/network`): the evidence side of the egress guard.

Shows the hard-coded allowlist alongside the most recent entries from the guard's
rotating log, so you can see for yourself that the server only ever talks to
YouTube and the metadata-enrichment hosts. The enforcement lives in
:mod:`yt_playlist.egress`; this page just surfaces what it recorded.
"""
from fastapi import APIRouter, Request

from yt_playlist.egress import ALLOWED_DOMAINS, guard


def _parse(line):
    """Turn one log line ('<ts> verdict=.. via=.. method=.. host=.. path=.. ...') into a dict.

    The message is space-separated key=value pairs (none of the values contain spaces:
    paths are percent-encoded, query strings are stripped), so a simple split is safe.
    """
    idx = line.find("verdict=")
    if idx < 0:
        return {"ts": "", "raw": line}
    row = {"ts": line[:idx].strip()}
    for tok in line[idx:].split():
        k, _, v = tok.partition("=")
        row[k] = v
    return row


def build(ctx) -> APIRouter:
    router = APIRouter()
    templates = ctx.templates

    @router.get("/network")
    def network(request: Request):
        rows = [_parse(ln) for ln in reversed(guard().recent(500))]   # newest first
        return templates.TemplateResponse(request, "network.html", {
            "allowlist": sorted(ALLOWED_DOMAINS),
            "rows": rows,
        })

    return router
