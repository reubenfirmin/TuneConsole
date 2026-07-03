"""#75 Persist the extension's live now-playing reports as play events.

The cookie-era fast plays sync (30-minute get_history polls) was removed with the extension
migration; this is its replacement. Each report becomes a timestamped play_events row (the
fine-grained source of truth), also feeds the existing (track, day) history model so every
downstream consumer (charts, transient model, graduation) keeps working unchanged, stamps
last_plays_sync_at (the plays half of staleness_factor), and folds likeStatus into the
like/dislike model. The daily full sync's get_history() stays as backfill for plays that happen
while the app is not running; record_history_plays dedups per (track, date), so the two sources
never double-count a day."""
from yt_playlist.rec import graduation
from yt_playlist.util.matching import identity_key as make_key

_STATUSES = {"LIKE", "DISLIKE", "INDIFFERENT"}


def resolve_identity(store, brand_id):
    """Map a report's brand id (ytcfg DELEGATED_SESSION_ID; empty on the main account) to an
    identity id: exact brand_account_id match, else the master identity, else the first configured
    identity. None when no identities exist yet (pre-setup: nothing to attribute the play to)."""
    idents = store.get_identities()
    if not idents:
        return None
    if brand_id:
        for i in idents:
            if i.brand_account_id == brand_id:
                return i.id
    master = next((i for i in idents if i.is_master), None)
    return master.id if master else idents[0].id


def handle_play_event(ctx, msg, now) -> bool:
    """Persist one {type:'play'} bridge frame. Returns True when a NEW play was recorded (a
    same-track re-report, e.g. a likeStatus change, merges into the previous event instead)."""
    title = (msg.get("title") or "").strip()
    artist = (msg.get("artist") or "").strip()
    if not title:
        return False
    store = ctx.store
    ident = resolve_identity(store, (msg.get("brandId") or "").strip())
    if ident is None:
        return False
    key = make_key(title, artist)
    status = (msg.get("likeStatus") or "").strip()
    new = store.record_play_event(ident, key, (msg.get("videoId") or None), now,
                                  playlist_ytm_id=(msg.get("playlist") or None),
                                  like_status=(status or None))
    if new:
        store.record_history_plays(ident, now, [key])       # keep the (track, day) model current
        store.set_setting("last_plays_sync_at", str(now))   # plays-freshness for staleness_factor
    if status in _STATUSES:
        graduation.apply_dislikes(store, {key: status}, now)    # idempotent by design
    return new
