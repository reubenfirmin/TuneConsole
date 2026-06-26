"""Registry of every user-tunable *result-shaping* knob in the recommender.

One source of truth for the Taste Model control panel: each knob's label, plain-English
explanation, range, step, and default live here, and every consumer reads its value through
`get_param(store, name)` (falling back to the default). This replaces magic numbers that were
scattered across recommend.py, so the page can render the knobs generically and reset them.

Lane and genre *weights* are NOT here - those are multiplicative weights stored in the
`rec_weights` table (axes `lane:*` / `genre:*`); see LANES below for their labels/help. This
module owns the scalar params (windows, ratios, counts, penalties), stored in the `settings`
table as `rec_param:<name>` keys.

Embedding internals (dim, method, item2vec hyperparams, seeds) are deliberately NOT exposed:
hand-setting them only degrades quality, so they stay managed by Auto-tune.
"""
from dataclasses import dataclass

SETTING_PREFIX = "rec_param:"


@dataclass(frozen=True)
class ParamSpec:
    name: str
    label: str
    group: str          # section the knob renders under ("discovery")
    explanation: str    # one-line, user-facing
    min: float
    max: float
    step: float
    default: float
    integer: bool = False
    advanced: bool = False
    boolean: bool = False


# Each default equals the constant it replaced, so behaviour is unchanged until a user moves it.
PARAMS = [
    ParamSpec("comfort_min_plays", "Comfort min plays", "discovery",
              "Comfort Listening surfaces your high-rotation favorites that have gone quiet. This is "
              "the fewest past plays a track needs to qualify - higher = only your most-worn tracks.",
              1, 50, 1, 4, integer=True),
    ParamSpec("comfort_recency_full_days", "Comfort recency window (days)", "discovery",
              "Comfort Listening favors tracks you haven't played in a while. A track reaches its "
              "full weight once this many days have passed since its last play; more recent plays "
              "demote it (a track played today barely shows).",
              1, 365, 1, 30, integer=True),
    ParamSpec("erosion_view_cap", "Card rotation cap", "discovery",
              "How many times you can reload Home before each card rotates to a fresh set of "
              "suggestions. Lower = the cards turn over faster.",
              1, 10, 1, 3, integer=True),
    ParamSpec("generated_gc_days", "Generated playlist cleanup (days)", "discovery",
              "A daily worker deletes generated playlists you never play (locally and on YouTube). "
              "Each gets this many days from when it was created to be played; if fewer than half of "
              "its tracks show up in your play history by then, it's collected. Deletions are backed "
              "up and undoable from the Actions page.",
              1, 365, 1, 7, integer=True),
    ParamSpec("palette_absence_penalty", "Out-of-palette penalty", "discovery",
              "How hard to penalize genres absent from your library (scaled by how eclectic you "
              "are). Higher = stick closer to genres you already have.",
              0.0, 2.0, 0.05, 0.5),
    ParamSpec("candidate_pool_factor", "Candidate pool depth", "discovery",
              "How many candidates each lane fetches per slot shown. Deeper = more variety for "
              "erosion to rotate through, at a little more compute.",
              2, 10, 1, 4, integer=True, advanced=True),
    ParamSpec("dislike_suppress_days", "Dislike ban length (days)", "discovery",
              "How long a thumbs-down hides a track before it can resurface.",
              1, 3650, 1, 365, integer=True),
    ParamSpec("cluster_content_weight", "Cluster content blend", "discovery",
              "How much the Clusters canvas leans on what tracks SOUND like (genre/era) vs how you've "
              "filed them in playlists. 0 = pure playlist co-occurrence (old behaviour); higher = more "
              "musical similarity, so a seed reaches its own genre even when you never playlist it there.",
              0.0, 1.0, 0.05, 0.30),
    # Cluster-ring shaping (defaults mirror embed.CLUSTER_BETA / embed.SEED_FANOUT; both apply at query
    # time, so a change takes effect on the next grow with no rebuild).
    ParamSpec("cluster_beta", "Cluster prune strength", "discovery",
              "On the Clusters canvas, how hard pruning a track ('not this') steers a branch away from "
              "similar music. 0 = a pruned track is just removed; higher = the whole ring leans away "
              "from its neighbourhood, so one prune reshapes more of what grows next.",
              0.0, 2.0, 0.05, 0.60),
    ParamSpec("cluster_seed_spread", "Cluster seed spread", "discovery",
              "When a Clusters node grows from several pinned tracks, how much the ring reaches toward "
              "each individual seed's neighbourhood vs their blended average. 0 = pure average; higher = "
              "a minority pick (one odd track among many) still pulls in its own kind, not just the "
              "centre of mass.",
              0.0, 1.0, 0.05, 0.50, advanced=True),
    # --- Artist relationship model (#28). How its blocks combine when relating artists. content_weight
    # is the §B (genre/era/audio) fraction vs the §A co-curation block (collab = 1 - content, the
    # cluster_content_weight convention); edge_weight adds the §C Last.fm-edge bonus on top. ---
    ParamSpec("artist_content_weight", "Artist content blend", "discovery",
              "When relating artists, how much to lean on what they SOUND like (genre/era/audio) vs who "
              "you co-curate them with. 0 = pure co-curation; higher = more genre/audio similarity, which "
              "also lets artists with thin co-curation (or none) still be placed.",
              0.0, 1.0, 0.05, 0.30, advanced=True),
    ParamSpec("artist_edge_weight", "Artist Last.fm-edge weight", "discovery",
              "How much a Last.fm similar-artist edge adds when relating artists, on top of co-curation "
              "and content. Reaches out-of-corpus artists the user hasn't curated together yet.",
              0.0, 1.0, 0.05, 0.10, advanced=True),
    # --- Breadth steering (#7). The interactive Breadth bar on the Home fingerprint binds to
    # breadth_bias; breadth_gain is its (advanced) sensitivity. Default 0 == today's behaviour. ---
    ParamSpec("breadth_bias", "Breadth", "discovery",
              "Tilts the feed toward your focused core (left) or your eclectic edges (right). The "
              "center follows your natural breadth and changes nothing; drag right to surface your "
              "under-played genres more, left to concentrate on your dominant ones. Redistributes "
              "across genres you already have - it doesn't pull in brand-new ones.",
              -1.0, 1.0, 0.05, 0.0),
    ParamSpec("breadth_gain", "Breadth sensitivity", "discovery",
              "How hard a full Breadth drag bites. At 1.0, a full drag roughly doubles a family that "
              "sits at half (or twice) your average share.",
              0.2, 3.0, 0.05, 1.0, advanced=True),
    # --- Right-now responsiveness (transient model). Defaults == the constants below. ---
    ParamSpec("play_transient_w", "Recent-play push", "transient",
              "How hard a recent play tilts the feed toward similar music. 0 = recent plays don't "
              "steer; higher = your last plays dominate.", 0.0, 2.0, 0.05, 0.30),
    ParamSpec("like_transient_w", "Recent-like push", "transient",
              "How hard a recent thumbs-up tilts the feed (stronger than a passive play by default).",
              0.0, 2.0, 0.05, 0.45),
    ParamSpec("dislike_transient_w", "Recent-dislike push", "transient",
              "How hard a recent thumbs-down pushes the feed away from similar music.",
              0.0, 3.0, 0.05, 1.50),
    ParamSpec("audio_transient_w", "Recent-sound push", "transient",
              "How hard recent listening tilts the feed toward similar SOUND (tempo, energy, mood), "
              "not just genre/era. 0 = the sound of recent plays doesn't steer.", 0.0, 2.0, 0.05, 0.30),
    ParamSpec("facet_gain", "Facet responsiveness", "transient",
              "How strongly a genre/era/artist lean re-ranks the feed. Higher = right-now leans bite "
              "harder.", 0.0, 1.0, 0.05, 0.35),
    ParamSpec("mood_alpha", "Mood tilt", "transient",
              "How hard a mood gesture tilts the lanes, relative to the underlying taste score.",
              0.0, 1.0, 0.05, 0.35),
    ParamSpec("mood_recency_alpha", "Recency emphasis", "transient",
              "How much your newest interactions dominate over older ones. Higher = only the very "
              "latest plays/likes matter.", 0.05, 0.9, 0.05, 0.35),
    ParamSpec("recent_play_limit", "Recent window (tracks)", "transient",
              "How many recent plays/likes feed the right-now model.", 5, 200, 5, 50, integer=True),
    ParamSpec("stale_decay_halflife_d", "Right-now relax half-life (days)", "transient",
              "Once a sync goes stale, how fast the right-now model relaxes back to your durable taste.",
              1, 30, 1, 3, integer=True),
    ParamSpec("facet_mult_min", "Facet floor", "transient",
              "Even the strongest negative lean keeps a facet at least this present (never fully muted).",
              0.0, 1.0, 0.05, 0.35, advanced=True),
    ParamSpec("facet_mult_max", "Facet ceiling", "transient",
              "Cap on how much a positive lean can boost a facet.", 1.0, 4.0, 0.1, 2.5, advanced=True),
    # --- Learning (graduation: right-now -> permanent). Defaults == the constants below. ---
    ParamSpec("graduation_enabled", "Learning enabled", "graduation",
              "When on, sustained right-now behavior gradually rewrites your durable taste weights. "
              "Turn off to freeze your permanent taste and let right-now effects stay temporary.",
              0, 1, 1, True, boolean=True),
    ParamSpec("theme_threshold", "Graduation threshold", "graduation",
              "How much sustained signal a genre/era/artist must accumulate before it nudges your "
              "permanent taste. Higher = learning is slower and more deliberate.", 0.2, 5.0, 0.1, 1.2),
    ParamSpec("graduate_up", "Graduate-up step", "graduation",
              "Permanent weight multiplier applied when a facet graduates upward.",
              1.0, 1.5, 0.01, 1.05, advanced=True),
    ParamSpec("graduate_down", "Graduate-down step", "graduation",
              "Permanent weight multiplier applied when a facet graduates downward.",
              0.5, 1.0, 0.01, 0.95, advanced=True),
    ParamSpec("source_w_like", "Speed: likes", "graduation",
              "How fast an explicit like graduates toward permanent taste.", 0.0, 2.0, 0.05, 1.0, advanced=True),
    ParamSpec("source_w_dislike", "Speed: dislikes", "graduation",
              "How fast an explicit dislike graduates toward permanent taste.", 0.0, 2.0, 0.05, 1.0, advanced=True),
    ParamSpec("source_w_vibe", "Speed: mood gestures", "graduation",
              "How fast a mood/vibe gesture graduates toward permanent taste.", 0.0, 2.0, 0.05, 1.0, advanced=True),
    ParamSpec("source_w_feedback", "Speed: suggestion feedback", "graduation",
              "How fast a suggestion dismiss (wrong vibe/era, too mainstream, not this artist) and the "
              "Home why-chips steer your permanent taste. Routed through the same graduation ledger as "
              "every other signal.", 0.0, 2.0, 0.05, 1.0, advanced=True),
    ParamSpec("pop_mainstream_min", "Mainstream threshold", "discovery",
              "Deezer popularity (rank) at or above which a track counts as 'mainstream', the axis the "
              "'too mainstream' dismiss steers. Tracks with no popularity value are never mainstream.",
              0, 1000000, 50000, 500000, integer=True, advanced=True),
    # The intent-vs-behavior graduation balance: two visible (non-advanced) knobs. Both channels now
    # graduate by the same daily-exposure mechanic, so the weights are directly comparable (per-day vs
    # per-day). The default ratio 0.5 : 0.08 (~6:1) encodes "explicit steering moves long-term taste
    # faster than passive listening" (spec §2).
    ParamSpec("source_w_slider", "Intent: nudge bars", "graduation",
              "How fast your explicit steering (the Home nudge bars, while held) graduates into "
              "permanent taste, per day held. The intent side of the intent-vs-behavior balance.",
              0.0, 2.0, 0.05, 0.5),
    ParamSpec("source_w_play", "Behavior: listens", "graduation",
              "How fast your listening graduates into permanent taste, per day of sustained plays "
              "(exposure, the same mechanic as the nudge bars). The behavior side of the balance; kept "
              "below intent so passive listening doesn't silently rewrite taste.",
              0.0, 1.0, 0.01, 0.08),
    ParamSpec("play_grad_session_cap", "Play graduation cap / session", "graduation",
              "(Deprecated, unused since plays graduate by daily exposure.) Maximum total play-"
              "graduation contribution from one listening session.",
              0.0, 2.0, 0.05, 0.4, advanced=True),
]

PARAMS_BY_NAME = {p.name: p for p in PARAMS}


# Lane weights (rec_weights table, axis `lane:<name>`): labels + help for the page. Clamp [0.2, 3.0].
LANES = [
    ("neighbourhood", "Neighbourhood", "Tracks close to what you've been playing recently."),
    ("rotation", "Rotation", "More of what you play most - your steady rotation."),
    ("deep_cut", "Deep cuts", "Overlooked tracks by artists you already have."),
    ("explore", "Explore", "New-to-you music that still sits near your taste."),
]
LANE_MIN, LANE_MAX, LANE_DEFAULT = 0.2, 3.0, 1.0

# Genre-family weights (rec_weights table, axis `genre:<family>`): 0 mutes, 1 neutral, 2 favors.
GENRE_MIN, GENRE_MAX, GENRE_DEFAULT, GENRE_STEP = 0.0, 2.0, 1.0, 0.1

# #18: how much of the transient facet lean reaches OUTWARD discovery (new artists/albums). Damped
# (<1) vs the in-library surfaces, so the deliberately-stable discovery pool isn't yanked around by a
# single mood gesture; permanent genre weights still apply at full strength (and 0 hard-excludes).
DISCOVERY_TRANSIENT_DAMP = 0.5


def _clamp(spec, value):
    if spec.boolean:
        return value.lower() in ("1", "true", "yes", "on") if isinstance(value, str) else bool(value)
    v = max(spec.min, min(spec.max, float(value)))
    return int(round(v)) if spec.integer else v


def get_param(store, name):
    """Current value of a scalar knob - the stored override, clamped, or the registry default."""
    spec = PARAMS_BY_NAME[name]
    raw = store.get_setting(SETTING_PREFIX + name)
    if raw is None or raw == "":
        return spec.default
    if spec.boolean:
        return raw == "1"
    try:
        return _clamp(spec, raw)
    except (TypeError, ValueError):
        return spec.default


def set_param(store, name, value) -> None:
    """Persist a scalar knob override (clamped to its spec range)."""
    spec = PARAMS_BY_NAME[name]
    if spec.boolean:
        store.set_setting(SETTING_PREFIX + name, "1" if _clamp(spec, value) else "0")
    else:
        store.set_setting(SETTING_PREFIX + name, str(_clamp(spec, value)))


def reset_param(store, name) -> None:
    """Drop a scalar knob's override so it reverts to the registry default."""
    PARAMS_BY_NAME[name]   # validate the name
    store.delete_setting(SETTING_PREFIX + name)


def reset_all_params(store) -> None:
    """Revert every scalar knob to its default."""
    for p in PARAMS:
        store.delete_setting(SETTING_PREFIX + p.name)


# --- Transient model (lifecycle / recency) ---
SYNC_STALE_S = 24 * 3600       # sync older than this is "stale" (also used by recommend.sync_status)
MOOD_RECENCY_ALPHA = 0.35      # EMA over interaction RANK; newest pick ~35% of the blend
STALE_DECAY_HALFLIFE_D = 3     # once sync stale, transient relaxes with this half-life (days)
# Source weights into the transient leans
PLAY_TRANSIENT_W = 0.30        # one recent play's positive push
LIKE_TRANSIENT_W = 0.45        # one recent like's positive push to facet leans (stronger than a play)
DISLIKE_TRANSIENT_W = 1.50     # one recent dislike's negative push (strong, explicit)
RECENT_PLAY_LIMIT = 50         # how many recent plays feed the transient leans
# Facet overlay shape (read by _axis_weights_for and roll_recipe). The transient overlay DE-RANKS but
# never banishes: one gentle "less X" roughly halves X in the feed, "a lot"/sustained strongly reduces
# but always leaves some present. Banishing a facet is the job of dislike (a ban) or graduation
# (sustained transient → a lasting permanent-weight nudge), not a single transient gesture.
FACET_GAIN = 0.35              # slope: how hard a unit lean bends the facet multiplier off 1.0
FACET_MULT_MIN = 0.35          # floor: even the strongest transient lean keeps a facet present, not muted
FACET_MULT_MAX = 2.5           # ceiling: cap on how much a positive lean can boost a facet
# Dislike (permanent suppression side)
DISLIKE_SUPPRESS_DAYS = 365
# Graduation
THEME_THRESHOLD = 1.2
GRADUATE_UP = 1.05
GRADUATE_DOWN = 0.95
# --- Graduation source weights (Approach 1: source-aware funnel) ---
# Each transient signal contributes a source-weighted amount to rec_theme. The transient effect is
# immediate; graduation is gated by accumulation past THEME_THRESHOLD. Calibrated so today's vibe
# lever is preserved (a mood event still contributes ±1 with SOURCE_W_VIBE=1.0).
SOURCE_W_VIBE = 1.0       # vibe / facet lever (unchanged from today)
SOURCE_W_LIKE = 1.0       # explicit thumbs-up - strong
SOURCE_W_DISLIKE = 1.0    # thumbs-down (sign supplied by caller) - strong
SOURCE_W_SLIDER = 0.5     # per held-day of a full lean (~2-3 held days -> one graduation step)
SOURCE_W_PLAY = 0.08      # one play - weak; passive listening must not silently rewrite taste
PLAY_GRAD_SESSION_CAP = 0.4   # max play-graduation contribution per session (a binge can't graduate)
