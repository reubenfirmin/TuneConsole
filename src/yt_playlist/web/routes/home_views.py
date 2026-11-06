"""Home's prepared read models and fragment rendering for its route groups.

Recommendation policy remains in rec.home_cards/home_prepared. This owner keeps
one prepared payload per request and supplies the same response metadata to the
board and tuning routes.
"""
from fastapi import Response

from yt_playlist.core.activity import WorkBudgetExceeded, WorkClosing
from yt_playlist.rec import artist_start, choice_evidence, home_cards, home_prepared, home_vibe, mix_tuning, onboarding, rec_params
from yt_playlist.rec.home_cards import _epoch
from yt_playlist.web.jobs import start_fresh_pool, start_library_pool
from yt_playlist.web.theme import genre_tint
from yt_playlist.web.routes.home_access import state as growth_state


class PoolJobs:
    def __init__(self, ctx):
        self.ctx = ctx

    def latest(self, kind, parameters):
        return self.ctx.jobs.latest(kind, parameters)

    def start(self, kind, parameters, excluded):
        return (start_fresh_pool(self.ctx, parameters) if kind == "fresh_pool"
                else start_library_pool(self.ctx, parameters, excluded))


class HomeViews:
    def __init__(self, ctx, *, store=None, writer=None, display_only=False):
        self.ctx = ctx
        self.store = ctx.store if store is None else store
        self.writer = self.store if writer is None else writer
        self.display_only = display_only
        self.templates = ctx.templates
        self.recommendations = home_cards.HomeCards(self.writer, PoolJobs(ctx), ctx.logger)

    def schedule(self):
        if self.ctx.rec_worker:
            self.ctx.rec_worker.trigger_home_refresh()

    def prepared(self, now, request=None):
        if request is not None and hasattr(request.state, "prepared_home"):
            return request.state.prepared_home
        payload = home_prepared.load(self.store, now, self.schedule, display_only=self.display_only)
        if request is not None:
            request.state.prepared_home = payload
        return payload

    def card_context(self, request, now, *, live=False, **options):
        store = self.store
        record = False
        if live:
            mix_tuning.expire(store, now)
            with store.read_view() as reader:
                payload = home_prepared.peek(reader, now, self.schedule)
                request.state.prepared_home = payload
                calculation = home_cards.HomeCards(reader, self.recommendations.jobs, self.ctx.logger,
                    genre_options=(payload or {}).get('genre_options'))
                result = calculation.cards(home_cards.CardsRequest(now, record=False, **options))
                tuning = home_prepared.tuning_key(reader, now)
            home_prepared.remember(store, payload, result, now, tuning=tuning)
            self.schedule()
        else:
            payload = self.prepared(now, request)
            record = (request.url.path in ("/", "/home/cards", "/home/refresh-cards")
                      and not getattr(self.writer, 'activity_paused', False)
                      and not getattr(self.writer, '_read_only', False))
            if record and request.url.path == "/":
                record = growth_state(self.ctx, request)['ready_formats']['mixes']
            result = home_prepared.cards(store, payload, now, self.recommendations, self.schedule,
                                        record=record, writer=self.writer)
        if not result.protos and not home_vibe.current(store, now):
            first = artist_start.first_set(store, now)
            if first:
                result.protos = [first]
                result.slots = [{'proto': first}]
                result.preparing = True
                result.choice_context = {'source': 'explicit_artist_choices',
                                         'artists': first['recipe']['artists']}
        context = vars(result).copy()
        vibe_request = self.ctx.vibe_requests.state() if self.ctx.vibe_requests else None
        context['vibe_request'] = vibe_request
        if vibe_request and vibe_request['phase'] in ('pending', 'waiting'):
            context['preparing'] = True
            context['preference_notice'] = None
        frozen_context = result.choice_context if result.choice_context is not None else (payload or {}).get('choice_context')
        if (live or record) and result.protos and frozen_context is not None:
            for proto in result.protos:
                if proto.get('experiment'):
                    proto['experiment']['ranker_controlled'] = False
            options = [{"id": p["experiment_id"], "position": i + 1,
                        "label": p["label"], "note": p["note"], "intent": p["lane"],
                        "genres": list(p.get('genres', [])),
                        "assignment": p.get("assignment")} for i, p in enumerate(result.protos)]
            try:
                oid = choice_evidence.present(self.writer, "mixes", options, now,
                    context=frozen_context, window=result.epoch, vibe=result.vibe or None)
                for proto in result.protos:
                    proto["recipe"] = {**proto.get("recipe", {}), "opportunity_id": oid,
                                       "option_id": proto["experiment_id"]}
            except Exception:
                self.ctx.logger.warning("Mix choice capture failed", exc_info=True)
        try:
            attempt = min(10, max(0, int(request.query_params.get("prepare_attempt", "0"))))
        except ValueError:
            attempt = 0
        waiting = result.preparing or (not result.protos and bool(result.slots))
        context["prepare_attempt"] = (min(10, attempt + 1) if not live and waiting
            and not (vibe_request and vibe_request['phase'] in ('pending', 'waiting')) else None)
        # Preparation can outlast the first minute after a restart. Keep checking
        # at a bounded rate until usable offers arrive instead of stranding the
        # board after ten requests.
        context["prepare_interval"] = min(30, 3 * 2 ** min(attempt, 4))
        context["genre_options"] = [option | {"tint": genre_tint(option["family"])}
                                    for option in result.genre_options]
        self.recommendations.audit(result.protos, now, request.url.path, reader=store)
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
        growth = growth_state(self.ctx, request)
        return {"fingerprint": home_prepared.fingerprint(store, self.prepared(now, request), now),
                "tuning_active": self.tuning_active(),
                "vibe_presets": home_vibe.PRESETS,
                "vibes_ready": growth['vibes_ready'], "vibe": home_vibe.current(store, now),
                "has_modes": bool(store.modes.list_summaries(active_only=True)),
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
            {**context, "regenerating": regenerating, "swap_oob": swap_oob, "status_oob": True})
        return self.tuning_response(response, now)

    def cards_safe(self, request, now, *, swap_oob=False):
        try:
            return self.cards_fragment(request, now, swap_oob=swap_oob)
        except (WorkBudgetExceeded, WorkClosing):
            # The app's read-recovery response preserves the mounted cards.
            raise
        except Exception:  # noqa: BLE001 - don't strand a fragment on a spinner
            self.ctx.logger.exception("home cards render failed")
            if request.url.path in ('/home/vibe', '/home/cards'):
                # Keep playable cards mounted; observation can retry its read.
                return Response(status_code=503)
            return self.templates.TemplateResponse(request, "_partials/mode_cards.html",
                                                   {"protos": [], "swap_oob": swap_oob, "status_oob": True})

    def tuning_fragment(self, request, now):
        store = self.store
        growth = growth_state(self.ctx, request)
        controls = self.templates.TemplateResponse(
            request, "_partials/taste_fingerprint.html",
            {"fingerprint": home_prepared.fingerprint(store, self.prepared(now), now),
             "tuning_active": self.tuning_active(),
             "vibe_presets": home_vibe.PRESETS,
             "vibes_ready": growth['vibes_ready'],
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
