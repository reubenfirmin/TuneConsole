"""Home's prepared read models and fragment rendering for its route groups.

Recommendation policy remains in rec.home_cards/home_prepared. This owner keeps
one prepared payload per request and supplies the same response metadata to the
board and tuning routes.
"""
from fastapi import Response

from yt_playlist.rec import home_cards, home_prepared, home_vibe, mix_tuning, onboarding, rec_params
from yt_playlist.rec.home_cards import _epoch
from yt_playlist.web.jobs import start_fresh_pool, start_library_pool
from yt_playlist.web.theme import genre_tint


class PoolJobs:
    def __init__(self, ctx):
        self.ctx = ctx

    def latest(self, kind, parameters):
        return self.ctx.jobs.latest(kind, parameters)

    def start(self, kind, parameters, excluded):
        return (start_fresh_pool(self.ctx, parameters) if kind == "fresh_pool"
                else start_library_pool(self.ctx, parameters, excluded))


class HomeViews:
    def __init__(self, ctx):
        self.ctx = ctx
        self.store = ctx.store
        self.templates = ctx.templates
        self.recommendations = home_cards.HomeCards(ctx.store, PoolJobs(ctx), ctx.logger)

    def schedule(self):
        if self.ctx.rec_worker:
            self.ctx.rec_worker.trigger_home_refresh()

    def prepared(self, now, request=None):
        if request is not None and hasattr(request.state, "prepared_home"):
            return request.state.prepared_home
        payload = home_prepared.load(self.store, now, self.schedule)
        if request is not None:
            request.state.prepared_home = payload
        return payload

    def card_context(self, request, now, *, live=False, **options):
        store = self.store
        payload = self.prepared(now, request)
        if live:
            result = self.recommendations.cards(home_cards.CardsRequest(now, record=False, **options))
            home_prepared.remember(store, payload, result, now)
            self.schedule()
        else:
            record = request.url.path in ("/", "/home/cards", "/home/refresh-cards")
            if request.url.path == "/":
                record = (onboarding._has_synced(store)
                          and not onboarding.onboarding_active(store, now))
            result = home_prepared.cards(store, payload, now, self.recommendations, self.schedule,
                                        record=record)
        context = vars(result).copy()
        try:
            attempt = max(0, int(request.query_params.get("prepare_attempt", "0")))
        except ValueError:
            attempt = 0
        context["prepare_attempt"] = attempt + 1 if not live and payload is None and attempt < 10 else None
        context["genre_options"] = [option | {"tint": genre_tint(option["family"])}
                                    for option in result.genre_options]
        self.recommendations.audit(result.protos, now, request.url.path)
        return context

    def tuning_active(self):
        store = self.store
        genres = store.rec_proposals.get_proposals("home_genre_request") or {}
        return (bool(store.rec_model.get_leans()) or bool(store.rec_model.hidden_facets())
                or bool(store.settings.get_setting(home_vibe.SETTING))
                or rec_params.get_param(store, "breadth_bias") != 0
                or (genres.get("epoch") == _epoch(store, "cards")
                    and bool(genres.get("include_genres") or genres.get("avoid_genres"))))

    def tuning_response(self, response, now):
        response.headers["X-Mix-Tuning-Remaining"] = str(mix_tuning.remaining(self.store, now))
        response.headers["X-Mix-Tuning-Active"] = str(self.tuning_active()).lower()
        return response

    def feed_context(self, request, now):
        store = self.store
        return {"fingerprint": home_prepared.fingerprint(store, self.prepared(now, request), now),
                "tuning_active": self.tuning_active(),
                "vibe_presets": home_vibe.PRESETS, "vibe": home_vibe.current(store, now),
                "has_modes": bool(store.modes.list_modes(active_only=True)),
                **self.card_context(request, now)}

    def discovery_context(self, now, request=None):
        selection = home_prepared.discovery(self.store, self.prepared(now, request), now)
        self.queue_artist_images(selection, now)
        return {**selection, "discovery_busy": self.busy()}

    def queue_artist_images(self, selection, now):
        ids = [a['artist_id'] for a in selection.get('discovery_artists', []) if not a.get('thumbnail')]
        if self.ctx.rec_worker and ids:
            self.ctx.rec_worker.trigger_artist_images(
                [a['artist_id'] for a in self.store.discovery.artist_images_due(ids, now)])

    def busy(self):
        return bool(self.ctx.rec_worker and self.ctx.rec_worker.discovery_busy)

    def cards_fragment(self, request, now, *, regenerating=False, use_genre_request=True,
                       genre_notice=None, swap_oob=False):
        live = request.method == "POST" and request.url.path != "/home/refresh-cards"
        context = self.card_context(request, now, live=live, regenerating=regenerating,
                                    use_genre_request=use_genre_request, genre_notice=genre_notice)
        response = self.templates.TemplateResponse(
            request, "_partials/mode_cards.html",
            {**context, "regenerating": regenerating, "swap_oob": swap_oob})
        return self.tuning_response(response, now)

    def cards_safe(self, request, now, *, swap_oob=False):
        try:
            return self.cards_fragment(request, now, swap_oob=swap_oob)
        except Exception:  # noqa: BLE001 - don't strand a fragment on a spinner
            self.ctx.logger.exception("home cards render failed")
            return self.templates.TemplateResponse(request, "_partials/mode_cards.html",
                                                   {"protos": [], "swap_oob": swap_oob})

    def tuning_fragment(self, request, now):
        store = self.store
        controls = self.templates.TemplateResponse(
            request, "_partials/taste_fingerprint.html",
            {"fingerprint": home_prepared.fingerprint(store, self.prepared(now), now),
             "tuning_active": self.tuning_active(), "vibe_presets": home_vibe.PRESETS,
             "vibe": home_vibe.current(store, now)})
        cards = self.cards_safe(request, now, swap_oob=True)
        return self.tuning_response(Response(controls.body + cards.body, media_type="text/html"), now)

    def pool_fragment(self, request, job):
        now = self.ctx.now_fn()
        result = self.recommendations.pool(job, now, request.url.path)
        if result.proto:
            payload = self.prepared(now)
            menu = home_prepared.cards(self.store, payload, now, self.recommendations,
                                       self.schedule, record=False)
            lane = result.proto["lane"]
            menu.protos = [p for p in menu.protos if p["lane"] != lane] + [result.proto]
            slots = [{"proto": result.proto, "pool": None} if
                     (s["proto"]["lane"] if s["proto"] else s["pool"]["lane"]) == lane else s
                     for s in menu.slots]
            menu.slots = slots or [{"proto": result.proto, "pool": None}]
            home_prepared.remember(self.store, payload, menu, now)
            return self.templates.TemplateResponse(request, "_partials/generated_playlist.html",
                                                   {"p": result.proto})
        return self.templates.TemplateResponse(request, "_partials/pool_card.html", {"pool": result.pool})
