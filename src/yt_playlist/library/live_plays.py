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
from yt_playlist.library import rating_projection as ratings
from yt_playlist.core import playback_inbox as playback_feedback
from yt_playlist.util.matching import identity_key as make_key

# Only explicit ratings act from the player pipeline. INDIFFERENT is deliberately excluded: the
# player's like-button readout shows INDIFFERENT before YTM hydrates the real rating, so folding it
# in would wrongly clear a genuine like (which then re-records on the next sync, the flap that
# ratcheted graduation). The authoritative un-like is the library sync's whole-run INDIFFERENT.
_STATUSES = {"LIKE", "DISLIKE"}


def resolve_identity(store, brand_id):
    """Map a report's brand id (ytcfg DELEGATED_SESSION_ID; empty on the main account) to an
    identity id. Unknown or ambiguous remote accounts cannot be attributed safely."""
    matches = [i.id for i in store.accounts.get_accounts()
               if (i.brand_account_id or '') == (brand_id or '')]
    return matches[0] if len(matches)==1 else None


def handle_play_event(ctx, msg, now) -> bool:
    from yt_playlist.library.playback_composition import observations
    return observations(ctx.store, getattr(ctx, 'rec_worker', None)).observe('live', msg, now)


def persist_play_event(ctx, msg, now, ident, *, inline_analytics=False):
    from yt_playlist.controllers.playback_observations import Observation
    from yt_playlist.library.playback_composition import observations
    return observations(ctx.store, getattr(ctx, 'rec_worker', None)).project(
        Observation('live', ident, msg, now), inline_analytics=inline_analytics)
