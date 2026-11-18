"""Bind listening-learning capture to explicit descriptor and taxonomy owners."""
from contextlib import contextmanager
from yt_playlist.controllers.choice_learning import ChoiceLearning
from yt_playlist.controllers.choice_context import ChoiceContexts
from yt_playlist.controllers.feedback_publication import FeedbackPublication
from yt_playlist.rec import choice_bandit


def learning(store):
    return ChoiceLearning(expand=store.rec_ledger.expand_mix_options,
        parameters=lambda: choice_bandit.parameters(store),
        modes=lambda: store.modes.list_modes(active_only=True), axes=store.genres.feature_axes)


def resolve_context(store, context):
    return ChoiceContexts(store.rec_ledger.evidence()).resolve(context)


def evidence(store):
    from yt_playlist.controllers.choice_evidence import ChoiceEvidence
    from yt_playlist.rec import group_roots
    return ChoiceEvidence(store.rec_ledger.evidence(), publish_mix=store.rec_ledger.publish_mix_menu,
        retain=lambda *args: group_roots.retain(store, *args), roots_enabled=lambda: group_roots.enabled(store))


def playback(store):
    from yt_playlist.controllers.playback_choices import PlaybackChoices
    from yt_playlist.rec import rec_params
    observations = None

    def response_command():
        nonlocal observations
        if observations is None:
            observations = responses(store)
        return observations

    def album_capture():
        with store.read_view() as reader:
            return reader.rec_ledger.album_playback_context(), rec_params.get_param(reader, 'choice_attribution_h') * 3600

    return PlaybackChoices(store.rec_ledger, paused=lambda: getattr(store, 'activity_paused', False),
        opportunity=lambda identity: response_command().opportunity(identity),
        respond=lambda *args, **kwargs: response_command().safe_respond(*args, **kwargs),
        horizon=lambda: rec_params.get_param(store, 'choice_attribution_h') * 3600,
        album_capture=album_capture, source=store.collection.playback_source, recipe=store.playlist_recipes.get_recipe)


def feedback(store):
    observations = responses(store)
    return FeedbackPublication(store.album_feedback,
        respond=observations.respond,
        evidence=lambda *args: store.rec_ledger.evidence().get(*args),
        paused=lambda: store.activity_paused,
        cursor=lambda: getattr(store, '_album_feedback_response_after', 0),
        advance=lambda value: setattr(store, '_album_feedback_response_after', value))


def responses(store):
    from yt_playlist.controllers.choice_responses import ChoiceResponses
    from yt_playlist.library import group_lifetimes
    return ChoiceResponses(store.rec_ledger.evidence(), capture=getattr(store, 'choice_capture', None),
        paused=lambda: store.activity_paused, admission=lambda **kw: group_lifetimes.admission(store, **kw),
        pending=lambda *args: group_lifetimes.pending(store, *args),
        roots_enabled=lambda: group_lifetimes.enabled(store), persist=evidence(store).respond)


def reports(store):
    from yt_playlist.controllers.choice_reports import ChoiceReports
    from yt_playlist.rec import rec_params

    @contextmanager
    def learners():
        with store.read_view() as reader:
            yield learning(reader), getattr(reader.taste_groups, 'learning_head', reader.taste_groups.head)()

    return ChoiceReports(store.rec_ledger.evidence(),
        parameters=lambda: (rec_params.get_param(store, 'choice_halflife_d'),
                            rec_params.get_param(store, 'choice_attribution_h') * 3600),
        album_ids=store.collection.preferred_album_ids, learners=learners, publish=store.choice_findings.publish)


def replay(store):
    from yt_playlist.controllers.choice_replay import ChoiceReplay
    from yt_playlist.core.choice_checkpoints import checkpoint
    ledger = store.rec_ledger.evidence()
    contexts = ChoiceContexts(ledger)
    return ChoiceReplay(ledger, resolve=contexts.resolve, learning=contexts.learning,
        checkpoint=lambda identity: checkpoint(ledger, identity), expand=store.rec_ledger.expand_mix_options)


def storage(store):
    from yt_playlist.controllers.choice_storage import ChoiceStorage, EvidenceProtection
    from yt_playlist.rec import choice_retention, group_roots
    from yt_playlist.core.choice_capture_contract import STORAGE_PHASE
    from yt_playlist.library.group_composition import backfill
    from yt_playlist.rec import home_cache, product_comparison, rec_params
    capture_queue = getattr(store, 'choice_capture', None)

    @contextmanager
    def capture():
        with store.read_view() as reader:
            yield EvidenceProtection(reader.rec_proposals, reader.radio_checkpoints, reader.model_selection,
                reader.settings, home=lambda surface: home_cache.read(reader, surface)
                    if surface in home_cache.SURFACES else reader.rec_proposals.get_proposals(surface),
                comparison=lambda: (product_comparison.state(reader),
                    reader.rec_proposals.get_proposals(product_comparison.MONITOR) or {}))

    def inventory():
        if not group_roots.enabled(store):
            return False
        if not hasattr(store, '_group_root_backfill'):
            store._group_root_backfill = backfill(store)
        return store._group_root_backfill.step()

    return ChoiceStorage(store.rec_ledger.evidence(), store.settings, phase_key=STORAGE_PHASE,
        inventory=inventory,
        acknowledge=lambda now: choice_retention.acknowledge(store, now,
            lambda _store, at: reports(store).compile(at)),
        more=lambda: getattr(store, '_choice_retention_more', False), capture=capture,
        pending=lambda: (capture_queue.artifacts(), capture_queue.opportunities()) if capture_queue is not None else ((), ()),
        protected=lambda: group_roots.enabled(store) and not store.group_evidence.roots_complete(),
        pending_protection=lambda: store.album_feedback.protection(capture_queue),
        retired=(lambda offers, checkpoints: group_roots.retired(store, offers, checkpoints)) if group_roots.enabled(store) else None,
        phase=lambda value: setattr(store, '_evidence_phase', value),
        progress=lambda value: setattr(store, '_evidence_progress', value),
        retention_days=lambda: rec_params.get_param(store, 'choice_retention_d'),
        diagnostic_days=choice_retention.DIAGNOSTIC_DAYS, diagnostic_fraction=choice_retention.DIAGNOSTIC_FRACTION)
