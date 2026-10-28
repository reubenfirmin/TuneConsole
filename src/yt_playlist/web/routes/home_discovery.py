"""Prepared discovery/story reads and explicit visible-story measurements."""
from fastapi import APIRouter, Depends, Request, Response
from pydantic import BaseModel, Field

from yt_playlist.rec import album_choices, album_notes, album_tuning, choice_evidence, home_prepared, into_recently
from yt_playlist.rec.home_cards import _epoch
from yt_playlist.util import genre_map
from yt_playlist.web.context import request_form


class StoryImpressions(BaseModel):
    subjects: list[str] = Field(max_length=into_recently.MAX_STORIES)


class ChoiceResponse(BaseModel):
    opportunity_id: str = Field(max_length=64)
    option_id: str = Field(max_length=512)
    kind: str = Field(max_length=20)


def _familiarity(summary):
    if not summary['complete'] or not summary['tracks']:
        return {'level': 'partial' if summary['plays'] else 'unknown',
                'label': 'Some listening recorded; album coverage is incomplete.' if summary['plays']
                         else 'Listening history unavailable for this album.'}
    plays = summary['plays']
    average = plays / summary['tracks']
    level, label = ('unplayed', 'No recorded track plays.') if not plays else (
        ('light', 'Lightly played.') if average < 1 else
        ('familiar', 'Familiar.') if average < 3 else ('frequent', 'Frequently played.'))
    return {'level': level, 'label': label}


def build(ctx, views):
    router = APIRouter()
    store, now_fn, templates = ctx.store, ctx.now_fn, ctx.templates

    @router.post("/home/format", status_code=204)
    async def home_format(request: Request):
        form = await request.form()
        preference, panel = form.get("format"), form.get("panel")
        if preference is not None:
            if preference not in {"auto", "albums", "mixes", "radio", "both"}:
                return Response(status_code=400)
            store.settings.set_setting("home_format", preference)
        if panel in {"album-choices", "directions", "radio"}:
            store.settings.set_setting("home_last_panel", panel)
        return Response(status_code=204)

    @router.get("/home/album-choices")
    def home_album_choices(request: Request):
        now = now_fn()
        prepared = album_choices.load(store, now, views.schedule)
        payload = prepared or {}
        pool = album_choices.eligible(store, payload.get("items", []), now)
        playing = ctx.bridge.now_playing or {}
        current = playing.get("album_browse_id")
        if not current and playing.get("playlist_ytm_id"):
            source = store.collection.playback_source(playing["playlist_ytm_id"], playing.get("video_id"))
            current = source.get("browse") if source and source["kind"] == "album" else None
        pool = [album for album in pool if album["browse_id"] != current]
        epoch = _epoch(store, "album_choices")
        tuning = album_tuning.state(store, now)
        refresh = store.rec_proposals.get_proposals('album_menu_refresh') or {}
        excluded = refresh.get('exclude', []) if refresh.get('epoch') == epoch else []
        items, policy = album_choices.menu(pool, tuning['values'], epoch, tuning['genres'], exclude=excluded)
        policy['temporary_until'] = now + tuning['remaining'] if tuning['remaining'] else None
        policy['standing_defaults'] = album_tuning.defaults(store)
        options = [{**a, "id": a["browse_id"], "position": i + 1} for i, a in enumerate(items)]
        oid = None
        if options:
            try:
                oid = choice_evidence.present(store, "albums", options, now,
                    context={**payload.get("context", {}), 'album_menu': policy}, window=epoch)
            except Exception:
                ctx.logger.warning("Album choice capture failed", exc_info=True)
        store.rec_proposals.request_album_notes(items, now)
        if ctx.rec_worker and store.rec_proposals.get_proposals('album_note_requests'):
            views.schedule()
        queued_notes = store.rec_proposals.get_proposals('album_note_requests') or {}
        listening = store.collection.album_listening_summary(album['browse_id'] for album in items)
        items = [{**album, 'notes': album_notes.read(store, album),
                  'familiarity': _familiarity(listening[album['browse_id']]),
                  'notes_pending': album['browse_id'] in queued_notes and ctx.rec_worker is not None}
                 for album in items]
        genres = sorted({genre_map.family(album.get('genre')) for album in pool} - {''})
        genres = sorted(set(genres) | set(tuning['genres']))
        return templates.TemplateResponse(request, "_partials/album_choices.html", {
            "albums": items, "opportunity_id": oid, "preparing": prepared is None,
            "filtered": bool(tuning['genres']), "tuning": tuning, "tuning_axes": album_tuning.AXES,
            "tuning_defaults": album_tuning.defaults(store),
            "genres": [{'key': genre, 'label': genre.removeprefix('other:').title()} for genre in genres]})

    def rotate_album_choices(previous=''):
        now = now_fn()
        held = choice_evidence.close_window(store, 'albums', now, opportunity_id=previous)
        if held:
            ids = [item['browse_id'] for item in held['options']]
        else:
            payload = album_choices.load(store, now) or {}
            tuning = album_tuning.state(store, now)
            epoch = _epoch(store, 'album_choices')
            refresh = store.rec_proposals.get_proposals('album_menu_refresh') or {}
            excluded = refresh.get('exclude', []) if refresh.get('epoch') == epoch else []
            items, _ = album_choices.menu(album_choices.eligible(store, payload.get('items', []), now),
                tuning['values'], epoch, tuning['genres'], exclude=excluded)
            ids = [item['browse_id'] for item in items]
        store.rec_ledger.refresh_card('album_choices', 1, now)
        store.rec_proposals.put_proposals('album_menu_refresh',
            {'epoch': _epoch(store, 'album_choices'), 'exclude': ids}, now)

    @router.post("/home/album-choices/refresh")
    def refresh_album_choices(request: Request, form=Depends(request_form)):
        rotate_album_choices(form.get('opportunity_id', ''))
        return home_album_choices(request)

    @router.post('/home/album-choices/tune')
    def tune_album_choices(request: Request, form=Depends(request_form)):
        try:
            if form.get('reset') == '1':
                album_tuning.reset(store)
            else:
                values = {axis['key']: float(form[axis['key']]) / 100 for axis in album_tuning.AXES}
                genres = form.getlist('genre')
                known = set(genre_map.all_families())
                payload = album_choices.load(store, now_fn()) or {}
                known.update(genre_map.family(album.get('genre')) for album in payload.get('items', []))
                if any(genre not in known for genre in genres):
                    raise ValueError('Unknown album genre')
                album_tuning.apply(store, now_fn(), values, genres)
        except (KeyError, ValueError, TypeError):
            return Response('Choose valid album controls', status_code=422)
        rotate_album_choices(form.get('opportunity_id', ''))
        return home_album_choices(request)

    @router.get('/home/album-notes')
    def home_album_notes(request: Request, browse: str, opportunity: str = '', attempt: int = 0):
        prepared = album_choices.load(store, now_fn()) or {}
        albums = prepared.get('items', [])
        if opportunity and len(opportunity) <= 64:
            held = choice_evidence.opportunity(store, opportunity)
            if held and held.get('format') == 'albums':
                albums = held.get('options', []) + albums
        album = next((row for row in albums if row.get('browse_id') == browse), None)
        if album is None:
            return Response(status_code=404)
        store.rec_proposals.request_album_notes([album], now_fn(), visible=True)
        if ctx.rec_worker:
            views.schedule()
        pending = browse in (store.rec_proposals.get_proposals('album_note_requests') or {})
        return templates.TemplateResponse(request, '_partials/album_notes.html', {
            'a': album, 'notes': album_notes.read(store, album), 'opportunity_id': opportunity,
            'notes_scene': True,
            'notes_pending': pending and ctx.rec_worker is not None and attempt < 80,
            'notes_attempt': max(0, attempt) + 1})

    @router.post("/home/listening-response", status_code=204)
    def listening_response(event: ChoiceResponse):
        # Playback and saves are accepted only from their confirmed server owners.
        if event.kind not in {"visible", "open", "choice"}:
            return Response(status_code=400)
        now = now_fn()
        choice_evidence.safe_respond(store, event.opportunity_id, event.kind, now,
            option_id=event.option_id, visible=[event.option_id] if event.kind == "visible" else ())
        if event.kind == "choice" and ctx.rec_worker:
            ctx.rec_worker.trigger_home_refresh()
        return Response(status_code=204)

    @router.get("/home/into-recently")
    def home_into_recently(request: Request):
        """Read worker-prepared stories; the request only rotates the already decorated pool."""
        now = now_fn()
        schedule = ctx.rec_worker.trigger_story_refresh if ctx.rec_worker else lambda: None
        cards, preparing = into_recently.load(store, now, epoch=_epoch(store, "into_recently"), schedule=schedule)
        if cards or preparing:
            groups = into_recently.story_groups(cards)
            return templates.TemplateResponse(request, "_partials/into_recently.html",
                                              {"cards": cards, "groups": groups,
                                               "spreads": into_recently.editorial_spreads(groups),
                                               "preparing": preparing})
        return Response(status_code=200)

    @router.post("/home/into-recently/impressions", status_code=204)
    def story_impressions(visible: StoryImpressions):
        payload = store.rec_proposals.get_proposals(into_recently.SURFACE) or {}
        known = {card['subject'] for card in payload.get('cards', [])}
        store.rec_ledger.record_story_views(known.intersection(visible.subjects), now_fn())
        return Response(status_code=204)

    @router.get("/home/discovery")
    def home_discovery(request: Request):
        return templates.TemplateResponse(request, "_partials/home_discovery.html",
                                          views.discovery_context(now_fn()))

    @router.get("/home/discover")
    def home_discover(request: Request):
        # From the accumulating discovery pool: recency-biased, repeat-aware, filled in by the
        # background scan of every artist you're interested in (not a top-10 overwrite).
        now = now_fn()
        albums = home_prepared.discovery(store, views.prepared(now), now)["discovery_albums"]
        return templates.TemplateResponse(request, "_partials/discover.html",
                                          {"albums": albums,
                                           "building": not albums and views.busy(),
                                           "stale": bool(albums) and views.busy()})

    @router.get("/home/new-artists")
    def home_new_artists(request: Request):
        # From the accumulating new-artist pool: best taste-fit first, de-prioritizing recently-shown.
        now = now_fn()
        artists = home_prepared.discovery(store, views.prepared(now), now)["discovery_artists"]
        views.queue_artist_images({'discovery_artists': artists}, now)
        return templates.TemplateResponse(request, "_partials/new_artists.html",
                                          {"artists": artists,
                                           "building": not artists and views.busy(),
                                           "stale": bool(artists) and views.busy()})

    return router

