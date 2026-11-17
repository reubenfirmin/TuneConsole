"""Home transport adapters and rendering of detached command results."""
from fastapi import Response

from yt_playlist.core.activity import WorkBudgetExceeded, WorkClosing
from yt_playlist.library.home_board_composition import board
from yt_playlist.rec.home_prepared import card_record_gate
from yt_playlist.web.jobs import start_fresh_pool, start_library_pool
from yt_playlist.web.routes.home_access import state as growth_state
from yt_playlist.web.theme import genre_tint


class PoolJobs:
    def __init__(self, ctx):
        self.ctx = ctx

    def latest(self, kind, parameters):
        return self.ctx.jobs.latest(kind, parameters)

    def start(self, kind, parameters, excluded):
        return (start_fresh_pool(self.ctx, parameters) if kind == 'fresh_pool'
                else start_library_pool(self.ctx, parameters, excluded))


def card_view(context):
    return {**context, 'genre_options': [option | {'tint': genre_tint(option['family'])}
                                        for option in context['genre_options']]}


class HomeViews:
    def __init__(self, ctx, *, store=None, writer=None, display_only=False):
        self.ctx = ctx
        self.store = ctx.store if store is None else store
        self.writer = self.store if writer is None else writer
        self.templates = ctx.templates
        self.commands, self.recommendations = board(self.store, self.writer, jobs=PoolJobs(ctx),
            schedule=self.schedule, vibe_requests=ctx.vibe_requests, worker=ctx.rec_worker,
            logger=ctx.logger, display_only=display_only)

    def schedule(self):
        if self.ctx.rec_worker:
            self.ctx.rec_worker.trigger_home_refresh()

    def prepared(self, now, request=None):
        if request is not None and hasattr(request.state, 'prepared_home'):
            return request.state.prepared_home
        payload = self.commands.load(now)
        if request is not None:
            request.state.prepared_home = payload
        return payload

    def _options(self, request):
        try:
            attempt = int(request.query_params.get('prepare_attempt', '0'))
        except ValueError:
            attempt = 0
        return {'path': request.url.path, 'attempt': attempt,
                'record': card_record_gate(request.url.path,
                    activity_paused=getattr(self.writer, 'activity_paused', False),
                    read_only=getattr(self.writer, '_read_only', False),
                    mixes_ready=lambda: growth_state(self.ctx, request)['ready_formats']['mixes'])}

    def card_context(self, request, now, *, live=False, **options):
        payload = None if live else self.prepared(now, request)
        return card_view(self.commands.query(payload, now, live=live,
                                            **self._options(request), **options))

    def tuning_active(self):
        return self.commands.tuning()

    def tuning_response(self, response, now):
        response.headers['X-Mix-Tuning-Remaining'] = str(self.commands.remaining(now))
        response.headers['X-Mix-Tuning-Active'] = str(self.commands.tuning()).lower()
        return response

    def feed_context(self, request, now):
        return card_view(self.commands.feed(self.prepared(now, request), now,
            vibes_ready=growth_state(self.ctx, request)['vibes_ready'], **self._options(request)))

    def discovery_context(self, now, request=None):
        return self.commands.discovery_query(self.prepared(now, request), now)

    def queue_artist_images(self, selection, now):
        self.commands.artist_images(selection, now)

    def busy(self):
        return self.commands.busy()

    def cards_fragment(self, request, now, *, regenerating=False, use_genre_request=True,
                       genre_notice=None, swap_oob=False):
        live = request.method == 'POST' and request.url.path != '/home/refresh-cards'
        context = self.card_context(request, now, live=live, regenerating=regenerating,
                                    use_genre_request=use_genre_request, genre_notice=genre_notice)
        response = self.templates.TemplateResponse(request, '_partials/mode_cards.html',
            {**context, 'regenerating': regenerating, 'swap_oob': swap_oob, 'status_oob': True})
        return self.tuning_response(response, now)

    def cards_safe(self, request, now, *, swap_oob=False):
        try:
            return self.cards_fragment(request, now, swap_oob=swap_oob)
        except (WorkBudgetExceeded, WorkClosing):
            raise
        except Exception:
            self.ctx.logger.exception('home cards render failed')
            if request.url.path in ('/home/vibe', '/home/cards'):
                return Response(status_code=503)
            return self.templates.TemplateResponse(request, '_partials/mode_cards.html',
                {'protos': [], 'swap_oob': swap_oob, 'status_oob': True})

    def tuning_fragment(self, request, now):
        context = self.commands.controls(self.prepared(now), now,
            vibes_ready=growth_state(self.ctx, request)['vibes_ready'])
        cards = self.cards_safe(request, now, swap_oob=True)
        controls = self.templates.TemplateResponse(request, '_partials/taste_fingerprint.html', context)
        return self.tuning_response(Response(controls.body + cards.body, media_type='text/html'), now)

    def pool_fragment(self, request, job):
        now = self.ctx.now_fn()
        result = self.commands.pool_query(job, self.prepared(now), now, request.url.path)
        if result.proto:
            return self.templates.TemplateResponse(request, '_partials/generated_playlist.html', {'p': result.proto})
        return self.templates.TemplateResponse(request, '_partials/pool_card.html', {'pool': result.pool})
