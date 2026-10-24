"""#75 Persist the extension's live now-playing reports as play events.

The cookie-era fast plays sync (30-minute get_history polls) was removed with the extension
migration; this is its replacement. Each report becomes a timestamped play_events row (the
fine-grained source of truth), also feeds the existing (track, day) history model so every
downstream consumer (charts, transient model, graduation) keeps working unchanged, stamps
last_plays_sync_at (#85: feeds the sync-status display badge, actions.sync_status; the
transient model no longer damps on sync staleness, each event decays on its own wall clock),
and folds likeStatus into the like/dislike model. The daily full sync's get_history() stays as
backfill for plays that happen while the app is not running; record_history_plays dedups per
(track, date), so the two sources never double-count a day."""
from yt_playlist.library import ratings
from yt_playlist.util.matching import identity_key as make_key

# Only explicit ratings act from the player pipeline. INDIFFERENT is deliberately excluded: the
# player's like-button readout shows INDIFFERENT before YTM hydrates the real rating, so folding it
# in would wrongly clear a genuine like (which then re-records on the next sync, the flap that
# ratcheted graduation). The authoritative un-like is the library sync's whole-run INDIFFERENT.
_STATUSES = {"LIKE", "DISLIKE"}


def resolve_identity(store, brand_id):
    """Map a report's brand id (ytcfg DELEGATED_SESSION_ID; empty on the main account) to an
    identity id. Unknown or ambiguous remote accounts cannot be attributed safely."""
    matches = [i.id for i in store.identities.get_identities()
               if (i.brand_account_id or '') == (brand_id or '')]
    return matches[0] if len(matches)==1 else None


def handle_play_event(ctx, msg, now) -> bool:
    """Persist one {type:'play'} bridge frame. Returns True when a NEW play was recorded (a
    same-track re-report, e.g. a likeStatus change, merges into the previous event instead)."""
    title = (msg.get("title") or "").strip()
    artist = (msg.get("artist") or "").strip()
    if (not title or msg.get("metadataVerified") is False or msg.get("paused")
            or msg.get("muted") or msg.get("tabMuted") or msg.get("volume") == 0):
        return False
    store = ctx.store
    ident = resolve_identity(store, (msg.get("brandId") or "").strip())
    if ident is None:
        return False
    key = make_key(title, artist)
    if msg.get('videoId'):
        key = store.tracks.observe_track(msg['videoId'],title,artist,now=now)
    status = (msg.get("likeStatus") or "").strip()
    rating = store.tracks.item_rating(ident, msg.get("videoId"))
    confirmed = rating is not None and rating["source"] == "api"
    if confirmed:
        # An API write does not refresh YTM's player bar. Its old readout must not undo the
        # confirmed result; another confirmed action or a library sync can reconcile it.
        status = rating["status"]
    from yt_playlist.rec import choice_evidence
    context_choice = choice_evidence.album_playback_context(store, msg, now, identity_id=ident)
    new = store.history.record_play_event(ident, key, (msg.get("videoId") or None), now,
                                  playlist_ytm_id=(msg.get("playlist") or None),
                                  like_status=(status or None),source_timezone=msg.get('timeZone'),
                                  context_choice=bool(context_choice))
    if new:
        store.settings.set_setting("last_plays_sync_at", str(now))   # plays-freshness for the sync-status badge
        choice_evidence.record_playback(store, msg, now, identity_id=ident)
    # A title alone cannot attribute an account's rating to an exact upload.
    if status in _STATUSES and msg.get('videoId'):
        if not confirmed:
            ratings.record_observations(store, ident, {msg['videoId']: status},
                                        source='action', now=now)
        else:
            ratings.reconcile_recordings(store, {key})
    return new
