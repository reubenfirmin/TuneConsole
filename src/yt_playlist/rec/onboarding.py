"""Guide new accounts until useful listening offers arrive, then retain setup completion."""
import random

from yt_playlist.integrations.youtube_music_access import YouTubeMusicAccess
from yt_playlist.rec import mix_tuning, rec_params
from yt_playlist.rec.onboarding_outputs import MIN_MIXES, prepared_mixes_ready, starter_album

SETUP_COMPLETE = 'onboard_setup_complete'
LASTFM_NUDGE_SNOOZE_S = 30 * 86400
TAKEOUT_NAG_SNOOZE_S = 90 * 86400
INTEGRATION_REMINDER_DELAY_S = 7 * 86400


def reminder_due(*, eligible, dismissed_at, now, snooze):
    if not eligible:
        return False
    try:
        return now - float(dismissed_at) >= snooze
    except (ValueError, TypeError):
        return True


def integration_reminder_due(*, recommendation_at, dismissed_at, now, snooze):
    """Follow an eligible unlock recommendation one week later, retaining its snooze."""
    return reminder_due(eligible=recommendation_at is not None
                        and now >= recommendation_at + INTEGRATION_REMINDER_DELAY_S,
                        dismissed_at=dismissed_at, now=now, snooze=snooze)


def lastfm_nudge_due(store, now):
    from yt_playlist.rec import account_growth
    node = next(node for node in account_growth.view(store, now)['nodes'] if node['id'] == 'lastfm')
    return integration_reminder_due(recommendation_at=node['recommendation_at'],
        dismissed_at=store.settings.get_setting('lastfm_nudge_dismissed_at'),
        now=now, snooze=LASTFM_NUDGE_SNOOZE_S)


def takeout_nag_due(store, now):
    from yt_playlist.rec import account_growth
    node = next(node for node in account_growth.view(store, now)['nodes'] if node['id'] == 'takeout')
    return integration_reminder_due(recommendation_at=node['recommendation_at'],
        dismissed_at=store.settings.get_setting('takeout_nag_dismissed_at'),
        now=now, snooze=TAKEOUT_NAG_SNOOZE_S)


def completion(*, artists_required, remembered, mixes_ready, albums_ready, imported):
    complete = not artists_required and (remembered or _setup_complete(
        mixes_ready=mixes_ready, albums_ready=albums_ready))
    return complete, 'artists' if artists_required else 'ready' if complete else 'import' if not imported else 'learn'


def starter(store, radio, now):
    """Complete starter query, including local facts used to build its listening offer."""
    from yt_playlist.rec import home_cards, artist_start
    result = starter_kind(store, radio.bootstrap)
    if result['kind'] == 'artists':
        return {**result, 'artist_choices': store.discovery.starter_artists(),
                'song_choices': artist_start.saved_songs(store),
                'minimum_artists': artist_start.MIN_ARTISTS}
    if result['kind'] == 'albums':
        return {**result, 'album': starter_album(store)}
    if result['kind'] == 'radio':
        return {**result, 'radio_bootstrap': result['bootstrap'],
                'radio_tracks': radio.history_tracks(result['bootstrap']['state'])}
    tracks = library_sample(store, n=home_cards.PROTO_SIZE)
    # A one-song import is not a ready listening session. Once the actual import
    # has finished, a genuinely small collection can still be played.
    if len(tracks) < 3 and store.settings.get_setting('last_sync_at') is None:
        tracks = []
    return {**result, 'proto': home_cards._proto(store, 'onboard_library', 'First sounds', tracks, now)
            if tracks else None}


def sample(store, kind, clients, now):
    from yt_playlist.core.accounts import main_client
    from yt_playlist.rec import home_cards
    tracks = (radio_sample(store, main_client(store, clients()), now, n=home_cards.PROTO_SIZE)
              if kind == 'onboard_radio' else library_sample(store, n=home_cards.PROTO_SIZE))
    return home_cards._proto(store, kind, 'Something to listen to', tracks, now) if tracks else None


def status(store, now, jobs, worker):
    from yt_playlist.rec import home_preferences
    result = arrival_status(store, now)
    result['setup']['listening_format'] = home_preferences.default_format(store)
    result['setup'].update({key: value for key, value in import_progress(store, jobs, worker).items()
                            if key != 'complete'})
    return result


def import_progress(store, jobs, worker):
    """Cheap import evidence must remain available while readiness is computing."""
    job = jobs.find_active_library()
    events = job.events.since(0)[1] if job else []
    progress = next((event for _, event in reversed(events) if event.get('stage')), {})
    activity = progress.get('stage')
    current = progress.get('i', progress.get('index'))
    target = progress.get('total', progress.get('count'))
    completed = progress.get('completed', max(0, (current or 1) - 1))
    imported = store.settings.get_setting('last_sync_at') is not None
    return dict(running=bool(job), training=bool(worker and worker.busy),
        complete=store.settings.get_setting(SETUP_COMPLETE) == '1',
        imported=imported,
        step=next((event.get('text', '') for _, event in reversed(events) if event.get('text')), ''),
        activity=activity, current=current, target=target, completed=completed)


def starter_kind(store, radio_bootstrap):
    """Which First-run starter experience Home should offer right now.

    `radio_bootstrap` is a zero-arg callable so it's only invoked when the
    preferred format is actually radio, matching the prior route's laziness.
    """
    from yt_playlist.rec import artist_start, home_preferences

    if artist_start.required(store):
        return select_starter(artists_required=True)
    preference = home_preferences.default_format(store)
    if preference == 'albums':
        return select_starter(preference=preference)
    if preference == 'radio':
        bootstrap = radio_bootstrap()
        return select_starter(preference=preference, bootstrap=bootstrap)
    return select_starter(preference=preference)


def select_starter(*, artists_required=False, preference='mixes', bootstrap=None):
    if artists_required:
        return {'kind': 'artists'}
    if preference == 'albums':
        return {'kind': 'albums'}
    if preference == 'radio' and bootstrap and (
            bootstrap['seeds'] and bootstrap.get('seedMenu') or bootstrap['state'].get('active')):
        return {'kind': 'radio', 'bootstrap': bootstrap}
    return {'kind': 'library', 'radio_fallback': preference == 'radio'}


def dismiss_lastfm_nudge(store, now):
    store.settings.set_setting('lastfm_nudge_dismissed_at', str(now))


def dismiss_takeout_nag(store, now):
    store.settings.set_setting('takeout_nag_dismissed_at', str(now))


def dismiss_identities_nudge(store):
    store.settings.set_setting('identities_nudge_dismissed', '1')


def dismiss_intro_nudge(store):
    store.settings.set_setting('intro_dismissed', '1')


def dismiss_recap_nudge(store, month):
    if month:
        store.settings.set_setting('recap_dismissed_month', month)


def dismiss_update_nudge(store, version):
    dismissed = version or store.enrichment_state.release_check()[2]
    if dismissed:
        store.settings.set_setting('backend_update_dismissed_version', dismissed)


def complete(store):
    store.settings.set_setting('onboard_dismissed', '1')


def setup_completed(store) -> bool:
    """First-run completion survives disposable offers and model rebuilds.

    Older installations retain actual mix offers or a presented listening menu.
    Use that evidence to graduate them without treating fitted modes, imported
    likes or a dismissed introduction as a usable first offer.
    """
    if store.settings.get_setting(SETUP_COMPLETE) == '1':
        return True
    for surface in ('home_shown', 'choice_window:mixes', 'choice_window:albums'):
        try:
            shown = store.rec_proposals.get_proposals(surface) or {}
        except (ValueError, TypeError):
            continue
        if not isinstance(shown, dict):
            continue
        offer = None
        if surface == 'choice_window:mixes' and isinstance(shown.get('opportunity_id'), str) and shown['opportunity_id']:
            from yt_playlist.rec import choice_evidence
            offer = choice_evidence.opportunity(store, shown['opportunity_id'])
        if presented_offer(surface, shown, offer):
            return True
    return False


def presented_offer(surface, shown, offer=None):
    """An actual presented offer graduates setup; fitted/imported state alone cannot."""
    if surface == 'home_shown':
        signatures = shown.get('signatures')
        return isinstance(signatures, list) and any(isinstance(value, str) and value for value in signatures)
    identity = shown.get('opportunity_id')
    return bool(isinstance(identity, str) and identity and not (
        surface == 'choice_window:mixes' and offer is not None and len(offer.get('options', [])) < MIN_MIXES))


def _remember_completion(store, complete):
    if (complete and not getattr(store, '_read_only', False)
            and not getattr(store, 'activity_paused', False)
            and store.settings.get_setting(SETUP_COMPLETE) != '1'):
        store.settings.set_setting(SETUP_COMPLETE, '1')


def library_size(store) -> int:
    return store.rec_query.tracks_total()


def _setup_complete(*, mixes_ready, albums_ready=False) -> bool:
    # Verified songs can produce useful offers while unrelated imports still
    # need retrying. Full-sync freshness and metadata coverage aren't gates.
    return mixes_ready or albums_ready


def arrival_status(store, now=None) -> dict:
    """Local setup evidence, shared by Home and the app-wide setup banner.

    Metadata coverage is a count, never a percentage pretending to measure model training.
    Observations from incomplete imports are not ready songs.
    """
    from yt_playlist.rec import account_growth, artist_start, group_viz

    with store.read_view() as reader:
        facts = reader.readiness.arrival()
        songs, covers = facts['songs'], facts['covers']
        coverage = reader.enrichment_reports.coverage_stats()
        modes = sum(group.get('leaf', False) for group in (group_viz.prepared(reader) or {}).get('groups', ()))
        model_tracks = reader.rec_model.rec_content_vectors_count()
        discoveries = reader.rec_model.rec_discovered_content_vectors_count()
        imported = reader.settings.get_setting("last_sync_at") is not None
        import time
        growth = account_growth.view(reader, time.time() if now is None else now)
        mixes_ready = growth['ready_formats']['mixes']
        albums_ready = growth['ready_formats']['albums']
        artists_required = artist_start.required(reader)
        complete, phase = completion(artists_required=artists_required,
            remembered=not artists_required and setup_completed(reader), mixes_ready=mixes_ready,
            albums_ready=albums_ready, imported=imported)
        result = {"songs": songs, "playlists": len(reader.playlists.get_playlists()),
                "albums": len(reader.collection.get_saved_albums()),
                "covers": list(dict.fromkeys(row["thumbnail"] or
                    f"https://i.ytimg.com/vi/{row['video_id']}/hqdefault.jpg" for row in covers)),
                "outcome": reader.settings.get_setting("last_sync_outcome"),
                "setup": {"complete": complete, "phase": phase, "imported": imported,
                          "artists_required": artists_required,
                          "analyzed": coverage["processed"], "total": coverage["total"],
                          "genres": coverage["genre"], "model_tracks": model_tracks,
                          "modes": modes, "mixes_ready": mixes_ready, "albums_ready": albums_ready,
                          "offers_ready": mixes_ready or albums_ready, "discoveries": discoveries}}
    _remember_completion(store, complete)
    return result


def prepared_albums_ready(store, now=None) -> bool:
    """Album listeners can graduate independently of prepared mixes and fitted modes."""
    import time
    from yt_playlist.rec import growth_readiness
    return growth_readiness.albums(store, time.time() if now is None else now)


def initial_setup_pending(store, now=None) -> bool:
    """Keep setup until useful offers arrive once, independently of modes."""
    if setup_completed(store):
        _remember_completion(store, True)
        return False
    import time
    from yt_playlist.rec import account_growth
    growth = account_growth.view(store, time.time() if now is None else now)
    complete = _setup_complete(mixes_ready=growth['ready_formats']['mixes'],
                               albums_ready=growth['ready_formats']['albums'])
    _remember_completion(store, complete)
    return not complete


def cleanup_count(store) -> int:
    """The CACHED playlist-cleanup count (the worker / cleanup page run the O(n^2) scan and cache it;
    home only ever reads the cached number, never the scan)."""
    from yt_playlist.rec.actions import CLEANUP_SURFACE
    return (store.rec_proposals.get_proposals(CLEANUP_SURFACE) or {}).get("count", 0)


def _has_synced(store) -> bool:
    # A sync of EITHER kind has run (matches actions.sync_status). NOT enrichment-processed: an empty
    # account has nothing to enrich, but is still synced and must see onboarding (radio).
    return any(store.settings.get_setting(k) is not None for k in ("last_sync_at", "last_plays_sync_at"))


def onboarding_active(store, now) -> bool:
    """Keep a synced account in setup until useful offers arrive or it dismisses setup."""
    if setup_completed(store) or store.settings.get_setting("onboard_dismissed") == "1":
        return False
    if not _has_synced(store):
        return False
    return initial_setup_pending(store, now)


def library_sample(store, n=12) -> list[dict]:
    """A genre-diverse sample of owned tracks: round-robin across genre families, so ratings teach
    breadth. Falls back to any owned tracks when genres are sparse."""
    from yt_playlist.controllers.growth_readiness import OnboardingSamples
    return OnboardingSamples(store.readiness, random.Random()).sample(n)


def _discovered_sample(store, now, n):
    """Keep candidate selection and its duration eligibility in one local read view."""
    from yt_playlist.rec import surfaces

    mix_tuning.expire(store, now)
    if not getattr(store, '_read_only', False) and not store._lock.held_by_current_thread:
        with store.read_view() as reader:
            return _discovered_sample(reader, now, n)
    candidates = surfaces.cold_candidates(store, now, limit=n)
    metadata = store.rec_eligibility.offer_metadata([it.video_id for it in candidates])
    maximum = rec_params.max_track_duration_s(store)
    out = []
    for it in candidates:
        duration = metadata.get(it.video_id, {}).get('duration')
        if duration is not None and duration > maximum:
            continue
        out.append({"video_id": it.video_id, "title": it.title, "artist": it.artist,
                    "album": getattr(it, "album", "") or "", "thumbnail": it.thumbnail,
                    "key": it.key, "duration": duration})
    return out


def radio_sample(store, client, now, n=12) -> list[dict]:
    """Top up the locally discovered starter tracks from the account's YouTube Music Home feed."""
    if client is None:
        return []
    try:
        out = _discovered_sample(store, now, n)
    except Exception:  # noqa: BLE001 - cold pool is best-effort
        out = []
    if len(out) >= n:
        return out[:n]
    try:
        tracks = YouTubeMusicAccess.for_client(client).home_tracks()
    except Exception:  # noqa: BLE001 - keep any local tracks when the provider is unavailable
        return out
    seen = {track['video_id'] for track in out}
    maximum = rec_params.max_track_duration_s(store)
    for track in tracks:
        video, duration = track['video_id'], track['duration']
        if video in seen:
            continue
        if duration is None:
            duration = store.tracks.duration_for_video(video)
        if duration is not None and duration > maximum:
            continue
        out.append(dict(track, album='', thumbnail=None, key='', duration=duration))
        seen.add(video)
        if len(out) == n:
            break
    return out
