"""#93 Dynamic radio: a server-side, in-process session that keeps ONE next track primed and re-picks
on every relevant player event. The picker reuses the SAME persisted-vector scoring the Home cards use
(surfaces._score_candidates, already tilted by scoring._apply_mood across the transient / session / now
layers), so ratings, plays, and the session tilt steer radio for free. The only radio-specific signal
is a session-scoped skip-penalty map (never a weight write). Everything here is cheap: persisted-vector
reads and numpy, no rebuild, safe to run on the event path."""
import random
import threading

import numpy as np

from yt_playlist.library.listen_derive import classify_exit
from yt_playlist.rec import embed, rec_params, surfaces, transient
from yt_playlist.rec.rec_dao import RecDao
from yt_playlist.util import genre_map

WATCH_URL = "https://music.youtube.com/watch?v={vid}"

DECK_LABELS = ("A", "B")
RADIO_DECK_TITLE = {"A": "TuneConsole Radio A", "B": "TuneConsole Radio B"}
RADIO_DECK_SETTING = {"A": "radio_playlist_a_ytm", "B": "radio_playlist_b_ytm"}
RADIO_PLAYLIST_SETTING = "radio_playlist_ytm"   # the v2 single-tab session's own setting key

# #93 rank-decay sampling's shared RNG: module-level so tests can inject determinism by
# monkeypatching `radio_mod._rng` to a fixed-seed random.Random(), instead of this module ever
# seeding from wall-clock time itself (which would make a test's expected sequence unreproducible).
_rng = random.Random()


def _other(label) -> str:
    return "B" if label == "A" else "A"


def _new_deck() -> dict:
    # One logical deck: its mini-playlist ytm id, its committed pick queue, the vids last reconciled
    # onto it, and the boundary vid the extension toggles at (the last pick's video id). Tab/window
    # identity for the deck (which browser tab of the dedicated radio window shows this deck) is owned
    # by the extension-side deck manager (T7j/T7k), not this pure session state; per the 2026-07-04
    # probe amendment the mechanism is a tab within a dedicated unfocused window (not a bare background
    # tab), but that window/tab id has no representation here since this dict never crosses into JS.
    return {"playlist_ytm": None, "queue": [], "applied_vids": [], "boundary_vid": None}


class RadioSession:
    """In-process radio state (one per app). Mutated from the WS worker thread (react) and the
    /radio/* request handlers, so every mutation is under `lock` (RLock: pick_next nests in react)."""

    def __init__(self):
        self.lock = threading.RLock()
        self.tilts: dict = {}
        # Per-attempt generation stamp (T7i carried fix): bumped by the /radio/start route once per
        # attempt, under `lock`, and echoed back verbatim in `deck-start`'s `"gen"`. NEVER touched by
        # reset(): it must keep climbing across stop/restart cycles so a stale deck-ready from an
        # earlier attempt can never coincide with a later one's stamp (unlike epoch, which is scoped to
        # a single session's toggles and is meant to restart at 0 each session).
        self.start_gen = 0
        self.reset()

    def reset(self, keep_tilts=False):
        self.active = False
        self.started_at = 0.0
        self.dispatched_keys: set = set()      # identity keys already played this session (no repeat)
        self.dispatched_vids: set = set()      # video ids handed to the tab (provenance stamp source)
        self.recent_radio: set = set()         # #93 cross-session freshness cooldown (keys played by a
                                                # prior radio session within radio_freshness_days), set by
                                                # start_session/start_dual_session before seeding
        self.artist_counts: dict = {}          # artist -> dispatched count (artist cap)
        self.skips: list = []                  # [(artist, mode_id, ts)] session skip-penalty events
        self.queue: list = []                  # ordered picks in the playlist (played head kept)
        self.pos: int = -1                     # index in queue of the track believed playing
        self.applied_vids: list = []           # vid list last reconciled onto the playlist
        self.primed = None                     # {key, video_id, url, title, artist} or None
        self.playlist_ytm = None               # this session's "TuneConsole Radio" ytm id
        self.dual_deck = False                 # True only after the extension confirms two decks
        self.live_label = "A"                  # which logical deck (A/B) is LIVE
        self.decks = {"A": _new_deck(), "B": _new_deck()}
        self.epoch = 0                         # bumped per confirmed toggle (toggle-race guard)
        self.standby_dirty = False             # a model shift is pending delivery to the standby deck
        # Waiting-state net: True from a "deck-waiting" pevent (a deck-play attempt was rejected,
        # blocked autoplay/MEI) until the next live play frame confirms real playback. Cleared here on
        # every reset (stop, restart, disconnect) so a stale waiting flag never survives past its session.
        self.waiting = False
        if not keep_tilts:
            self.tilts: dict = {}              # SESSION taste tilts {axis: mult}; never rec_weights
            # Fallback diagnostics (visibility wave): the reason dual-deck fell back to single-tab
            # mode, from EITHER side -- the extension's own reported reason (deck-ready
            # {fallback:true, reason}), or the server's own dual-start except path ("server: " +
            # exception class/message). None while dual is healthy or radio has never attempted dual
            # this session. Cleared on a HARD reset (stop/restart/disconnect) and by /radio/start's
            # explicit clear at attempt start, same as tilts -- but NOT by the keep_tilts=True internal
            # reseed start_session/start_dual_session run on themselves, so a reason the dual-start
            # except path just stamped survives into the v2 fallback start_session runs moments later
            # (that reset would otherwise silently wipe the very reason this wave exists to surface).
            # A gen-matched deck-ready that CONFIRMS dual also clears it -- the bridge's WS handler
            # does that directly, not through this reset.
            self.fallback_reason = None

    @property
    def live(self) -> dict:
        return self.decks[self.live_label]

    @property
    def standby(self) -> dict:
        return self.decks[_other(self.live_label)]


def _params(store) -> dict:
    gp = rec_params.get_param
    return {
        "artist_cap": int(gp(store, "radio_artist_cap")),
        "pool": int(gp(store, "radio_candidate_pool")),
        "artist_pen": float(gp(store, "radio_skip_artist_penalty")),
        "mode_pen": float(gp(store, "radio_skip_mode_penalty")),
        "halflife_h": float(gp(store, "radio_skip_halflife_h")),
        "volume_floor": float(gp(store, "radio_volume_floor")),
        "variety": float(gp(store, "radio_variety")),
    }


def _recent_radio_keys(store, now) -> set:
    """#93 cross-session freshness: track keys radio already played, across ANY prior session, within
    the last `radio_freshness_days` days. Matched by provenance: a play whose playlist_ytm_id is one of
    the radio ytm ids this install has ever used (both dual-deck settings, RADIO_DECK_SETTING["A"/"B"],
    plus the v2 single-tab RADIO_PLAYLIST_SETTING) -- the same ids playlist_watch_url stamps into every
    URL radio itself navigates to, so a play recorded while radio was driving carries one of them
    naturally. 0 (or negative) `radio_freshness_days` turns the cooldown off entirely (no query). Empty
    when none of those settings have ever been set (a fresh install with no radio history yet)."""
    days = float(rec_params.get_param(store, "radio_freshness_days"))
    if days <= 0:
        return set()
    ytm_ids = {store.get_setting(RADIO_DECK_SETTING["A"]), store.get_setting(RADIO_DECK_SETTING["B"]),
               store.get_setting(RADIO_PLAYLIST_SETTING)}
    ytm_ids.discard(None)
    ytm_ids.discard("")
    if not ytm_ids:
        return set()
    since = now - days * 86400.0
    return store.plays_by_list_ids_since(ytm_ids, since)


def _sample_ranked(ranked, variety):
    """Sample ONE winner key from `ranked` (a list of (key, adj) pairs already sorted best-first):
    P(rank i) proportional to variety**i, so rank 0 is always at least as likely as any other rank and
    higher variety flattens the tail in more often. `variety` <= 0 (the registry floor) short-circuits
    to `ranked[0][0]` with NO rng draw at all, so it reproduces the pre-#93 deterministic argmax byte-
    for-byte (existing tests that assert exact pick order pin radio_variety to 0 for exactly this
    reason). A single-candidate `ranked` also short-circuits, since there is nothing to sample among."""
    if variety <= 0 or len(ranked) == 1:
        return ranked[0][0]
    weights = [variety ** i for i in range(len(ranked))]
    total = sum(weights)
    r = _rng.random() * total
    acc = 0.0
    for (k, _adj), w in zip(ranked, weights):
        acc += w
        if r < acc:
            return k
    return ranked[-1][0]   # float rounding fallback: never leave the winner unset


def _score_map(store, now):
    """{key: taste score} over persisted collaborative vectors, tilted by the live layered model, or
    None when the model is not built yet. Mirrors surfaces.for_you's warm branch: persisted reads
    only, no rebuild."""
    if not store.rec_vectors_count():
        return None
    pt = surfaces.playlist_taste(store)
    keys, V, idx = embed.load_vectors(store)
    if not pt or V is None:
        return None
    return surfaces._score_candidates(store, pt, keys, V, idx, now)


def _modeinfo(store):
    """(cidx, CV, C, mode_ids): content-vector index + stacked active-mode centroids for nearest-mode
    skip scoping, or None when there are no modes / no content vectors / a dim mismatch (the same stale
    -modes guard as layers.now_mode_mix). None makes the mode penalty term inert, never a crash."""
    modes = store.modes.list_modes(active_only=True)
    if not modes:
        return None
    _k, CV, cidx = embed.load_content_vectors(store)
    if CV is None:
        return None
    C = np.stack([m["centroid"].astype(np.float64) for m in modes])
    if C.shape[1] != CV.shape[1]:
        return None
    return cidx, CV, C, [m["mode_id"] for m in modes]


def _nearest_mode(key, modeinfo):
    """The active mode whose centroid is nearest `key`'s content vector (argmax dot, the same rule as
    layers.now_mode_mix), or None when the key has no content vector / modeinfo is None."""
    if modeinfo is None:
        return None
    cidx, CV, C, mode_ids = modeinfo
    ci = cidx.get(key)
    if ci is None:
        return None
    return mode_ids[int((C @ CV[ci].astype(np.float64)).argmax())]


def skip_penalty(artist, mode_id, session, now, params) -> float:
    """Session-scoped penalty for a candidate: for each recorded skip, a decayed artist term (when the
    artist matches) plus a decayed nearest-mode term (when the region matches). decay is the shared
    wall-clock kernel transient.decay_weight(age, halflife_days). Never touches rec_weights."""
    pen = 0.0
    for a, m, ts in session.skips:
        w = transient.decay_weight(now - ts, params["halflife_h"] / 24.0)
        if artist and a == artist:
            pen += params["artist_pen"] * w
        if mode_id is not None and m is not None and m == mode_id:
            pen += params["mode_pen"] * w
    return pen


def _axis_info(store, keys):
    """{key: (family, sub, decade, artist)} for the session tilt, resolved exactly like
    scoring._axis_weights_for. Only called when a tilt is set (else the tilt is a neutral no-op)."""
    dao = RecDao(store)
    genres, decades, artists = dao.track_genres(keys), dao.track_decades(keys), dao.track_artists(keys)
    out = {}
    for k in keys:
        g = genres.get(k)
        fam = genre_map.family(g) if g else None
        sub = genre_map.subgenre(g) if g else None
        out[k] = (fam, sub, decades.get(k), artists.get(k))
    return out


def _tilt_mult(info, tilts) -> float:
    """Session tilt multiplier for one candidate: clamp(genre) * clamp(era) * clamp(artist), each a
    product of the matching session tilts (default 1.0), clamped to the fingerprint [GENRE_MIN,
    GENRE_MAX] range. A candidate with no value on an axis is neutral there. Never touches rec_weights."""
    fam, sub, dec, art = info
    lo, hi = rec_params.GENRE_MIN, rec_params.GENRE_MAX
    gt = tilts.get(f"genre:{fam}", 1.0) if fam else 1.0
    if sub and sub != fam:
        gt *= tilts.get(f"genre:{sub}", 1.0)
    et = tilts.get(f"era:{dec}", 1.0) if dec is not None else 1.0
    at = tilts.get(f"artist:{art}", 1.0) if art else 1.0
    clamp = lambda x: max(lo, min(hi, x))
    return clamp(gt) * clamp(et) * clamp(at)


def _exclusions(session) -> set:
    """The single no-repeat set, unioned from every place a key can currently be "spoken for":
    the v2 single-deck queue, BOTH dual-deck queues (session.decks["A"|"B"]["queue"], so deck B never
    repeats a pick deck A already committed and vice versa), session.dispatched_keys, the primed key,
    and (#93) session.recent_radio, the cross-session freshness cooldown populated at session start.

    Invariant: dispatched_keys holds ONLY tracks that actually PLAYED (the played-head fold in on_play,
    or an equivalent play-confirmation path, is the sole writer). Queue membership alone is what excludes
    a queued-but-unplayed pick; dropping a pick from whichever queue holds it (delete-rebuild, force
    top-up) de-excludes it automatically, with no separate dispatched_keys reversal required anywhere."""
    ex = {q["key"] for q in session.queue}
    ex |= {q["key"] for q in session.decks["A"]["queue"]}
    ex |= {q["key"] for q in session.decks["B"]["queue"]}
    ex |= set(session.dispatched_keys)
    ex |= set(session.recent_radio)
    if session.primed:
        ex.add(session.primed["key"])
    return ex


def pick_next(store, session, now) -> dict | None:
    """A SAMPLED eligible next track as {key, video_id, url, title, artist}, or None -- NOT a pure
    argmax (see #93). Scores the catalog with the live layered model, drops already-played keys, the
    primed key, tracks with no video id, and (cross-session freshness) `session.recent_radio` first,
    THEN ranks the top `radio_candidate_pool` of what remains and folds in the SESSION TILT
    (multiplicative) and the SESSION SKIP PENALTY (additive), plus the artist cap. Eligibility is
    filtered before the pool truncation so a session only stops when the whole catalog is exhausted,
    not when the fixed-size pool has been dispatched; the artist cap stays a diversity rule applied
    over that pool, not an eligibility filter. `_axis_info` is only resolved (extra DAO reads) when
    `session.tilts` is non-empty; an empty tilt map is a neutral no-op with no extra cost.

    RANK-DECAY SAMPLING: every candidate surviving the artist cap is kept (not just the best), ranked
    by its adjusted score descending, then ONE winner is drawn via `_sample_ranked` with P(rank i)
    proportional to `radio_variety`**i. At `radio_variety` == 0 this is a deterministic short-circuit
    to rank 0 with no rng draw at all, byte-equivalent to the pre-#93 argmax, so a session-wide taste
    ordering still means something; above 0 it is what keeps a fresh session from opening on the exact
    same handful of tracks every time, and (with skip/tilt feedback shifting the ranking underneath it)
    what makes repeated re-picks of the same underlying model still vary.

    FAIL-OPEN starvation guard: if eligibility comes back empty AND `session.recent_radio` is
    non-empty, the freshness cooldown is cleared and eligibility is recomputed once -- a small catalog
    must never make radio refuse to start (or restart) just because the cooldown ate the whole pool."""
    scores = _score_map(store, now)
    if not scores:
        return None
    params = _params(store)
    modeinfo = _modeinfo(store)

    def _eligible_ranked():
        excl = _exclusions(session)
        candidates = [k for k in scores if k not in excl]
        meta = store.tracks_by_keys(candidates)
        eligible = [k for k in candidates if meta.get(k) and meta[k].get("video_id")]
        return meta, sorted(eligible, key=lambda k: -scores[k])[:params["pool"]]

    meta, ranked = _eligible_ranked()
    if not ranked and session.recent_radio:
        session.recent_radio = set()
        meta, ranked = _eligible_ranked()

    axis_info = _axis_info(store, ranked) if session.tilts else {}
    pairs = []
    for k in ranked:
        m = meta[k]
        artist = m.get("artist")
        if artist and session.artist_counts.get(artist, 0) >= params["artist_cap"]:
            continue
        mult = _tilt_mult(axis_info[k], session.tilts) if session.tilts else 1.0
        adj = scores[k] * mult - skip_penalty(artist, _nearest_mode(k, modeinfo), session, now, params)
        pairs.append((k, adj))
    if not pairs:
        return None
    pairs.sort(key=lambda kv: -kv[1])
    best_key = _sample_ranked(pairs, params["variety"])
    m = meta[best_key]
    return {"key": best_key, "video_id": m["video_id"], "title": m.get("title"),
            "artist": m.get("artist"), "url": WATCH_URL.format(vid=m["video_id"])}


RADIO_PLAYLIST_TITLE = "TuneConsole Radio"


def playlist_watch_url(vid, playlist_ytm) -> str:
    # ALWAYS carries &list=: the queue is our playlist, never YouTube's radio.
    return f"https://music.youtube.com/watch?v={vid}&list={playlist_ytm}"


def upcoming_picks(session, limit=8) -> list:
    """Up to `limit` upcoming tracks as [{"title", "artist"}, ...], for the /bridge/status "Up next"
    feedback loop (visibility wave): the one place the owner can SEE Populate / a steer tweak actually
    changed something, since v2's YTM tab queue is a frozen snapshot until navigation and dual's live
    deck queue is likewise frozen until the next toggle. Empty when radio is not active.

    v2 single-tab: session.queue beyond session.pos (exclusive of the now-playing track itself) --
    exactly what on_play's tail rebuild / force_topup just changed.

    Dual mode: `session.pos` is ALSO the live deck's own queue index here (toggle_decks resets it to 0
    on every swap; `_on_play_dual` advances it within `session.live["queue"]`, never `session.queue`,
    which dual mode leaves empty) -- so the live deck's remaining queue is `live["queue"][pos+1:]`,
    same slice shape as v2. The standby deck's queue is appended after it (it is the very thing
    rebuild_standby / a dual steer tweak just changed, and would otherwise never show up here since it
    is invisible in YTM until the next toggle promotes it)."""
    if not getattr(session, "active", False):
        return []
    if session.dual_deck:
        picks = session.live["queue"][session.pos + 1:] + session.standby["queue"]
    else:
        picks = session.queue[session.pos + 1:]
    return [{"title": p.get("title"), "artist": p.get("artist")} for p in picks[:limit]]


def note_dispatch(session, pick) -> None:
    """Mark `pick` committed into the queue: artist cap only. Being PLAYED (dispatched_keys) is folded
    later by on_play when pos advances past it."""
    if pick.get("artist"):
        session.artist_counts[pick["artist"]] = session.artist_counts.get(pick["artist"], 0) + 1


def _pick_tail(store, session, now, depth, target=None) -> list:
    """Up to `depth` fresh picks appended to `target` (defaults to session.queue for the v2 single-tab
    path). Each pick is committed into the artist-cap tally via note_dispatch, then appended to `target`.
    `target` is one of session.queue / session.decks["A"]["queue"] / session.decks["B"]["queue"], and
    _exclusions unions all three, so appending here is itself what keeps a later pick (in this same call,
    or a later call seeding the OTHER deck) from repeating an earlier one: no dispatched_keys write is
    needed for that, since dispatched_keys is played-only (see _exclusions). Returns the picks appended
    (may be shorter than depth, or empty, at catalog exhaustion)."""
    if target is None:
        target = session.queue
    added = []
    for _ in range(depth):
        pick = pick_next(store, session, now)
        if pick is None:
            break
        note_dispatch(session, pick)
        target.append(pick)
        added.append(pick)
    return added


def start_session(store, session, now) -> dict | None:
    """Begin a session and seed the queue. Pure w.r.t. network: the bridge performs the playlist
    create/reconcile + navigate + prime from the returned plan. Returns None (fail-open) if nothing is
    pickable yet.

    #93: populates session.recent_radio (the cross-session freshness cooldown) BEFORE seeding, so the
    very first pick already excludes whatever radio played recently in a prior session."""
    session.reset(keep_tilts=True)
    session.active = True
    session.started_at = now
    session.recent_radio = _recent_radio_keys(store, now)
    depth = int(rec_params.get_param(store, "radio_seed_depth"))
    seeds = _pick_tail(store, session, now, depth)
    if not seeds:
        session.reset()
        return None
    session.pos = 0
    session.primed = session.queue[1] if len(session.queue) > 1 else None
    return {"seed_vids": [p["video_id"] for p in session.queue],
            "first": session.queue[0], "primed": session.primed}


def _seed_deck(store, session, deck, now, depth) -> list:
    """Seed one deck's queue with up to `depth` picks; set its boundary_vid to the last pick (the
    toggle trigger). Uses the session-wide exclusion set (via _pick_tail) so decks never share a
    track."""
    picks = _pick_tail(store, session, now, depth, target=deck["queue"])
    deck["boundary_vid"] = deck["queue"][-1]["video_id"] if deck["queue"] else None
    return picks


def start_dual_session(store, session, now) -> dict | None:
    """Begin a DUAL-deck session: seed deck A (live) and deck B (standby) with DISJOINT picks (deck B
    excludes deck A's picks via _exclusions' queue-union: deck B's picker sees deck A's committed queue
    directly, not via dispatched_keys). `dual_deck` is set True here provisionally; the extension
    confirms two real decks exist before anything is armed on that side. Pure w.r.t. network: the bridge
    reconciles both mini playlists by ytm id and sends deck-start from the returned plan.

    Fail-open, all the way to None, on either seed being empty: shipping dual_deck with an empty standby
    (deck B) is a real gap, not just deck A. The bridge's dual-start attempt (T7h) only branches on
    `dplan is None` to fall back to the v2 single-tab `start_session` path; it does not know how to
    consume any other partial shape. So on EITHER deck failing to seed, this fully unwinds
    (session.reset()) and returns None, exactly like the existing deck-A-empty case, rather than
    inventing a second "shape" the bridge would have to special-case.

    #93: populates session.recent_radio (the cross-session freshness cooldown) BEFORE seeding either
    deck, same as start_session."""
    session.reset(keep_tilts=True)
    session.active = True
    session.dual_deck = True
    session.started_at = now
    session.recent_radio = _recent_radio_keys(store, now)
    depth = int(rec_params.get_param(store, "radio_deck_size"))
    a = _seed_deck(store, session, session.decks["A"], now, depth)
    if not a:
        session.reset()
        return None
    b = _seed_deck(store, session, session.decks["B"], now, depth)
    if not b:
        session.reset()
        return None
    session.live_label = "A"
    session.pos = 0

    def _plan(label):
        d = session.decks[label]
        return {"playlist_key": label, "vids": [p["video_id"] for p in d["queue"]],
                "first": d["queue"][0] if d["queue"] else None, "boundary": d["boundary_vid"]}

    return {"live": _plan("A"), "standby": _plan("B")}


def _uncommit_deck(session, deck) -> None:
    """Reverse note_dispatch's artist-cap bookkeeping for every pick in `deck`'s queue, so those picks
    are eligible again once the queue itself is cleared (mirrors _rebuild_tail_at's dropped-pick
    handling). dispatched_keys is NOT touched here: under the single-source exclusion model it is
    played-only (see _exclusions), and note_dispatch never wrote to it either, so a queued-but-never-
    played pick was never a member to begin with. In particular, when this is called on a deck that just
    got folded by toggle_decks (its keys are now legitimately in dispatched_keys because they PLAYED),
    discarding them here would wrongly re-open already-played tracks."""
    for q in deck["queue"]:
        artist = q.get("artist")
        if artist and artist in session.artist_counts:
            session.artist_counts[artist] -= 1
            if session.artist_counts[artist] <= 0:
                del session.artist_counts[artist]


def rebuild_standby(store, session, now) -> dict | None:
    """Rebuild the standby deck from the freshest model: full delete-rebuild, same shape as
    _rebuild_tail_at but for a whole deck instead of a queue tail. Returns an apply plan stamped with
    the CURRENT epoch only when the standby's vids actually changed, else None (nothing to apply).
    Caller holds nothing; the fn takes session.lock. Never raises.

    Fail-open on total catalog exhaustion: if re-seeding yields nothing at all while the standby
    previously held a working queue, that would ship an empty standby playlist for no benefit (the old
    picks are still perfectly valid, nothing fresher exists to replace them with) -- so this rolls the
    uncommit back, restores the prior queue verbatim, and reports no change, mirroring
    start_dual_session's fail-open unwind rather than degrading a good deck to nothing."""
    try:
        with session.lock:
            if not session.active or not session.dual_deck:
                return None
            standby = session.standby
            label = _other(session.live_label)
            prior_queue = list(standby["queue"])
            prior_vids = [p["video_id"] for p in prior_queue]
            _uncommit_deck(session, standby)
            standby["queue"] = []
            depth = int(rec_params.get_param(store, "radio_deck_size"))
            _seed_deck(store, session, standby, now, depth)
            if not standby["queue"] and prior_queue:
                standby["queue"] = prior_queue
                for q in prior_queue:
                    note_dispatch(session, q)
                standby["boundary_vid"] = prior_queue[-1]["video_id"]
                session.standby_dirty = False
                return None
            session.standby_dirty = False
            vids = [p["video_id"] for p in standby["queue"]]
            if vids == prior_vids:
                return None
            first = standby["queue"][0] if standby["queue"] else None
            return {"playlist_key": label, "vids": vids, "first": first,
                    "boundary": standby["boundary_vid"], "epoch": session.epoch}
    except Exception:
        return None


def toggle_decks(session) -> dict:
    """A confirmed local toggle: swap live/standby, bump epoch, fold the old live deck's queue into
    dispatched_keys (every one of those picks PLAYED, since the toggle only fires at the deck's
    boundary_vid), reset pos, and mark the new standby (the old live tab) dirty for a fresh rebuild.
    Returns {new_live, epoch}. Never raises."""
    try:
        with session.lock:
            old_live = session.live
            for q in old_live["queue"]:
                session.dispatched_keys.add(q["key"])
            session.live_label = _other(session.live_label)
            session.epoch += 1
            session.pos = 0
            session.standby_dirty = True
            return {"new_live": session.live_label, "epoch": session.epoch}
    except Exception:
        return {"new_live": session.live_label, "epoch": session.epoch}


def _on_play_dual(session, vid) -> dict:
    """Dual-deck play handling (caller holds session.lock; session.active and session.dual_deck are
    true). The live deck's snapshot is frozen in DUAL mode, so unlike v2's on_play this does NOT delete-
    rebuild the tail: it only advances `pos` within the live deck's already-committed queue and folds the
    played head into dispatched_keys (the sole no-repeat writer). Reactivity to a fresher model lives on
    the STANDBY side (rebuild_standby), not here. Returns {foreign, at_boundary, standby_dirty}, the
    dual plan shape the bridge (T7g) branches on via the presence of "at_boundary".

    `at_boundary` is True when `vid` IS the live deck's boundary_vid (the last committed pick): the
    bridge uses this, combined with `standby_dirty`, to fire an eager rebuild_standby BEFORE the toggle
    (so the new live deck is fresh the instant it goes live).

    `foreign` is the S2 toggle-trigger signal: the bridge unconditionally sends `deck-toggle` whenever
    it is True (see T7g), so this function -- not the bridge -- is responsible for gating it. A vid
    outside the live deck's queue is ambiguous on its own: it is the expected shape of BOTH (a) YTM
    autoplay leaking into the live tab right after the boundary track finished (the real toggle trigger),
    and (b) the user simply browsing away mid-deck to something of their own choosing. Distinguishing
    them requires knowing what was playing just before this frame: only case (a) is preceded by the
    boundary track already being the position we last confirmed. So `foreign` is set True ONLY when the
    session was already sitting AT the boundary when this frame arrived; otherwise it is False and the
    frame is treated as an inert do-not-fight no-op, same as v2's unrecognized-vid branch. Getting this
    gate wrong in the permissive direction (signalling foreign on every off-queue vid) would let an early
    mid-deck browse-away toggle the decks and fold the live deck's entire unplayed tail into
    dispatched_keys as though it had played, permanently poisoning no-repeat for tracks that never
    played. Never touches session.queue/pos/dispatched_keys in the foreign branch: nothing new played."""
    live = session.live
    q = live["queue"]
    prior_pos = session.pos
    was_at_boundary = (live["boundary_vid"] is not None and 0 <= prior_pos < len(q)
                        and q[prior_pos]["video_id"] == live["boundary_vid"])
    idx = next((i for i, p in enumerate(q) if p["video_id"] == vid), None)
    if idx is None:
        return {"foreign": was_at_boundary, "at_boundary": False,
                "standby_dirty": session.standby_dirty}
    # Fold the played head (everything strictly before the new position) into no-repeat.
    for p in q[max(prior_pos, 0):idx]:
        session.dispatched_keys.add(p["key"])
    session.pos = idx
    at_boundary = (live["boundary_vid"] is not None and vid == live["boundary_vid"])
    return {"foreign": False, "at_boundary": at_boundary, "standby_dirty": session.standby_dirty}


def on_play(store, session, vid, now) -> dict:
    """A play frame: `vid` is now playing. If it is one of our queued picks AT OR AFTER the current
    position, advance pos (folding the played head into dispatched_keys, the ONLY place dispatched_keys
    is ever written), then DELETE the unplayed tail (`queue[pos+1:]`) and unconditionally rebuild it with
    `_pick_tail(radio_seed_depth)` fresh picks from the CURRENT model. Every dropped pick has its
    `note_dispatch` bookkeeping (the artist cap tally) reversed so it is eligible again for the rebuild
    (it was never in dispatched_keys to begin with, since it was queued but never played): this is what
    lets a skip recorded since a pick was queued actually purge it (a skip should steer what comes next,
    including picks already queued but not yet played, not just future picks). A vid EARLIER than the
    current position is a backward jump (rewind / replay), not an advance, and is an inert no-op: see the
    guard below. Returns {desired_vids, prime}; desired_vids is None when the playlist membership did not
    change (`_pick_tail` is deterministic given the model, so an unchanged model reproduces the same
    tail). Does NOT commit `session.applied_vids` itself: that commit is the caller's job, made only once
    the reconcile it drives from `desired_vids` actually succeeds (see bridge._radio_apply), so a failed
    reconcile leaves `applied_vids` stale and the identical `desired_vids` is retried on the next play
    frame instead of being silently treated as already-applied. Never raises."""
    try:
        with session.lock:
            if not session.active:
                return {"desired_vids": None, "prime": None}
            if session.dual_deck:
                return _on_play_dual(session, vid)
            idx = next((i for i, q in enumerate(session.queue) if q["video_id"] == vid), None)
            if idx is None:
                return {"desired_vids": None, "prime": None}   # user navigated off our queue: do not fight
            if idx < session.pos:
                # Backward jump (rewind / replay an earlier queued track): do NOT fold or delete-rebuild.
                # Folding here would be wrong (nothing new played), and rebuilding at `idx` would drop
                # the already-played/queued tail between idx and the current pos, destroying history and
                # future picks the user has not even reached. Same "do not fight navigation" inert
                # response as an unrecognized vid.
                return {"desired_vids": None, "prime": None}
            # Fold the played head (everything strictly before the new position) into no-repeat.
            for q in session.queue[max(session.pos, 0):idx]:
                session.dispatched_keys.add(q["key"])
            session.pos = idx
            return _rebuild_tail_at(store, session, now, idx)
    except Exception:
        return {"desired_vids": None, "prime": None}


def _rebuild_tail_at(store, session, now, idx) -> dict:
    """Delete-then-rebuild the unplayed tail after position `idx` (caller holds session.lock): drop
    `queue[idx+1:]`, reverse note_dispatch's bookkeeping (the artist cap count) for each dropped pick,
    top back up to radio_seed_depth from the CURRENT model, and re-prime. Shared by on_play (advance +
    rebuild) and force_topup (rebuild in place). Nothing needs to touch dispatched_keys here: it is
    played-only (written solely by on_play's played-head fold), so a pick that was queued but never
    played was never added to it in the first place. Simply dropping the pick from `session.queue` is
    what makes it eligible again, since `_exclusions` excludes it via queue membership alone."""
    dropped = session.queue[idx + 1:]
    session.queue = session.queue[:idx + 1]
    for q in dropped:
        artist = q.get("artist")
        if artist and artist in session.artist_counts:
            session.artist_counts[artist] -= 1
            if session.artist_counts[artist] <= 0:
                del session.artist_counts[artist]
    # Clear the stale prime BEFORE re-picking: it feeds _exclusions, and a dropped-but-still-primed
    # pick would otherwise be barred from its own re-pick (caught by the force_topup fixture: the
    # rebuild skipped the best eligible track because it happened to be the old prime).
    session.primed = None
    depth = int(rec_params.get_param(store, "radio_seed_depth"))
    _pick_tail(store, session, now, depth)
    session.primed = session.queue[idx + 1] if len(session.queue) > idx + 1 else None
    desired = [q["video_id"] for q in session.queue]
    changed = desired != session.applied_vids
    return {"desired_vids": desired if changed else None, "prime": session.primed}


def force_topup(store, session, now) -> dict:
    """Rebuild the unplayed tail RIGHT NOW from the current model, without a play frame: no advance,
    no fold, position unchanged. Same delete-then-rebuild semantics and no-self-commit contract as
    on_play (the caller commits applied_vids only after its reconcile succeeds). Added as a testing/
    maintenance affordance (the Populate-tail button): lets the owner force a mid-track append and
    watch the live queue react. Returns {desired_vids, prime}; inert dict when inactive or empty.
    Never raises."""
    try:
        with session.lock:
            if not session.active or not session.queue:
                return {"desired_vids": None, "prime": None}
            return _rebuild_tail_at(store, session, now, max(session.pos, 0))
    except Exception:
        return {"desired_vids": None, "prime": None}


def record_skip(store, session, video_id, now) -> None:
    """Append a skip-penalty event for `video_id` (artist + nearest mode of the resolved key). A no-op
    when the video id does not resolve to a library key (nothing to penalize)."""
    if not video_id:
        return
    key = store.identity_key_for_video(video_id)
    if not key:
        return
    m = store.tracks_by_keys([key]).get(key) or {}
    session.skips.append((m.get("artist"), _nearest_mode(key, _modeinfo(store)), now))


def _record_and_mark(store, session, vid, now) -> None:
    """record_skip, plus (dual mode only) mark the standby dirty so it is rebuilt from the model this
    skip just nudged before the imminent toggle delivers it. Inert in single-tab mode: `standby_dirty`
    is never read there."""
    record_skip(store, session, vid, now)
    if session.dual_deck:
        session.standby_dirty = True


def _is_dislike(msg) -> bool:
    # A curation 'rate' pevent carries the action in its url path (.../dislike). Best-effort.
    url = (msg.get("url") or "")
    return url.rstrip("/").rsplit("/", 1)[-1] == "dislike"


def react(store, session, msg, now) -> dict:
    """A pevent: record the session skip signal only. The queue itself reacts on the next play frame
    (on_play), at most one track later, since a native skip is just the user moving inside our own
    queue (the extension advances the YTM player to the next queue entry on its own; there is nothing
    for us to navigate). Returns {desired_vids: None, prime: None} in steady state: react never
    mutates the playlist, it only feeds the model that on_play's next tail rebuild will read.

    Never raises: the whole body runs under a top-level guard, and ANY exception while interpreting
    `msg` (None, a non-dict, a non-string kind, anything) is swallowed and the inert no-op result is
    returned instead.
    """
    try:
        with session.lock:
            if not session.active:
                return {"desired_vids": None, "prime": None}
            kind = (msg.get("kind") or "").strip()
            vid = msg.get("videoId")
            if kind == "track_exit":
                if classify_exit(msg.get("position"), msg.get("duration")) == "skip":
                    _record_and_mark(store, session, vid, now)
            elif kind == "rate":
                if _is_dislike(msg):
                    _record_and_mark(store, session, vid, now)
            elif kind == "volume":
                try:
                    vol = float(msg.get("volume"))
                except (TypeError, ValueError):
                    vol = 1.0
                if vol <= _params(store)["volume_floor"]:
                    _record_and_mark(store, session, vid, now)
            # bye / state / tick / completion / unknown: inert.
            return {"desired_vids": None, "prime": None}
    except Exception:
        return {"desired_vids": None, "prime": None}
