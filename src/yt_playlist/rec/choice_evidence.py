"""Whole-context choice evidence shared by Albums, Mix tapes and On air.

Raw records are immutable JSON owned by RecLedgerRepo. Capture and inventory/replay belong to
the existing worker; serving only freezes events and reads exact menu metadata. No track is trained here.
"""
import hashlib
import json
import logging
import math
import uuid
from copy import deepcopy

from yt_playlist.core.activity import check_cancellable_work, WorkBudgetExceeded, WorkClosing
from yt_playlist.core.evidence_ledger import canonical
from yt_playlist.rec.choice_context import context_manifest as _context_manifest, resolve_context

logger = logging.getLogger(__name__)
FORMATS = {"albums", "mixes", "radio"}
ACTIONS = {"visible", "open", "choice", "start", "save", "promote", "return", "experience", "feedback", "withdraw", "close"}


def present(store, format, options, now, *, context, window, episode_id=None):
    """Rerenders reuse one complete menu; edits or new model inputs create another opportunity."""
    identity = hashlib.sha256(canonical([format, window, options, context])).hexdigest()[:32]
    if issue(store, format, options, now, context=context, episode_id=episode_id,
             opportunity_id=identity, reuse=True) is None:
        return None
    held = store.rec_proposals.get_proposals('choice_window:' + format) or {}
    if held.get('opportunity_id') != identity:
        store.rec_proposals.put_proposals('choice_window:' + format,
            {'opportunity_id': identity, 'window': str(window)}, now)
    return identity


def opportunity(store, identity):
    """Exact serving metadata, including an offer awaiting its worker write."""
    capture = getattr(store, 'choice_capture', None)
    pending = capture.get(identity) if capture is not None else None
    return pending or store.rec_ledger.evidence().get('opportunity', identity)


def close_window(store, format, now):
    """An explicit request for alternatives closes the visible previous offer.

    Reloads, background refreshes and expired caches never imply rejection.
    """
    held = store.rec_proposals.get_proposals('choice_window:' + format) or {}
    if held.get('opportunity_id'):
        safe_respond(store, held['opportunity_id'], 'close', now,
                     details={'reason': 'requested_alternatives'})


def model_context(store, now, *, facet_leans=None):
    # Albums and Mix tapes prepare on the same worker timestamp. Retain the exact
    # captured context instead of loading both model spaces again for each surface.
    return store.memoized_read(('choice-model-context', now, canonical(facet_leans)),
        lambda: _model_context(store, now, facet_leans=facet_leans))


def prepare_context(store, now):
    """Capture shared model inputs before either surface's ranking deadline starts."""
    from yt_playlist.core.activity import work_budget, WorkBudgetExceeded
    from yt_playlist.rec import progressive, transient
    try:
        with work_budget(progressive.Budget().fit_seconds), store.read_snapshot() as snapshot:
            reader = snapshot.store
            model_context(reader, now, facet_leans=transient.facet_leans(reader, now))
        return True
    except WorkBudgetExceeded:
        store.maintenance_diagnostics.record('deferred', reasons=('time_budget',),
                                             phase='rec: listening context')
        return False


def freeze_context(store, context):
    """Keep large immutable model captures out of disposable SQLite menus."""
    from yt_playlist.core.evidence_ledger import EvidenceBudgetExceeded
    try:
        ledger = store.rec_ledger.evidence()
        return {'artifact': ledger.artifact(_context_manifest(ledger, context))}
    except EvidenceBudgetExceeded:
        logger.warning('Listening context capture deferred: evidence storage is full')
        return {'capture_status': 'storage_full'}
    except (WorkBudgetExceeded, WorkClosing):
        raise
    except Exception:
        logger.warning('Listening context capture failed', exc_info=True)
        return {'capture_status': 'unavailable'}


def _model_context(store, now, *, facet_leans=None):
    """Freeze actual four-stage readings on the worker's private preparation snapshot.

    Context centroids/weights are retained, not merely a hash whose input later disappears.
    This supports predictions on these recorded options, not fitting arbitrary new embeddings.
    """
    from yt_playlist.rec import layers, rec_params, scoring, transient
    has_content = bool(store.settings.get_setting("rec_content_model"))
    current, count, _ = layers.now_mode_mix(store, now) if has_content else (None, 0, [])
    session, session_count, _ = layers.session_mode_mix(store, now) if has_content else (None, 0, [])
    policy = {"params": {spec.name: rec_params.get_param(store, spec.name) for spec in rec_params.PARAMS},
              "weights": store.rec_model.get_weights(now=now), "leans": store.rec_model.get_leans(),
              "content_model": store.settings.get_setting("rec_content_model"),
              "content_generation": store.settings.get_setting("rec_content_generation"),
              "collaborative_inputs": store.settings.get_setting("rec_fit_inputs"),
              "group_inputs": store.settings.get_setting("rec_mode_fit_inputs"),
              "album_feedback": store.settings.get_setting("album_feedback"),
              "format_preference": store.settings.get_setting("home_format"),
              "method": store.settings.get_setting("rec_embed_method"),
              "dim": store.settings.get_setting("rec_dim")}
    def taste(value):
        return {"titles": value.titles, "centroids": value.centroids.tolist(),
                "weights": value.weights.tolist(), "sources": getattr(value, "sources", [])}
    return {"schema": 1, "at": now, "policy": policy,
        "groups": [{**mode, "centroid": mode["centroid"].tolist()}
                   for mode in store.modes.list_modes(active_only=True)],
        "layers": {"now": {"modes": current, "events": count,
                           "window_h": rec_params.get_param(store, "now_window_h")},
                   "session": {"modes": session, "events": session_count,
                               "halflife_h": rec_params.get_param(store, "session_halflife_h")},
                   "recent": {"facets": transient.facet_leans(store, now) if facet_leans is None else facet_leans},
                   "saved": {"collaborative": taste(scoring.playlist_taste(store, now=now)),
                             "content": taste(scoring.content_taste(store, now=now)) if has_content else None}}}


def issue(store, format, options, now, *, context, episode_id=None, opportunity_id=None, reuse=False):
    """Freeze the complete actual menu and its pre-response state, with optional scored forecast."""
    if format not in FORMATS or not math.isfinite(now):
        raise ValueError("Unknown listening format")
    options, context = deepcopy(list(options)), deepcopy(context)
    ids = [str(option["id"]) for option in options]
    if not options or any(not identity for identity in ids) or len(ids) != len(set(ids)):
        raise ValueError("A choice opportunity needs distinct alternatives")
    identity = opportunity_id or uuid.uuid4().hex
    comparison = context.get('comparison')
    record = {"schema": 1, "id": identity, "format": format, "at": now, "observed_at": now,
              "episode_id": episode_id or identity,
              "options": options}
    if comparison:
        record['comparison'] = comparison
    else:
        from yt_playlist.rec import product_comparison
        monitoring = product_comparison.monitoring_assignment(store, format, now)
        if monitoring:
            record['monitor'] = monitoring
    capture = getattr(store, 'choice_capture', None)
    if capture is not None:
        accepted = capture.enqueue('listening:' + format, _issue, store, record, context,
            reuse=reuse, opportunity=record, deduplicate=reuse,
            artifacts=(context['artifact'],) if context.get('artifact') else ())
        return identity if accepted else None
    return _issue(store, record, context, reuse=reuse)


def _issue(store, record, context, *, reuse=False):
    ledger = store.rec_ledger.evidence()
    identity = record['id']
    if reuse and ledger.get('opportunity', identity) is not None:
        return identity
    context = _context_manifest(ledger, context)
    # Derive forecasts only from the frozen offered scores, never later responses.
    options = record['options']
    scores = [option.get('score') for option in options]
    scored = all(isinstance(score, (int, float)) and math.isfinite(score) for score in scores)
    prediction = [str(options[i]['id']) for i in sorted(range(len(options)),
        key=lambda i: (-scores[i], i))] if scored else None
    record = {**record, 'context': hashlib.sha256(canonical(context)).hexdigest(),
              'prediction': prediction, 'prediction_status': 'issued' if scored else 'unscored'}
    ledger.put("opportunity", identity, record, artifact=context)
    return identity


def respond(store, opportunity_id, kind, now, *, option_id=None, action_id=None, visible=(), details=None):
    """Queue a frozen observation; the worker validates it against the actual issued menu."""
    if kind not in ACTIONS or not math.isfinite(now):
        raise ValueError("Unknown listening response")
    if kind not in {"visible", "withdraw", "close"} and option_id is None:
        raise ValueError("A listening response must name its option")
    options = {'option_id': str(option_id) if option_id is not None else None,
               'action_id': action_id, 'visible': tuple(map(str, visible)), 'details': deepcopy(details)}
    capture = getattr(store, 'choice_capture', None)
    if capture is not None:
        return capture.enqueue('listening:response', _respond, store, opportunity_id, kind, now, **options)
    return _respond(store, opportunity_id, kind, now, **options)


def _respond(store, opportunity_id, kind, now, *, option_id, action_id, visible, details):
    ledger = store.rec_ledger.evidence()
    offer = ledger.get("opportunity", opportunity_id)
    if offer is None:
        return False
    ids = {str(option["id"]) for option in offer["options"]}
    if (option_id is not None and str(option_id) not in ids) or not set(map(str, visible)) <= ids:
        raise ValueError("Response does not belong to this opportunity")
    if now < offer["at"]:
        raise ValueError("Response precedes its opportunity")
    identity = action_id or f"{opportunity_id}:{kind}:{option_id or ''}"
    # A retry must keep its original observation time; exact source IDs are never counted twice.
    previous = ledger.get("response", identity)
    if previous:
        if (previous["opportunity_id"], previous["kind"], previous["option_id"]) != (
                opportunity_id, kind, str(option_id) if option_id is not None else None):
            raise ValueError("Response ID was reused for another action")
        return False
    return ledger.put("response", identity, {"schema": 1, "id": identity,
        "opportunity_id": opportunity_id, "kind": kind, "option_id": str(option_id) if option_id is not None else None,
        "at": now, "observed_at": now, "source": "live", "precision": "exact",
        "visible": sorted(map(str, visible)), "details": details or {}})


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


def album_playback_context(store, msg, now, *, identity_id=None):
    """Resolve a recent deliberate album queue without treating navigation as playback."""
    from yt_playlist.rec import rec_params
    try:
        pending = json.loads(store.settings.get_setting('listening_album_pending') or 'null')
        if (not pending or not 0 <= now - pending['at'] <=
                rec_params.get_param(store, 'choice_attribution_h') * 3600):
            return None
        if (pending.get('identity_id') is not None and identity_id is not None
                and pending['identity_id'] != identity_id):
            return None
        if pending.get('videos') and msg.get('videoId') not in pending['videos']:
            return None
        if (msg.get('playlist') == pending['playlist']
                or msg.get('albumBrowseId') == pending['browse']):
            return pending
    except (ValueError, TypeError, KeyError):
        logger.warning('Invalid pending album playback context', exc_info=True)
    return None


def record_playback(store, msg, now, *, identity_id=None):
    """Called only after an eligible, verified new live play, never a heartbeat/navigation."""
    from yt_playlist.rec import rec_params
    def bind(opportunity, option):
        if identity_id is not None and msg.get('playlist') and msg.get('videoId'):
            store.rec_ledger.bind_choice_playback(identity_id, msg['playlist'], msg['videoId'],
                opportunity, option, now, rec_params.get_param(store, 'choice_attribution_h') * 3600)
    try:
        pending = album_playback_context(store, msg, now, identity_id=identity_id)
        if pending:
            if identity_id is not None and pending.get('identity_id') is None:
                store.settings.set_setting('listening_album_pending', json.dumps({**pending, 'identity_id': identity_id}))
            for kind in ("start", "experience"):
                safe_respond(store, pending["opportunity_id"], kind, now, option_id=pending["browse"],
                    action_id=f"album:{pending.get('action_id', pending['opportunity_id'])}:{kind}:{msg.get('videoId')}",
                    details={"video_id": msg.get("videoId"), "album_browse_id": pending["browse"],
                             "playlist": msg.get("playlist")})
            bind(pending['opportunity_id'], pending['browse'])
        source = store.collection.playback_source(msg.get("playlist")) if msg.get("playlist") else None
        if source and source["kind"] == "playlist":
            recipe = store.playlist_recipes.get_recipe(msg["playlist"]) or {}
            if recipe.get("opportunity_id") and recipe.get("option_id"):
                safe_respond(store, recipe["opportunity_id"], "start", now, option_id=recipe["option_id"],
                    action_id=f"mix:{source['id']}:start", details={"playlist_id": source["id"]})
                safe_respond(store, recipe["opportunity_id"], "experience", now, option_id=recipe["option_id"],
                    action_id=f"mix:{source['id']}:track:{msg.get('videoId')}",
                    details={"playlist_id": source["id"], "video_id": msg.get("videoId")})
                bind(recipe['opportunity_id'], recipe['option_id'])
    except WorkClosing:
        raise
    except Exception:
        logger.warning("listening playback attribution failed", exc_info=True)


def compile_report(store, now):
    """Rebuild compact descriptive/predictive findings; never infer missing actions as rejection."""
    ledger = store.rec_ledger.evidence()
    offers = {row["id"]: row for row in ledger.records("opportunity") if row["observed_at"] <= now}
    responses = sorted((r for r in ledger.records("response") if r["observed_at"] <= now),
                       key=lambda r: (r["observed_at"], r["id"]))
    withdrawn = {r["details"].get("action_id") for r in responses if r["kind"] == "withdraw"}
    rows = {format: {"offered": 0, "visible": 0, "opens": 0, "choices": 0, "predicted_choices": 0,
        "top_choice_hits": 0, "reciprocal_rank": 0., "starts": 0, "saves": 0,
        "returns": 0, "promotions": 0, "experience": 0, "independent_episodes": 0} for format in FORMATS}
    seen, episodes, stages = {}, {f: set() for f in FORMATS}, set()
    interests = []
    for offer in offers.values():
        rows[offer["format"]]["offered"] += 1
    for event in responses:
        check_cancellable_work()
        if event["id"] in withdrawn or event["kind"] == "withdraw":
            continue
        offer = offers.get(event["opportunity_id"])
        if not offer or event["at"] < offer["at"]:
            continue
        row, oid = rows[offer["format"]], offer["id"]
        if event["kind"] == "visible":
            if oid not in seen:
                row["visible"] += 1
            seen.setdefault(oid, set()).update(event["visible"])
        elif event["kind"] == "choice":
            row["choices"] += 1
            episodes[offer["format"]].add(offer["episode_id"])
            option = next((option for option in offer["options"] if str(option["id"]) == event["option_id"]), None)
            if option:
                tracks = option.get("tracks") or []
                edited = event.get("details", {}).get("tracks")
                if edited is not None:
                    tracks = edited
                details = event.get('details') or {}
                keys = (details['keys'] if 'keys' in details else
                        [track['key'] for track in tracks if track.get('key')] if edited is not None else
                        option.get('keys') or [track['key'] for track in tracks if track.get('key')])
                interests.append({"format": offer["format"], "option_id": event["option_id"],
                    "mode_id": option.get("mode_id"), "intent": option.get("intent"),
                    "keys": sorted(set(keys))[:100], "feature": option.get("feature"), "basis": option.get("basis"),
                    "at": event["at"], "episode": offer["episode_id"], "opportunity_id": oid})
            visible = seen.get(oid, set())
            prediction = [identity for identity in offer.get("prediction") or [] if identity in visible]
            if event["option_id"] in prediction:
                row["predicted_choices"] += 1
                rank = prediction.index(event["option_id"]) + 1
                row["top_choice_hits"] += int(rank == 1)
                row["reciprocal_rank"] += 1 / rank
        elif event["kind"] == "feedback" and event.get("details", {}).get("kind") == "more":
            option = next((option for option in offer["options"] if str(option["id"]) == event["option_id"]), None)
            if option:
                interests.append({"format": offer["format"], "option_id": event["option_id"],
                    "mode_id": option.get("mode_id"), "intent": option.get("intent"),
                    "keys": sorted(set(option.get("keys") or [track["key"] for track in option.get("tracks", []) if track.get("key")]))[:100],
                    "feature": option.get("feature"), "basis": option.get("basis"),
                    "at": event["at"], "episode": offer["episode_id"], "opportunity_id": oid})
        elif event["kind"] in {"open", "start", "save", "promote", "return", "experience"}:
            key = (oid, event["kind"], event["option_id"])
            if key not in stages:
                row[{"open": "opens", "start": "starts", "save": "saves", "promote": "promotions", "return": "returns",
                     "experience": "experience"}[event["kind"]]] += 1
                stages.add(key)
    from yt_playlist.rec import choice_summary
    outcomes = choice_summary.summarize(offers, [r for r in responses
        if r['id'] not in withdrawn and r['kind'] != 'withdraw'], FORMATS)
    for format, row in rows.items():
        row.update(outcomes[format])
        row['returns'] += row['return_days']
        row["independent_episodes"] = len(episodes[format])
        row["top_choice_rate"] = row["top_choice_hits"] / row["predicted_choices"] if row["predicted_choices"] else None
        row["mean_reciprocal_rank"] = row["reciprocal_rank"] / row["predicted_choices"] if row["predicted_choices"] else None
    watermark = hashlib.sha256(canonical([sorted(offers), [r["id"] for r in responses]])).hexdigest()
    report = {"schema": 1, "at": now, "watermark": watermark, "formats": rows,
              "status": "observational", "promotion_supported": False,
              "source_opportunities": len(offers), "source_responses": len(responses)}
    from yt_playlist.rec import home_preferences, rec_params
    report['home_format'] = home_preferences.compile_usage(offers, [r for r in responses
        if r['id'] not in withdrawn and r['kind'] != 'withdraw'], now,
        rec_params.get_param(store, 'choice_halflife_d'))
    # A bounded operational projection, never one positive training row for every generated song.
    from yt_playlist.rec.choice_learning import interest_key
    groups, episode_groups = {}, {}
    for interest in interests:
        identity = interest_key(interest)
        group = groups.setdefault(identity, {"latest": interest, "days": set(), "episodes": set()})
        group["latest"] = interest
        group["days"].add(int(interest["at"] // 86400))
        group["episodes"].add(interest["episode"])
        episode_groups.setdefault(interest["episode"], set()).add(identity)
    compiled = [{**group["latest"], "days": len(group["days"]), "episodes": len(group["episodes"]),
                 "episode_support": min(1., sum(1 / len(episode_groups[episode]) for episode in group["episodes"]))}
                for group in groups.values()]
    serving_contexts = sorted(compiled, key=lambda row: -row["at"])[:128]
    projection = {"schema": 1, "at": now,
        "watermark": watermark, "contexts_total": len(compiled),
        "contexts": serving_contexts}
    album_days = choice_summary.album_track_days(offers, [event for event in responses
        if event['id'] not in withdrawn and event['kind'] != 'withdraw' and event['at'] <= now],
        rec_params.get_param(store, 'choice_attribution_h') * 3600)
    # Share the existing bounded operational projection. The raw source evidence
    # remains in the ledger, and recompilation applies withdrawals and corrections.
    projection['album_track_days'] = {row['option_id']: album_days[row['option_id']]
        for row in serving_contexts if row['format'] == 'albums' and row['option_id'] in album_days}
    projection['interest_version'] = hashlib.sha256(canonical(
        [serving_contexts, projection['album_track_days']])).hexdigest()
    from yt_playlist.rec import choice_bandit
    bandit = choice_bandit.compile(store, offers, [r for r in responses
        if r['id'] not in withdrawn and r['kind'] != 'withdraw'], now)
    report['bandit'] = bandit
    store.rec_proposals.publish_choice_findings(report, projection, now, bandit=bandit)
    return report


def daily_due(store, now):
    previous = store.rec_proposals.get_proposals("choice_daily") or {}
    return now - previous.get("at", float("-inf")) >= 86400


def replay(store, now, predictor=None):
    """Predict before revealing each response, then advance an isolated candidate.

    A candidate implements predict(context, options) and observe(event). It receives only
    evidence already observed at this point in the replay. This evaluates the recorded
    menus; a candidate proposing different menus needs controlled prospective exposure.
    No candidate is fitted or published into serving by this interface.
    """
    ledger = store.rec_ledger.evidence()
    offers = {offer["id"]: offer for offer in ledger.records("opportunity")
              if offer["observed_at"] <= now}
    events = sorted((event for event in ledger.records("response") if event["observed_at"] <= now),
                    key=lambda event: (event["observed_at"], event["id"]))
    withdrawn = {event["details"].get("action_id") for event in events if event["kind"] == "withdraw"}
    forecasts, visible, choices, episodes = {}, {}, set(), {}
    counts = {format: {"choices": 0, "scored": 0, "hits": 0, "unseen": 0,
                       "unscored": 0, "future_context": 0} for format in FORMATS}
    # Predict at issue time, rather than on first response. A menu opened tomorrow must
    # not benefit from choices observed after it was originally prepared/issued.
    timeline = [(offer["observed_at"], 0, oid, offer) for oid, offer in offers.items()]
    timeline.extend((event["observed_at"], 1, event["id"], event) for event in events)
    for _at, phase, _identity, event in sorted(timeline):
        check_cancellable_work()
        if phase == 0:
            offer, oid = event, event["id"]
            try:
                context = resolve_context(store, {'artifact': offer['context']})
            except ValueError:
                context = None
            valid = context is not None and context.get("at", offer["at"]) <= offer["at"]
            forecasts[oid] = ((predictor.predict(context, offer["options"]) if predictor else offer["prediction"])
                              if valid else None)
            if not valid:
                counts[offer["format"]]["future_context"] += 1
            continue
        if event["id"] in withdrawn or event["kind"] == "withdraw":
            continue
        offer = offers.get(event["opportunity_id"])
        if not offer or event["at"] < offer["at"]:
            continue
        oid = offer["id"]
        if event["kind"] == "visible":
            visible.setdefault(oid, set()).update(event["visible"])
        elif event["kind"] == "choice":
            choices.add(event['id'])
            row = counts[offer["format"]]
            row["choices"] += 1
            prediction = forecasts[oid]
            if event["option_id"] not in visible.get(oid, set()):
                row["unseen"] += 1
            elif not prediction or event["option_id"] not in prediction:
                row["unscored"] += 1
            else:
                # Compare only alternatives actually visible before the choice.
                order = [identity for identity in prediction if identity in visible[oid]]
                hit = int(order[0] == event["option_id"])
                row["scored"] += 1
                row["hits"] += hit
                unit = (offer["format"], offer["episode_id"], int(event["at"] // 86400))
                episodes.setdefault(unit, []).append(hit)
        if predictor:
            predictor.observe(event)
    for format, row in counts.items():
        units = [sum(values) / len(values) for unit, values in episodes.items() if unit[0] == format]
        row["episode_days"] = len(units)
        row["episode_day_mean"] = sum(units) / len(units) if units else None
        # Descriptive block bootstrap; repeated tracks/branches do not inflate N.
        if len(units) >= 2:
            import numpy as np
            rng = np.random.default_rng(141)
            # Keep resampling memory bounded even for a long-lived ledger.
            means = np.concatenate([rng.choice(units, (min(32, 2000 - start), len(units)),
                                   replace=True).mean(axis=1) for start in range(0, 2000, 32)])
            row["interval95"] = list(map(float, np.quantile(means, [.025, .975])))
        else:
            row["interval95"] = None
    return {"schema": 1, "at": now, "formats": counts, "basis": "recorded menus only",
            "status": "diagnostic", "promotion_supported": False,
            "source_ids": sorted(choices)}


def maintain_storage(store, now):
    """Reclaim abandoned captures before preparing more; keep active and chosen contexts.

    The short grace protects in-flight preparation between artifact publication
    and its proposal transaction. Durable evidence uses its independent retention.
    """
    from yt_playlist.rec import rec_params
    protected = set()
    capture = getattr(store, 'choice_capture', None)
    if capture is not None:
        protected.update(capture.artifacts())
    from yt_playlist.rec import product_comparison
    live = product_comparison.state(store)
    if live.get('vectors'):
        protected.add(live['vectors'])
    with store._lock:
        surfaces = [row[0] for row in store.conn.execute(
            "SELECT surface FROM rec_proposals WHERE surface LIKE 'model_pair:%'")]
    for surface in surfaces:
        pair = store.rec_proposals.get_proposals(surface) or {}
        context = (pair.get('data') or {}).get('context') or (pair.get('data') or {}).get('choice_context') or {}
        if context.get('artifact'):
            protected.add(context['artifact'])
    runs = store.model_selection.selection_runs(limit=12)
    active = store.settings.get_setting('rec_selection_active_run')
    if active and not any(str(run['id']) == active for run in runs):
        run = store.model_selection.selection_run(active)
        if run:
            runs.append(run)
    for run in runs:
        if run.get('serving_vectors'):
            protected.add(run['serving_vectors'])
    for note in (store.rec_proposals.get_proposals('album_notes') or {}).values():
        if note.get('evidence'):
            protected.add(note['evidence'])
    for surface in ('album_prepared', 'home_prepared', 'home_tuned', 'home_choice'):
        payload = store.rec_proposals.get_proposals(surface) or {}
        for field in ('context', 'choice_context', 'album_choice_context'):
            context = payload.get(field) or {}
            if context.get('artifact'):
                protected.add(context['artifact'])
    return store.rec_ledger.evidence().maintain(
        now - rec_params.get_param(store, "choice_retention_d") * 86400,
        protected_artifacts=protected, protected_runs={live.get('run_id'),
            (store.rec_proposals.get_proposals(product_comparison.MONITOR) or {}).get('run_id')} - {None},
        orphan_before=now - 600)


def daily(store, now):
    storage = maintain_storage(store, now)
    report = compile_report(store, now)
    evaluation = replay(store, now)
    # Content-addressed revisions preserve corrections without inventing new trials.
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
