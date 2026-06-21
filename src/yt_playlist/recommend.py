"""Local recommendation logic. Pure functions over a Store (no web imports), like analysis.py."""
from dataclasses import dataclass

from yt_playlist import analysis

SYNC_STALE_S = 12 * 3600   # nudge to sync after 12h


@dataclass
class ForYouItem:
    title: str
    artist: str
    album: str
    video_id: str | None
    thumbnail: str | None
    plays: int
    days_since: int   # days since this song was last played


def for_you(store, now, window_days=90, min_plays=2, limit=24) -> list[ForYouItem]:
    """Tier-0 'forgotten gems': songs you played a lot but not recently."""
    rows = store.resurface_candidates(now, window_days=window_days, min_plays=min_plays, limit=limit)
    out = []
    for r in rows:
        last = r["last_played"]
        days_since = int((now - last) // 86400) if last is not None else 0
        out.append(ForYouItem(
            title=r["title"], artist=r["artist"], album=r["album"],
            video_id=r["video_id"], thumbnail=r["thumbnail"],
            plays=r["plays"], days_since=days_since))
    return out


@dataclass
class ActionItem:
    kind: str          # "auth" | "sync" | "cleanup"
    severity: str      # "high" | "med" | "low"
    title: str
    detail: str
    cta_label: str | None
    cta_href: str | None


def _ago(seconds) -> str:
    days = int(seconds // 86400)
    if days >= 1:
        return f"{days} day{'s' if days != 1 else ''} ago"
    hours = int(seconds // 3600)
    if hours >= 1:
        return f"{hours} hour{'s' if hours != 1 else ''} ago"
    return "recently"


def take_action(store, now, auth_expired) -> list[ActionItem]:
    """Operational triage for the Home tab: auth, sync staleness, cleanup nudges."""
    items: list[ActionItem] = []
    for label in auth_expired.values():
        items.append(ActionItem(
            "auth", "high", f"Re-authenticate {label}",
            "YouTube session expired — sync and recommendations are stale until you reconnect.",
            "Re-authenticate", "/setup"))

    last = store.get_setting("last_sync_at")
    if last is None or (now - float(last)) > SYNC_STALE_S:
        when = "never" if last is None else _ago(now - float(last))
        items.append(ActionItem(
            "sync", "med", "Time to sync",
            f"Last synced {when}. Use Sync at the top of the page to refresh your library.",
            None, None))

    empties = analysis.find_empty_playlists(store)
    if empties:
        items.append(ActionItem(
            "cleanup", "low", f"{len(empties)} empty playlist(s)",
            "Empty playlists clutter your library — review and remove them.",
            "Review", "/cleanup"))

    dupes = analysis.find_near_duplicate_groups(store)
    if dupes:
        items.append(ActionItem(
            "cleanup", "low", f"{len(dupes)} near-duplicate group(s)",
            "Some playlists heavily overlap — review for merges.",
            "Review", "/cleanup"))

    return items
