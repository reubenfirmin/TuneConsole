"""Whole-context choice evidence shared by Albums, Mix tapes and On air.

Raw records are immutable JSON owned by RecLedgerRepo. Capture and inventory/replay belong to
the existing worker; serving only freezes events and reads exact menu metadata. No track is trained here.
"""
import hashlib
import json
import logging
import math
import time
import uuid
from copy import deepcopy

from yt_playlist.core.activity import check_cancellable_work, WorkBudgetExceeded, WorkClosing
from yt_playlist.core.evidence_ledger import canonical
from yt_playlist.core import mix_choices
from yt_playlist.controllers.choice_context import ChoiceContexts
from yt_playlist.rec.choice_context import context_manifest as _context_manifest, learning_context
from yt_playlist.library.choice_composition import resolve_context
from yt_playlist.rec import group_roots, home_cache, recommendation_roots

logger = logging.getLogger(__name__)
from yt_playlist.core.choice_capture_contract import FORMATS, ACTIONS, STORAGE_SECONDS


def present(store, format, options, now, *, context, window, episode_id=None, vibe=None, reader=None, defer=None):
    """Rerenders reuse one complete menu; edits or new model inputs create another opportunity."""
    if getattr(store, 'activity_paused', False) or getattr(store, '_read_only', False):
        return None
    options, context = deepcopy(list(options)), deepcopy(context)
    if format == 'mixes':
        options = [mix_choices.visible_option(option) for option in options]
        context = ChoiceContexts(store.rec_ledger.evidence()).mix(context)
    menu = [format, window, options, context]
    if vibe is not None:
        menu.append(vibe)
    identity = hashlib.sha256(canonical(menu)).hexdigest()[:32]
    if _submit_offer(store, format, options, now, context=context, episode_id=episode_id,
                     opportunity_id=identity, reuse=True, vibe=vibe) is None:
        return None
    surface = 'choice_window:' + format
    save_window = store.rec_proposals.put_proposals
    value = {'opportunity_id': identity, 'window': str(window)}
    if defer is None:
        reader = store if reader is None else reader
        held = reader.rec_proposals.get_proposals(surface) or {}
        if held.get('opportunity_id') != identity:
            save_window(surface, value, now)
    else:
        defer('save listening choice window', save_window, surface, value, now)
    return identity


def opportunity(store, identity):
    """Exact serving metadata, including an offer awaiting its worker write."""
    from yt_playlist.library.choice_composition import responses
    return responses(store).opportunity(identity)


def close_window(store, format, now, *, opportunity_id):
    """An explicit request for alternatives closes the requesting view's offer.

    Reloads, background refreshes and expired caches never imply rejection.
    Missing or invalid view identity must not close another view's latest menu.
    """
    if not isinstance(opportunity_id, str) or not 0 < len(opportunity_id) <= 64:
        return None
    held = opportunity(store, opportunity_id)
    if not held or held.get('format') != format:
        return None
    safe_respond(store, opportunity_id, 'close', now,
                 details={'reason': 'requested_alternatives'})
    return held


def model_context(store, now, *, facet_leans=None):
    # Albums and Mix tapes prepare on the same worker timestamp. Retain the exact
    # captured context instead of loading both model spaces again for each surface.
    return store.memoized_read(('choice-model-context', now, canonical(facet_leans)),
        lambda: _model_context(store, now, facet_leans=facet_leans))


def prepare_context(store, now):
    """Complete shared model inputs before either surface starts ranking."""
    from yt_playlist.core.activity import WorkBudgetExceeded
    from yt_playlist.rec import transient
    try:
        with store.read_snapshot() as snapshot:
            reader = snapshot.store
            model_context(reader, now, facet_leans=transient.prepared_facet_leans(reader, now))
        return True
    except WorkBudgetExceeded:
        store.maintenance_diagnostics.record('deferred', reasons=('time_budget',),
                                             phase='rec: listening context')
        return False


def freeze_context(store, context):
    """Retain the original preparation inputs or defer its publication.

    Reclaim for each missing component and the manifest separately: an earlier
    component may fit while the final manifest still needs optional space.
    """
    from yt_playlist.core.evidence_ledger import EvidenceBudgetExceeded
    ledger = store.rec_ledger.evidence()
    pinned = {context['artifact']} if context.get('artifact') else set()

    def publish(value):
        try:
            identity = ledger.artifact(value)
        except EvidenceBudgetExceeded as exc:
            maintain_storage(store, context.get('at', time.time()), required_bytes=exc.required_bytes,
                             protected_artifacts=pinned)
            identity = ledger.artifact(value)
        # Reused components may be old enough for collection. Keep every input
        # already shared by this capture through any later allocation recovery.
        pinned.add(identity)
        return identity

    return {'artifact': publish(_context_manifest(ledger, context, publish=publish))}


def _model_context(store, now, *, facet_leans=None):
    """Freeze actual four-stage readings on the worker's private preparation snapshot.

    Context centroids/weights are retained, not merely a hash whose input later disappears.
    This supports predictions on these recorded options, not fitting arbitrary new embeddings.
    """
    from yt_playlist.rec import group_viz, rec_params, scoring, transient
    encoder = store.rec_model.recording_encoder()
    has_content = bool(encoder)
    recording = store.rec_model.recording_state()
    head = group_viz.prepared(store)
    (current, count, _), (session, session_count, _) = scoring.group_readings(store, head, now)
    policy = {"params": {spec.name: rec_params.get_param(store, spec.name) for spec in rec_params.PARAMS},
              "weights": store.rec_model.get_weights(now=now), "leans": store.rec_model.get_leans(),
              "content_model": encoder,
              "content_generation": recording.generation,
              "collaborative_inputs": recording.fit_fingerprint,
              "album_feedback": scoring.album_feedback_state.state(store),
              "format_preference": store.settings.get_setting("home_format"),
              "method": store.settings.get_setting("rec_embed_method"),
              "dim": store.settings.get_setting("rec_dim")}
    def taste(value):
        return {"titles": value.titles, "centroids": value.centroids.tolist(),
                "weights": value.weights.tolist(), "sources": getattr(value, "sources", [])}
    return {"schema": 2, "at": now, "policy": policy,
        "layers": {"now": {"groups": current, "events": count},
                   "today": {"groups": session, "events": session_count,
                               "halflife_h": rec_params.get_param(store, "session_halflife_h")},
                   "recent": {"facets": transient.facet_leans(store, now) if facet_leans is None else facet_leans},
                   "permanent": {"collaborative": taste(scoring.playlist_taste(store, now=now)),
                             "content": taste(scoring.content_taste(store, now=now)) if has_content else None}}}


def issue(store, format, options, now, *, context, episode_id=None, opportunity_id=None, reuse=False,
          vibe=None):
    """Freeze the complete actual menu and its pre-response state, with optional scored forecast."""
    if getattr(store, 'activity_paused', False):
        return None
    options, context = deepcopy(list(options)), deepcopy(context)
    if format == 'mixes':
        options = [mix_choices.visible_option(option) for option in options]
        context = ChoiceContexts(store.rec_ledger.evidence()).mix(context)
    return _submit_offer(store, format, options, now, context=context, episode_id=episode_id,
                         opportunity_id=opportunity_id, reuse=reuse, vibe=vibe)


def _submit_offer(store, format, options, now, *, context, episode_id, opportunity_id, reuse, vibe):
    if getattr(store, 'activity_paused', False):
        return None
    if format not in FORMATS or not math.isfinite(now):
        raise ValueError("Unknown listening format")
    if vibe is not None:
        from yt_playlist.rec.home_vibe import PRESETS
        if format != 'mixes' or vibe not in PRESETS:
            raise ValueError('Unknown displayed Vibe')
    context.pop('comparison', None)  # A restored offer can predate trial retirement.
    generations = set(recommendation_roots.references(context)) | set(recommendation_roots.from_options(options))
    if generations:
        context['recommendation_generations'] = recommendation_roots.references(
            {'recommendation_generations': sorted(generations)})
    ids = [str(option["id"]) for option in options]
    if not options or any(not identity for identity in ids) or len(ids) != len(set(ids)):
        raise ValueError("A choice opportunity needs distinct alternatives")
    identity = opportunity_id or uuid.uuid4().hex
    record = {"schema": 1, "id": identity, "format": format, "at": now, "observed_at": now,
              "episode_id": episode_id or identity,
              "options": options}
    if vibe is not None:
        # The visible tuning choice belongs to the small decision envelope.
        # It carries no playback recipe, recording membership or model history.
        record['vibe'] = vibe
    if format == 'mixes':
        record['context_at'] = context.get('at', now)
    capture = getattr(store, 'choice_capture', None)
    if capture is not None:
        try:
            close = group_roots.pending(store, {}, now)
        except WorkBudgetExceeded:
            return None
        try:
            accepted = capture.enqueue('listening:' + format, _issue, store, record, context,
                reuse=reuse, opportunity=record, deduplicate=reuse, on_close=close, group_context=context,
                artifacts=(context['artifact'],) if context.get('artifact') else ())
        except BaseException:
            if close is not None:
                close()
            raise
        return identity if accepted else None
    try:
        with group_roots.admission(store, blocking=False):
            return _issue(store, record, context, reuse=reuse)
    except WorkBudgetExceeded:
        return None


def _issue(store, record, context, *, reuse=False):
    with group_roots.admission(store):
        group_roots.retain(store, 'offer', record['id'], context, record['at'])
        return _issue_record(store, record, context, reuse=reuse)


def _issue_record(store, record, context, *, reuse=False):
    from yt_playlist.library.choice_composition import evidence
    return evidence(store).issue(record, context, reuse=reuse)


def respond(store, opportunity_id, kind, now, *, option_id=None, action_id=None, visible=(), details=None,
            synchronous=False):
    """Queue an observation, or persist a durable intent under nonblocking admission."""
    from yt_playlist.library.choice_composition import responses
    return responses(store).respond(opportunity_id, kind, now, option_id=option_id,
        action_id=action_id, visible=visible, details=details, synchronous=synchronous)


def _respond(store, opportunity_id, kind, now, *, option_id, action_id, visible, details):
    with group_roots.admission(store):
        return _respond_record(store, opportunity_id, kind, now, option_id=option_id,
                               action_id=action_id, visible=visible, details=details)


def _respond_record(store, opportunity_id, kind, now, *, option_id, action_id, visible, details):
    from yt_playlist.library.choice_composition import evidence
    return evidence(store).respond(opportunity_id, kind, now, option_id=option_id,
        action_id=action_id, visible=visible, details=details)


def safe_issue(*args, **kwargs):
    try:
        return issue(*args, **kwargs)
    except WorkClosing:
        raise
    except Exception:
        logger.warning("listening opportunity measurement failed", exc_info=True)
        return None


def safe_respond(*args, **kwargs):
    try:
        return respond(*args, **kwargs)
    except WorkClosing:
        raise
    except Exception:
        logger.warning("listening response measurement failed", exc_info=True)
        return False


def album_playback_context(store, msg, now, *, account_id=None):
    """Resolve a recent deliberate album queue without treating navigation as playback."""
    from yt_playlist.library.choice_composition import playback
    return playback(store).album_context(msg, now, account_id=account_id)


def record_playback(store, msg, now, *, account_id=None):
    """Called only after an eligible, verified new live play, never a heartbeat/navigation."""
    from yt_playlist.library.choice_composition import playback
    return playback(store).record(msg, now, account_id=account_id)


def refresh_report(store, now):
    """Home retries reuse unchanged evidence until an explicit projection deadline.

    Full storage inventory/retention belongs to daily maintenance. Continuous usage
    decay is refreshed at most every 15 minutes; event admission and bandit expiry
    can bring that deadline forward. Raw replay remains available independently.
    """
    from yt_playlist.library.listening_window import choice_timezone
    from yt_playlist.rec import choice_bandit, choice_statistics, group_learning, rec_params
    generation = store.rec_ledger.evidence().generation()
    inputs = hashlib.sha256(canonical({
        'bandit': choice_bandit.parameters(store),
        'modes': choice_bandit.generation(store.modes.list_modes(active_only=True)),
        'groups': store.taste_groups.serial(),
        'group_learning': group_learning.POLICY,
        'halflife': rec_params.get_param(store, 'choice_halflife_d'),
        'attribution': rec_params.get_param(store, 'choice_attribution_h'),
        'album_identity': [2, store.collection.album_alias_revision()],
        'listener_policy': [choice_statistics.INTEREST_POLICY, choice_timezone(store),
                            {str(account): zone for account, zone in store.history.listening_calendars().items()}],
    })).hexdigest()
    checkpoint = store.rec_proposals.report_checkpoint('listening_choices') or {}
    if (checkpoint.get('generation') == generation and checkpoint.get('inputs') == inputs
            and checkpoint.get('at') is not None and checkpoint.get('next_at') is not None
            and checkpoint['at'] <= now < checkpoint['next_at']):
        previous = store.rec_proposals.get_proposals('listening_choices')
        if previous is not None:
            return previous
    return compile_report(store, now, refresh={'generation': generation, 'inputs': inputs})


def compile_report(store, now, *, refresh=None):
    """Rebuild compact descriptive/predictive findings; never infer missing actions as rejection."""
    from yt_playlist.library.choice_composition import reports
    return reports(store).compile(now, refresh=refresh)


def compile_facts(all_offers, all_responses, now, *, halflife, attribution, album_ids):
    from yt_playlist.rec.choice_statistics import compile_facts as calculate
    return calculate(all_offers, all_responses, now, halflife=halflife, attribution=attribution, album_ids=album_ids)


def daily_due(store, now):
    previous = store.rec_proposals.report_checkpoint('choice_daily') or {}
    return previous.get('at') is None or now - previous['at'] >= 86400


def replay(store, now, predictor=None):
    """Predict before revealing each response, then advance an isolated candidate.

    A candidate implements predict(context, options) and observe(event). It receives only
    evidence already observed at this point in the replay. This evaluates the recorded
    menus; a candidate proposing different menus needs controlled prospective exposure.
    No candidate is fitted or published into serving by this interface.
    """
    from yt_playlist.library.choice_composition import replay as command
    return command(store).run(now, predictor)


def replay_facts(offers, events, captured, now, predictor=None):
    from yt_playlist.rec.choice_statistics import replay_facts as calculate
    return calculate(offers, events, captured, now, predictor)


def maintain_storage(store, now, *, required_bytes=0, protected_artifacts=()):
    from yt_playlist.core.activity import WorkBudgetExceeded
    store = group_roots.source(store)
    # Pending work is scheduling intent, not an unsafe installed format. The
    # domain admission/read leases fence publication and collection per turn.
    with store._evidence_state_lock:
        if getattr(store, '_evidence_maintenance', 0):
            return {'deferred': True}
        store._evidence_maintenance = getattr(store, '_evidence_maintenance', 0) + 1
        store._evidence_recovery_pending = True
    try:
        store.evidence_files.update_maintenance(pending=True)
        # Inventory/checkpoint/retention already commit durable prefixes. A
        # deadline inside a prefix rolls it back and repeats its reads next time.
        with group_roots.admission(store):
            result = _maintain_storage(store, now, required_bytes=required_bytes,
                                      protected_artifacts=protected_artifacts)
        store._evidence_recovery_failed = False
        if not result.get('deferred'):
            store.evidence_files.update_maintenance(pending=False, phase='inventory')
            store._evidence_recovery_pending = False
        return result
    except WorkBudgetExceeded:
        store.maintenance_diagnostics.record('deferred', reasons=('evidence-storage-budget',))
        return {'deferred': True}
    except Exception:
        store._evidence_recovery_failed = True
        raise
    finally:
        with store._evidence_state_lock:
            store._evidence_maintenance -= 1


def _maintain_storage(store, now, *, required_bytes=0, protected_artifacts=()):
    """Release acknowledged diagnostic contexts; preserve serving and correction inputs.

    The short grace protects in-flight preparation between artifact publication
    and its proposal transaction. Durable evidence uses its independent retention.
    """
    from yt_playlist.library.choice_composition import storage
    return storage(getattr(store, '_evidence_source', store)).run(now,
        required_bytes=required_bytes, protected_artifacts=protected_artifacts)


def daily(store, now):
    storage = maintain_storage(store, now)
    report = compile_report(store, now)
    evaluation = replay(store, now)
    # Content-addressed revisions preserve corrections on the original offer.
    identity = hashlib.sha256(canonical([int(now // 86400), report["watermark"]])).hexdigest()
    ledger = store.rec_ledger.evidence()
    previous = ledger.get("report", identity)
    if previous is None:
        from yt_playlist.core.evidence_ledger import EvidenceBudgetExceeded
        try:
            ledger.put("report", identity, {"id": identity, "report": report, "evaluation": evaluation})
        except EvidenceBudgetExceeded:
            storage["full"] = True
    store.rec_proposals.put_proposals("choice_daily", {"schema": 1, "at": now,
        "report_id": identity, "watermark": report["watermark"], "evaluation": evaluation, "storage": storage}, now)
    return evaluation
