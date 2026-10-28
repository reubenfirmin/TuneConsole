"""Pure monthly measurements from genuine, timestamped plays.

Account calendar days match the recap totals. Sessions use real elapsed timestamps;
they measure tracks per run, never listening duration. Missing months remain missing.
"""
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from statistics import median

from yt_playlist.util import genre_map

SESSION_GAP = 30 * 60
MIN_PLAYS = 20
MIN_DAYS = 4


def shift_month(month, offset):
    year, number = map(int, month.split("-"))
    index = year * 12 + number - 1 + offset
    return f"{index // 12:04d}-{index % 12 + 1:02d}"


def month_bounds(month):
    def stamp(value):
        return datetime.strptime(value, "%Y-%m").replace(tzinfo=timezone.utc).timestamp()
    return stamp(month), stamp(shift_month(month, 1))


def calendar_day(row, field="played_at"):
    """The account's projected date; detached UTC-only inputs use their UTC date."""
    return row.get("day") or datetime.fromtimestamp(row[field], timezone.utc).date().isoformat()


def day_number(row):
    return (date.fromisoformat(calendar_day(row)) - date(1970, 1, 1)).days


@dataclass
class MonthProfile:
    month: str
    calendar_days: int
    source_signature: frozenset
    complete: bool
    plays: int
    days: int
    tracks: Counter = field(default_factory=Counter)
    artists: Counter = field(default_factory=Counter)
    artist_days: dict = field(default_factory=dict)
    families: Counter = field(default_factory=Counter)
    family_days: dict = field(default_factory=dict)
    decades: Counter = field(default_factory=Counter)
    decade_days: dict = field(default_factory=dict)
    cohorts: dict = field(default_factory=dict)
    sessions: tuple = ()
    album_run_plays: int = 0
    album_covered_plays: int = 0
    album_run_days: set = field(default_factory=set)
    playlists: int = 0

    @property
    def eligible(self):
        return self.complete and self.plays >= MIN_PLAYS and self.days >= MIN_DAYS


def profile(month, events, meta, spans, playlists):
    since, until = month_bounds(month)
    rows = [p for p in events if calendar_day(p)[:7] == month]
    signature = frozenset((p["identity_id"], p["source"]) for p in rows)
    complete = bool(signature) and all(
        source in {"live", "takeout"} and spans[(identity, source)][0] < since
        and spans[(identity, source)][1] >= until for identity, source in signature)
    result = MonthProfile(month, round((until - since) / 86400), signature, complete,
                          len(rows), len({calendar_day(p) for p in rows}))
    groups = {"artist": defaultdict(set), "family": defaultdict(set), "decade": defaultdict(set)}
    by_identity = defaultdict(list)
    for p in rows:
        key = p["identity_key"]
        card = meta.get(key, {})
        if card.get("album_browse_ids") or card.get("album_browse_id"):
            result.album_covered_plays += 1
        day = day_number(p)
        result.tracks[key] += 1
        artist = card.get("artist")
        family = genre_map.family(card["genre"]) if card.get("genre") else None
        year = str(card.get("mb_year", ""))[:4]
        decade = f"{int(year) // 10 * 10}s" if year.isdigit() and 1900 <= int(year) <= 2100 else None
        for name, value, counts in (("artist", artist, result.artists),
                                     ("family", family, result.families),
                                     ("decade", decade, result.decades)):
            if value:
                counts[value] += 1
                groups[name][value].add(day)
                result.cohorts.setdefault((name, value), set()).add(key)
        by_identity[p["identity_id"]].append(p)
    result.artist_days, result.family_days, result.decade_days = (
        dict(groups[name]) for name in ("artist", "family", "decade"))
    sessions = []
    for rows in by_identity.values():
        rows.sort(key=lambda p: p["played_at"])
        session, run, run_albums = [], [], set()

        def finish_run():
            if len({p["identity_key"] for p in run}) >= 3:
                result.album_run_plays += len(run)
                result.album_run_days.update(day_number(p) for p in run)

        for p in rows:
            gap = p["played_at"] - session[-1]["played_at"] if session else SESSION_GAP + 1
            if gap > SESSION_GAP:
                if session:
                    sessions.append(len(session))
                session = []
            card = meta.get(p['identity_key'],{})
            albums = card.get('album_browse_ids') or ({card['album_browse_id']} if card.get('album_browse_id') else set())
            common = run_albums & albums
            if gap > SESSION_GAP or not common:
                finish_run()
                run = []
                common = albums
            run_albums = common
            if albums:
                run.append(p)
            session.append(p)
        finish_run()
        if session:
            sessions.append(len(session))
    result.sessions = tuple(sessions)
    result.playlists = sum(calendar_day(p, "created_at")[:7] == month for p in playlists)
    return result


def baselines(data, month):
    """Use recorded history for the same identities, including imports and live plays.

    Source changes do not erase musical history. Capture-sensitive measurements apply
    their own stricter check; incomplete months and missing months remain excluded.
    """
    current = profile(month, data["plays"], data["meta"], data["spans"], data["playlists"])
    previous = [profile(shift_month(month, -i), data["plays"], data["meta"], data["spans"],
                        data["playlists"]) for i in range(6, 0, -1)]
    identities = {identity for identity, _ in current.source_signature}
    previous = [p for p in previous if p.eligible
                and {identity for identity, _ in p.source_signature} == identities]
    return current, previous if current.eligible and len(previous) >= 3 else []


def typical(values):
    center = median(values)
    return center, median(abs(v - center) for v in values)
