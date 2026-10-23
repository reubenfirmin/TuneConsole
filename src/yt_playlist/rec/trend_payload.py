"""Recap, health and artwork presentation built on the rollup's private snapshot reader."""
from datetime import datetime, timezone
from urllib.parse import quote

from yt_playlist.rec import recap_details, recap_story, trend_insights, trend_statistics as stats

REDISCOVER_QUIET_DAYS = 90
_STALE_BUCKETS = (("played <30d", 30), ("30-90d", 90), ("90-365d", 365), (">365d", None))


def month_review(months, store, now, day_counts=(), meta=None):
    """Previous complete UTC month, with ledger-preferred plays and catalog-relative discovery.

    All reads use the caller's snapshot and simulated clock, including podium metadata and art.
    Coarse history remains the fresh-install fallback when no exact-event ledger exists.
    """
    meta = meta or {}
    month = stats.last_complete_month(now)
    cur = next((m for m in months if m["month"] == month), None)
    if cur is None:
        return None
    y, mo = (int(x) for x in cur["month"].split("-"))
    start = datetime(y, mo, 1, tzinfo=timezone.utc).timestamp()
    end = datetime(y + (mo == 12), (mo % 12) + 1, 1, tzinfo=timezone.utc).timestamp()
    pstart = datetime(y - (mo == 1), ((mo - 2) % 12) + 1, 1, tzinfo=timezone.utc).timestamp()
    # GENERATED_GROUP quarantine applies to the ledger; charts retain their own day semantics.
    ledger = store.trends.has_play_ledger()
    if ledger:
        this = store.trends.ledger_artist_plays(start, end)
        prev = store.trends.ledger_artist_plays(pstart, start)
    else:
        this = store.charts.listen_distribution("artist", since=start, until=end)
        prev = store.charts.listen_distribution("artist", since=pstart, until=start)
    deltas = {a: this.get(a, 0) - prev.get(a, 0) for a in set(this) | set(prev)}
    riser = max(deltas.items(), key=lambda kv: kv[1], default=None)
    faller = min(deltas.items(), key=lambda kv: kv[1], default=None)
    afp = store.trends.first_play_map("artist")
    catalog = store.trends.catalog_artists()
    new_this = {a: this.get(a, 0) for a, day in afp.items()
                if stats.month_of(day) == cur["month"] and a not in catalog}
    top_new = max(new_this.items(), key=lambda kv: kv[1], default=None)
    top_artists = [{"artist": a, "plays": n, "art": store.charts.artist_thumbnail(a)}
                   for a, n in sorted(this.items(), key=lambda kv: (-kv[1], kv[0]))[:3]]
    mtp = (store.trends.ledger_track_plays(start, end) if ledger
           else store.trends.month_track_plays(start, end))
    tk = max(mtp.items(), key=lambda kv: (kv[1], kv[0]), default=None)
    if tk:
        card = store.trends.track_cards([tk[0]]).get(tk[0], {})
        top_track = {"track": tk[0], "title": card.get("title"), "artist": card.get("artist", ""),
                     "plays": tk[1], "art": card.get("thumbnail")}
    else:
        top_track = None
    return {"month": cur["month"], "plays": cur["plays"], "listen_days": cur["listen_days"],
            "longest_streak": cur["longest_streak"],
            "top_new_artist": ({"artist": top_new[0], "plays": top_new[1]}
                               if top_new and top_new[1] > 0 else None),
            "riser": ({"artist": riser[0], "delta": riser[1]} if riser and riser[1] > 0 else None),
            "faller": ({"artist": faller[0], "delta": faller[1]} if faller and faller[1] < 0 else None),
            "play_days": stats.month_day_counts(day_counts, cur["month"]) if day_counts else [],
            "first_weekday": (datetime(y, mo, 1, tzinfo=timezone.utc).weekday() + 1) % 7,
            "top_artists": top_artists, "top_track": top_track,
            "binge": stats.month_peak_day(day_counts, meta, cur["month"]) if day_counts else None}


def health_snapshot(store, now):
    total, never = store.trends.never_played()
    counts = {label: 0 for label, _ in _STALE_BUCKETS}
    counts["never"] = 0
    for _key, last in store.trends.track_last_play():
        if last is None:
            counts["never"] += 1
            continue
        age_days = (now - last) / 86400.0
        for label, cap in _STALE_BUCKETS:
            if cap is None or age_days < cap:
                counts[label] += 1
                break
    staleness = [{"bucket": label, "n": counts[label]} for label, _ in _STALE_BUCKETS]
    staleness.append({"bucket": "never", "n": counts["never"]})
    quiet_before = now - REDISCOVER_QUIET_DAYS * 86400
    rediscover = store.trends.rediscover_tracks(quiet_before, limit=3)
    recency = store.collection.saved_albums_recency()
    saved = {a["browse"]: a for a in store.collection.get_saved_albums()}
    unopened = [saved[b] for b, ts in recency.items() if ts is None and b in saved]
    unopened_albums = {"count": len(unopened),
                       "items": [{"browse_id": a["browse"], "title": a["title"],
                                  "artist": a["artist"], "art": a.get("thumbnail")}
                                 for a in unopened[:3]]}
    return {"total_tracks": total, "never_played": never,
            "never_played_share": (never / total) if total else 0.0,
            "staleness": staleness, "dead_playlists": store.trends.dead_playlists(max_listens=0),
            "rediscover": rediscover, "unopened_albums": unopened_albums}


def bake_art(store, insights, now):
    """Batch track cards and artist art, and name track subjects in display headlines."""
    track_keys = [i["subject"] for i in insights if i["subject_kind"] == "track"]
    cards = store.trends.track_cards(track_keys)
    out = []
    for i in insights:
        if i["subject_kind"] == "track":
            card = cards.get(i["subject"], {})
            artist = i["artist"] or card.get("artist", "")
            title = card.get("title")
            anchor = (f"/album?browse={card['album_browse_id']}" if card.get("album_browse_id")
                      else f"/artist?name={quote(artist)}")
            headline = i["headline"]
            if title and i["kind"] == "repetition":
                headline = f'"{title}" is hooking in'
            elif title and i["kind"] == "song_of_week":
                headline = f"Song of the week: {title}"
            i = {**i, "title": title, "artist": artist, "art": card.get("thumbnail"), "headline": headline,
                 "anchor": anchor, "cta": "Open the album" if card.get("album_browse_id") else "See the artist"}
        else:
            i = {**i, "art": store.charts.artist_thumbnail(i["artist"]),
                 "anchor": f"/artist?name={quote(i['artist'])}", "cta": "Explore the catalog"}
        out.append({**i, "computed_at": now})
    return out


def assemble(store, now, day_counts, meta, weeks, months, ranked_insights, floor_day):
    """Prepare the existing published JSON shape; this owner never publishes or mutates settings."""
    insights = bake_art(store, ranked_insights, now)
    review = month_review(months, store, now, day_counts, meta)
    days = {day for day, _key, _cnt in day_counts}
    review_month = store.settings.get_setting("trend_spotlight_review_month")
    spotlight = trend_insights.detect_spotlight(
        weeks, months, review, floor_day, review_month, now, days=days, insights=insights)
    month_name = (datetime.strptime(review["month"] + "-01", "%Y-%m-%d").strftime("%B")
                  if review else None)
    details = recap_details.build(store, review["month"]) if review else []
    story = recap_story.build_story(store, months, review, insights, month_name, detail_beats=details)
    return {"built_at": now, "first_play_floor_day": floor_day, "weeks": weeks, "months": months,
            "overlays": stats.compute_overlay_series(day_counts, meta, weeks),
            "insights": insights, "review": review, "health": health_snapshot(store, now),
            "story": story, "spotlight": spotlight}
