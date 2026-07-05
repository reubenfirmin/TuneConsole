"""Home action cards and the sync-status badge: things needing attention (re-auth, cleanup,
enrichment) plus the 'last synced' freshness/staleness badge."""
from dataclasses import dataclass, field

from yt_playlist.library import analysis
from yt_playlist.util.duration import ago as _ago
from yt_playlist.rec import rec_params


SYNC_STALE_S = rec_params.SYNC_STALE_S   # highlight the Sync card after this (defined in rec_params)
# #85: this is display-only copy escalation (badge text), not model math -- the transient model no
# longer has a sync-staleness relax; every event decays on its own wall clock instead (transient.py).
# A fixed 3-day wait past "stale" before switching to the more alarming "drifting" message.
_URGENT_AFTER_S = 3 * 86400


@dataclass
class SyncStatus:
    last_synced_ago: str | None   # None if never synced
    stale: bool                   # never synced, or older than SYNC_STALE_S
    message: str | None           # highlight copy when stale, else None
    urgent: bool = False          # stale long enough to escalate the badge copy (display only, #85)


def sync_status(store, now) -> SyncStatus:
    # "Last synced" reflects the most recent sync of EITHER kind: a quick plays/auto sync keeps your
    # plays current just as a full sync does, so the badge must not claim you synced longer ago than
    # you actually did. Staleness rides the same most-recent stamp: recent plays = not stale.
    stamps = [float(s) for s in (store.get_setting("last_sync_at"),
                                 store.get_setting("last_plays_sync_at")) if s is not None]
    if not stamps:
        return SyncStatus(None, True, "Sync to pull in your library and recommendations.")
    age = now - max(stamps)
    if age > SYNC_STALE_S:
        if age > SYNC_STALE_S + _URGENT_AFTER_S:
            return SyncStatus(_ago(age), True,
                              f"We haven't seen your plays in {_ago(age)}. Your recommendations are "
                              "drifting. Sync now.", urgent=True)
        return SyncStatus(_ago(age), True, "It's been a while. Sync to refresh.")
    return SyncStatus(_ago(age), False, None)


@dataclass
class ActionItem:
    kind: str          # "auth" | "cleanup" | "enrich"
    severity: str      # "high" | "med" | "low"
    title: str
    detail: str
    cta_label: str | None
    cta_href: str | None
    thumbnail: str | None = None
    thumbnails: list = field(default_factory=list)   # 0..2 covers for cards that show several playlists
    key: str = ""      # stable id for dismiss/snooze (e.g. 'enrich:12', 'cleanup:all')
    note: str = ""     # one-line orienting summary for the card (count + why); detail is the full text
    badge: str = ""    # tiny count chip shown beside the CTA (the number); detail is its tooltip


CLEANUP_SURFACE = "cleanup"


def refresh_cleanup(store, now=None) -> dict:
    """Recompute the playlist-cleanup summary and cache it as a rec proposal (last-good serving).

    This is the ONLY place the heavy O(n²) cleanup scan runs for the home card: the rec worker calls
    it on every rebuild (so it tracks the playlist changes a sync brings in) and the /cleanup page
    calls it after every edit (its mutations HX-Refresh back through the GET). take_action then just
    reads the cached number. The home page never pays for the scan."""
    payload = analysis.cleanup_summary(store).as_payload()
    store.put_proposals(CLEANUP_SURFACE, payload, now)
    return payload


def take_action(store, now, auth_expired) -> list[ActionItem]:
    """Cards for things that genuinely need attention. Empty list = render nothing.

    Honors per-card snooze: an alert dismissed by the user stays hidden until its cooldown.
    """
    snoozed = store.suppressed_keys("alert", now)
    items: list[ActionItem] = []
    for label in auth_expired.values():
        items.append(ActionItem(
            "auth", "high", f"Sign in to YouTube Music ({label})",
            "You are signed out of YouTube Music. Open music.youtube.com and sign in; the extension "
            "picks the session back up and sync resumes.",
            "Open YouTube Music", "https://music.youtube.com", key=f"auth:{label}",
            note="Signed out - sync is paused", badge="!"))

    # Read the cached summary the rec worker / cleanup page materialize. Never scan on home load.
    cleanup = store.get_proposals(CLEANUP_SURFACE) or {}
    n = cleanup.get("count", 0)
    if n:
        items.append(ActionItem(
            "cleanup", "low", "Playlist cleanups",
            f"{n} playlist(s) look like duplicates, overlaps, or clutter - review and tidy them up "
            "on the cleanup page.",
            "Review", "/cleanup", thumbnails=cleanup.get("thumbnails", []), key="cleanup:all",
            note="Duplicates, overlaps & clutter to review", badge=str(n)))

    # Manual-enrichment nag cards were removed: the auto-enrich worker drains the library in the
    # background, so there is nothing to nag the user to enrich by hand here anymore.

    return [i for i in items if i.key not in snoozed]
