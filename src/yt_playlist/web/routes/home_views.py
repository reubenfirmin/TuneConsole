"""Home transport adapters and rendering of detached command results."""
from fastapi import Response

from yt_playlist.core.activity import WorkBudgetExceeded, WorkClosing, check_stopping
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
                                        for option in context.get('genre_options', [])]}


def prepare_attempt(request):
    try:
        return min(10, max(0, int(request.query_params.get('prepare_attempt', '0'))))
    except ValueError:
        return 0


def after_response(ctx, label, operation, args):
    try:
        if not ctx.store.activity_paused and not getattr(ctx.store, '_evidence_maintenance', 0):
            with ctx.store.priority.background(check_stopping):
                ctx.work.wrap('Home: ' + label, operation, *args)()
    except WorkClosing:
        return
    except Exception:
        ctx.logger.exception('Home background work failed: %s', label)


class HomeViews:
    def __init__(self, ctx, *, store=None, writer=None):
        self.ctx = ctx
        self.store = ctx.store if store is None else store
        self.writer = self.store if writer is None else writer
        self.templates = ctx.templates
        self.commands, self.recommendations = board(self.store, self.writer, jobs=PoolJobs(ctx),
            worker=ctx.rec_worker, logger=ctx.logger)

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
        return {'path': request.url.path, 'attempt': prepare_attempt(request),
                'record': card_record_gate(request.url.path,
                    activity_paused=getattr(self.writer, 'activity_paused', False),
                    read_only=getattr(self.writer, '_read_only', False),
                    mixes_ready=lambda: growth_state(self.ctx, request)['ready_formats']['mixes'])}

    def card_context(self, request, now, *, changed=False, **options):
        return card_view(self.commands.query(self.prepared(now, request), now, changed=changed,
                                            **self._options(request), **options))

    def tuning_active(self):
        return self.commands.tuning()

    def tuning_response(self, response, now, *, remaining=None, active=None):
        response.headers['X-Mix-Tuning-Remaining'] = str(self.commands.remaining(now) if remaining is None else remaining)
        response.headers['X-Mix-Tuning-Active'] = str(self.commands.tuning() if active is None else active).lower()
        return response

    def feed_context(self, request, now):
        return card_view(self.commands.feed(self.prepared(now, request), now,
            vibes_ready=growth_state(self.ctx, request)['vibes_ready'], **self._options(request)))

    def discovery_context(self, now, request=None):
        return self.commands.discovery_query(lambda: self.prepared(now, request), now)

    def queue_artist_images(self, selection, now):
        self.commands.artist_images(selection, now)

    def busy(self):
        return self.commands.busy()

    def cards_fragment(self, request, now, *, regenerating=False, use_genre_request=True,
                       genre_notice=None, swap_oob=False, remaining=None):
        changed = request.method == 'POST' and request.url.path != '/home/refresh-cards'
        context = self.card_context(request, now, changed=changed, regenerating=regenerating,
                                    use_genre_request=use_genre_request, genre_notice=genre_notice)
        if request.url.path == '/home/vibe' and not context['protos']:
            return Response(status_code=503)
        response = self.templates.TemplateResponse(request, '_partials/mode_cards.html',
            {**context, 'regenerating': regenerating, 'swap_oob': swap_oob, 'status_oob': True})
        return self.tuning_response(response, now, remaining=remaining,
                                    active=True if remaining is not None and context['vibe'] else None)

    def cards_safe(self, request, now, *, swap_oob=False, remaining=None):
        try:
            return self.cards_fragment(request, now, swap_oob=swap_oob, remaining=remaining)
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

    def tuning_dialog(self, request, now):
        with self.store.read_view() as reader:
            views = HomeViews(self.ctx, store=reader)
            context = views.commands.controls(views.prepared(now), now, vibes_ready=None)
            return self.templates.TemplateResponse(request, '_partials/taste_fingerprint.html',
                {**context, 'dialog_only': True})

    def pool_fragment(self, request, job):
        now = self.ctx.now_fn()
        result = self.commands.pool(job, now, request.url.path)
        if result.proto:
            return self.templates.TemplateResponse(request, '_partials/generated_playlist.html', {'p': result.proto})
        return self.templates.TemplateResponse(request, '_partials/pool_card.html', {'pool': result.pool})
