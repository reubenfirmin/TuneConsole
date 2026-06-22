"""Registry of every user-tunable *result-shaping* knob in the recommender.

One source of truth for the Taste Model control panel: each knob's label, plain-English
explanation, range, step, and default live here, and every consumer reads its value through
`get_param(store, name)` (falling back to the default). This replaces magic numbers that were
scattered across recommend.py, so the page can render the knobs generically and reset them.

Lane and genre *weights* are NOT here — those are multiplicative weights stored in the
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


# Each default equals the constant it replaced, so behaviour is unchanged until a user moves it.
PARAMS = [
    ParamSpec("comfort_min_plays", "Comfort min plays", "discovery",
              "Comfort Listening surfaces your high-rotation favorites that have gone quiet. This is "
              "the fewest past plays a track needs to qualify — higher = only your most-worn tracks.",
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
]

PARAMS_BY_NAME = {p.name: p for p in PARAMS}


# Lane weights (rec_weights table, axis `lane:<name>`): labels + help for the page. Clamp [0.2, 3.0].
LANES = [
    ("neighbourhood", "Neighbourhood", "Tracks close to what you've been playing recently."),
    ("rotation", "Rotation", "More of what you play most — your steady rotation."),
    ("deep_cut", "Deep cuts", "Overlooked tracks by artists you already have."),
    ("explore", "Explore", "New-to-you music that still sits near your taste."),
]
LANE_MIN, LANE_MAX, LANE_DEFAULT = 0.2, 3.0, 1.0

# Genre-family weights (rec_weights table, axis `genre:<family>`): 0 mutes, 1 neutral, 2 favors.
GENRE_MIN, GENRE_MAX, GENRE_DEFAULT, GENRE_STEP = 0.0, 2.0, 1.0, 0.1


def _clamp(spec, value) -> float:
    v = max(spec.min, min(spec.max, float(value)))
    return int(round(v)) if spec.integer else v


def get_param(store, name):
    """Current value of a scalar knob — the stored override, clamped, or the registry default."""
    spec = PARAMS_BY_NAME[name]
    raw = store.get_setting(SETTING_PREFIX + name)
    if raw is None or raw == "":
        return spec.default
    try:
        return _clamp(spec, raw)
    except (TypeError, ValueError):
        return spec.default


def set_param(store, name, value) -> None:
    """Persist a scalar knob override (clamped to its spec range)."""
    spec = PARAMS_BY_NAME[name]
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
MOOD_EVENT_CAP = 200           # bound the rec_mood table (count, not age)
STALE_DECAY_HALFLIFE_D = 3     # once sync stale, transient relaxes with this half-life (days)
# Source weights into the transient leans
PLAY_TRANSIENT_W = 0.30        # one recent play's positive push
DISLIKE_TRANSIENT_W = 1.50     # one recent dislike's negative push (strong, explicit)
RECENT_PLAY_LIMIT = 50         # how many recent plays feed the transient leans
# Facet overlay shape (read by _axis_weights_for and roll_recipe). The transient overlay DE-RANKS but
# never banishes: one gentle "less X" roughly halves X in the feed, "a lot"/sustained strongly reduces
# but always leaves some present. Banishing a facet is the job of dislike (a ban) or graduation
# (sustained transient → a lasting permanent-weight nudge), not a single transient gesture.
FACET_GAIN = 0.35
FACET_MULT_MIN = 0.35          # floor: even the strongest transient lean keeps a facet present, not muted
FACET_MULT_MAX = 2.5
# Dislike (permanent suppression side)
DISLIKE_SUPPRESS_DAYS = 365
# Graduation
THEME_THRESHOLD = 1.2
GRADUATE_UP = 1.05
GRADUATE_DOWN = 0.95
