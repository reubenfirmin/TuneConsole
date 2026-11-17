"""Tools > Discovery Pools (#53): a read-mostly view of the three discovery pools (albums, artists,
tracks) with engagement stats and the projected garbage-collection date (#52), plus an add-to-
collection action that Likes a discovered track (-> Liked Music, so it leaves the pool on next sync)."""
from typing import Literal
from collections import Counter
from uuid import UUID

from fastapi import APIRouter, Form, Request
from fastapi.responses import Response
from pydantic import BaseModel, Field

from yt_playlist.web.context import Ctx


def track_pool_summary(tracks):
    """Distributions of saved metadata, independent of provider processing timestamps."""
    total = len(tracks)
    genres, eras = Counter(), Counter()
    labels = {}
    missing_genre = missing_year = missing_audio = without_metadata = 0
    for track in tracks:
        genre = (track.get("genre") or "").strip()
        genres[genre.casefold()] += 1
        labels.setdefault(genre.casefold(), genre)
        year = str(track.get("year") or "").strip()
        decade = f"{int(year[:4]) // 10 * 10}s" if len(year) >= 4 and year[:4].isdigit() else ""
        eras[decade] += 1
        audio = any(v is not None and v != "" for v in track.get("audio", {}).values())
        missing_genre += not genre
        missing_year += not decade
        missing_audio += not audio
        without_metadata += not genre and not decade and not audio

    def bar(label, count, unknown=False):
        return {"label": label, "count": count, "pct": round(count / total * 100, 1) if total else 0,
                "unknown": unknown}

    ranked = sorted(((g, n) for g, n in genres.items() if g), key=lambda item: (-item[1], item[0]))
    genre_bars = [bar(labels[g], n) for g, n in ranked[:8]]
    if len(ranked) > 8:
        genre_bars.append(bar("Other genres", sum(n for _, n in ranked[8:])))
    if missing_genre:
        genre_bars.append(bar("No genre", missing_genre, True))
    era_bars = [bar(era, n) for era, n in sorted(eras.items()) if era]
    if missing_year:
        era_bars.append(bar("No year", missing_year, True))
    return {"genres": genre_bars, "eras": era_bars, "without_metadata": without_metadata,
            "gaps": [bar("No genre", missing_genre), bar("No year", missing_year),
                     bar("No audio features", missing_audio)]}


class VisibleImpressions(BaseModel):
    kind: Literal["album", "artist", "track"]
    ids: list[str] = Field(max_length=100)
    view_id: UUID


def build(ctx) -> APIRouter:
    router = APIRouter()
    store, templates = ctx.store, ctx.templates

    @router.get("/discovery")
    def discovery(request: Request):
        model = Ctx.discovery_actions(ctx).page(ctx.now_fn())
        return templates.TemplateResponse(request, "discovery.html",
            {**model, 'track_summary': track_pool_summary(model['tracks'])})

    @router.post("/discovery/impressions", status_code=204)
    def discovery_impressions(visible: VisibleImpressions):
        Ctx.discovery_actions(ctx).impressions(visible.kind, visible.ids, str(visible.view_id), ctx.now_fn())
        return Response(status_code=204)

    @router.post("/discovery/add")
    def discovery_add(kind: str = Form(...), id: str = Form(...)):
        """Add a discovered item to your collection. Tracks Like into Liked Music (the container-free
        library add); on the next sync the track becomes a library track and the pool prune removes it.
        Returns an empty 200 so the row is swapped out client-side. Best-effort: no client -> 503."""
        result = Ctx.discovery_actions(ctx).acquire(kind, id)
        status = {'unavailable': 503, 'missing_video': 422, 'unconfirmed': 502}.get(result['status'], 200)
        return Response(result['message'], status_code=status)

    return router
