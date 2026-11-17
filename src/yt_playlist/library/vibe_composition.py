"""Bind durable Vibe work to activity, coherent readers and publication owners."""
import json
from contextlib import contextmanager
from types import SimpleNamespace

from yt_playlist.controllers.vibe_preparation import VibePreparation
from yt_playlist.controllers.vibe_prediction import configured
from yt_playlist.core.activity import activity_for, check_cancellable_work
from yt_playlist.core.db_writes import atomic_write
from yt_playlist.rec import home_cache, home_cards, home_prepared, home_vibe, mix_tuning
from yt_playlist.rec.vibe_requests import VibeRequests


def recording_preparation(reader):
    from yt_playlist.controllers.vibe_recording_preparation import RecordingPreparation
    from yt_playlist.rec import musical_preparation, vibe_recording_rules as vibe_preparation, vibe_joint_state as joint

    def empty(vibe):
        state = joint.state(reader, vibe)
        return joint.current(reader, state) and state.get('outcome') == 'empty'

    return RecordingPreparation(reader.vibe_predictions, reader.musical_profiles, reader.genres,
        prediction=lambda: prediction().capture(), version=vibe_preparation.version, qualified=joint.qualified,
        masked=lambda rows: musical_preparation.masked_recordings(
            rows, inputs=musical_preparation.load_masked_inputs(reader, rows)),
        posterior=lambda vibe: joint.posterior(reader, vibe), current_empty=empty)


def joint_preparation(store):
    from yt_playlist.controllers.vibe_joint_preparation import JointPreparation
    from yt_playlist.rec import vibe_joint_state as preparation, vibe_joint_model as model

    @contextmanager
    def snapshot():
        with store.read_snapshot() as captured:
            reader = captured.store
            yield SimpleNamespace(settings=reader.settings, predictions=reader.vibe_predictions,
                priors=lambda rows, vibe: preparation.prior_inputs(reader, rows, vibe),
                refresh=lambda vibe: preparation._refresh_ready(reader, vibe), publish=captured.publish_ordered)

    return JointPreparation(store.settings, store.vibe_predictions, snapshot=snapshot,
        state=lambda vibe: preparation.state(store, vibe), qualified=preparation.qualified,
        configuration=preparation.config_identity, refresh=lambda vibe: preparation._refresh_ready(store, vibe),
        fit=model.fit, now=preparation.time.time, monotonic=preparation.time.monotonic,
        vibes=preparation.VIBES, max_rows=model.MAX_ROWS)


def prediction():
    return configured()


def requests(ctx):
    store = ctx.store

    def capture(reader, request, now):
        with home_vibe.authorized(reader, request['vibe']), home_cache.verified_reads(reader):
            raw = reader.settings.get_setting(mix_tuning.VIBE_REQUEST_SETTING)
            if (json.loads(raw) if raw else None) != request:
                return None
            payload = home_prepared.selection_context(reader, now)
            previous = home_prepared.peek(reader, now, readiness_only=True)
            previous_size = max((len(menu['protos']) for menu in (previous or {}).get('menus', [])), default=0)
            calculation = home_cards.HomeCards(reader, home_prepared._NoJobs(), ctx.logger,
                genre_options=(payload or {}).get('genre_options'))
            result = calculation.cards(home_cards.CardsRequest(now, record=False))
            return payload, result, home_prepared.tuning_key(reader, now), previous_size

    preparation = VibePreparation(expire=lambda now: mix_tuning.expire(store, now),
        snapshot=store.read_snapshot, capture=capture,
        remember=lambda *args, **kw: home_prepared.remember(store, *args, **kw),
        atomic=lambda: atomic_write(store), now=ctx.now_fn, logger=ctx.logger,
        schedule=lambda: ctx.rec_worker.trigger_candidate_refresh() if ctx.rec_worker else None)
    return VibeRequests(store.settings, work=activity_for(ctx), adjusting=lambda now: mix_tuning.adjusting_vibe(store, now),
        atomic=lambda: atomic_write(store), background=lambda: store.priority.background(check_cancellable_work),
        build=preparation.build, logger=ctx.logger)
