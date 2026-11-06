"""Prepared discovery/story reads and explicit visible-story measurements."""
from fastapi import APIRouter, Depends, Request, Response
from pydantic import BaseModel, Field

from yt_playlist.rec import account_growth, album_choices, album_notes, album_tuning, choice_evidence, home_prepared, into_recently
from yt_playlist.rec.home_cards import _epoch
from yt_playlist.web.context import request_form
from yt_playlist.web.routes.home_access import guard, state as growth_state


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


def story_context(store, now, schedule=lambda: None):
    cards, preparing = into_recently.load(store, now, epoch=_epoch(store, "into_recently"),
                                         schedule=schedule)
    groups = into_recently.story_groups(cards)
    return {"cards": cards, "groups": groups, "spreads": into_recently.editorial_spreads(groups),
            "preparing": preparing}


def story_fragment(ctx, request):
    """Render the prepared story pool after the caller establishes access."""
    schedule = ctx.rec_worker.trigger_story_refresh if ctx.rec_worker else lambda: None
    context = story_context(ctx.store, ctx.now_fn(), schedule)
    cards, preparing = context['cards'], context['preparing']
    if cards or preparing:
        return ctx.templates.TemplateResponse(request, "_partials/into_recently.html", context)
    return Response(status_code=200)


def album_context(ctx, views, now):
    """Compose one prepared album menu for both Home and its existing fragment."""
    store, writer = views.store, views.writer
    prepared = album_choices.load(store, now, views.schedule)
    payload = prepared or {}
    playing = ctx.bridge.now_playing or {}
    current = playing.get("album_browse_id")
    if not current and playing.get("playlist_ytm_id"):
        source = store.collection.playback_source(playing["playlist_ytm_id"], playing.get("video_id"))
        current = source.get("browse") if source and source["kind"] == "album" else None
    epoch = _epoch(store, "album_choices")
    comparison = payload.get('context', {}).get('comparison')
    if comparison and not getattr(writer, '_read_only', False) and not writer.activity_paused:
        held = {'epoch': epoch, 'comparison': comparison}
        if writer.rec_proposals.get_proposals('model_album_assignment') != held:
            writer.rec_proposals.put_proposals('model_album_assignment', held, now)
    tuning = album_tuning.state(store, now)
    refresh = store.rec_proposals.get_proposals('album_menu_refresh') or {}
    excluded = refresh.get('exclude', []) if refresh.get('epoch') == epoch else []
    pool, items, policy = album_choices.selection(store, payload.get('items', []), now,
        tuning['values'], epoch, tuning['genres'], exclude=excluded, current=current)
    policy['temporary_until'] = now + tuning['remaining'] if tuning['remaining'] else None
    policy['standing_defaults'] = album_tuning.defaults(store)
    options = [{**a, "id": a["browse_id"], "position": i + 1} for i, a in enumerate(items)]
    oid = None
    if options:
        try:
            oid = choice_evidence.present(writer, "albums", options, now,
                context={**payload.get("context", {}), 'album_menu': policy}, window=epoch)
        except Exception:
            ctx.logger.warning("Album choice capture failed", exc_info=True)
    writer.rec_proposals.request_album_notes(items, now)
    queued_notes = writer.rec_proposals.get_proposals('album_note_requests') or {}
    if ctx.rec_worker and queued_notes:
        views.schedule()
    listening = store.collection.album_listening_summary(album['browse_id'] for album in items)
    items = [{**album, 'notes': album_notes.read(store, album),
              'familiarity': _familiarity(listening[album['browse_id']]),
              'notes_pending': album['browse_id'] in queued_notes and ctx.rec_worker is not None}
             for album in items]
    genres = sorted({axis[0] for axis in store.genres.feature_axes(album.get('genre') for album in pool).values()} - {''})
    genres = sorted(set(genres) | set(tuning['genres']))
    return {"albums": items, "opportunity_id": oid, "preparing": prepared is None,
            "filtered": bool(tuning['genres']), "tuning": tuning, "tuning_axes": album_tuning.AXES,
            "tuning_defaults": album_tuning.defaults(store),
            "genres": [{'key': genre, 'label': genre.removeprefix('other:').title()} for genre in genres]}


def build(ctx, views):
    router = APIRouter()
    store, now_fn, templates = ctx.store, ctx.now_fn, ctx.templates

    @router.post("/home/format", status_code=204)
    def home_format(request: Request, form=Depends(request_form)):
        preference, panel = form.get("format"), form.get("panel")
        if preference is not None:
            if preference not in {"auto", "albums", "mixes", "radio", "both"}:
                return Response(status_code=400)
            if not all(growth_state(ctx, request)['ready_formats'].values()):
                return Response('Wait until all three listening formats are ready.', status_code=409)
            store.settings.set_setting("home_format", preference)
        if panel in {"album-choices", "directions", "radio"}:
            # Match fragment access: established listening retains its journey
            # prefix without scanning every current Home collection again.
            if (not account_growth.retained_access(store, panel)
                    and not account_growth.has_access(growth_state(ctx, request), panel)):
                return Response(status_code=409)
            store.settings.set_setting("home_last_panel", panel)
        return Response(status_code=204)

    @router.get("/home/album-choices", dependencies=[Depends(guard(ctx, 'album-choices'))])
    def home_album_choices(request: Request):
        return templates.TemplateResponse(request, "_partials/album_choices.html",
                                          album_context(ctx, views, now_fn()))

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
            _, items, _ = album_choices.selection(store, payload.get('items', []), now,
                tuning['values'], epoch, tuning['genres'], exclude=excluded)
            ids = [item['browse_id'] for item in items]
        store.rec_ledger.refresh_card('album_choices', 1, now)
        store.rec_proposals.put_proposals('album_menu_refresh',
            {'epoch': _epoch(store, 'album_choices'), 'exclude': ids}, now)

    @router.post("/home/album-choices/refresh", dependencies=[Depends(guard(ctx, 'album-choices'))])
    def refresh_album_choices(request: Request, form=Depends(request_form)):
        rotate_album_choices(form.get('opportunity_id', ''))
        return home_album_choices(request)

    @router.post('/home/album-choices/tune', dependencies=[Depends(guard(ctx, 'album-choices'))])
    def tune_album_choices(request: Request, form=Depends(request_form)):
        try:
            if form.get('reset') == '1':
                album_tuning.reset(store)
            else:
                values = {axis['key']: float(form[axis['key']]) / 100 for axis in album_tuning.AXES}
                genres = form.getlist('genre')
                known = set(store.genres.taxonomy()['families'])
                payload = album_choices.load(store, now_fn()) or {}
                known.update(axis[0] for axis in store.genres.feature_axes(
                    album.get('genre') for album in payload.get('items', [])).values())
                if any(genre not in known for genre in genres):
                    raise ValueError('Unknown album genre')
                album_tuning.apply(store, now_fn(), values, genres)
        except (KeyError, ValueError, TypeError):
            return Response('Choose valid album controls', status_code=422)
        rotate_album_choices(form.get('opportunity_id', ''))
        return home_album_choices(request)

    @router.get('/home/album-notes', dependencies=[Depends(guard(ctx, 'album-choices'))])
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

    @router.get("/home/into-recently", dependencies=[Depends(guard(ctx, 'recent'))])
    def home_into_recently(request: Request):
        """Read worker-prepared stories; the request only rotates the already decorated pool."""
        return story_fragment(ctx, request)

    @router.post("/home/into-recently/impressions", status_code=204, dependencies=[Depends(guard(ctx, 'recent'))])
    def story_impressions(visible: StoryImpressions):
        payload = store.rec_proposals.get_proposals(into_recently.SURFACE) or {}
        known = {card['subject'] for card in payload.get('cards', [])}
        store.rec_ledger.record_story_views(known.intersection(visible.subjects), now_fn())
        return Response(status_code=204)

    @router.get("/home/discovery", dependencies=[Depends(guard(ctx, 'discover'))])
    def home_discovery(request: Request):
        return templates.TemplateResponse(request, "_partials/home_discovery.html",
                                          views.discovery_context(now_fn()))

    @router.get('/home/revisit', dependencies=[Depends(guard(ctx, 'rediscover'))])
    def home_revisit(request: Request):
        now = now_fn()
        return templates.TemplateResponse(request, '_partials/rediscover.html',
            home_prepared.collections(store, views.prepared(now), now))

    @router.get("/home/discover", dependencies=[Depends(guard(ctx, 'discover'))])
    def home_discover(request: Request):
        # From the accumulating discovery pool: recency-biased, repeat-aware, filled in by the
        # background scan of every artist you're interested in (not a top-10 overwrite).
        now = now_fn()
        albums = home_prepared.discovery(store, views.prepared(now), now)["discovery_albums"]
        return templates.TemplateResponse(request, "_partials/discover.html",
                                          {"albums": albums,
                                           "building": not albums and views.busy(),
                                           "stale": bool(albums) and views.busy()})

    @router.get("/home/new-artists", dependencies=[Depends(guard(ctx, 'discover'))])
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

