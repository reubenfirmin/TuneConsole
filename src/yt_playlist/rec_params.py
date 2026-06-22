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
    ParamSpec("neighbourhood_taste_ratio", "Taste vs. recent mood", "discovery",
              "Balance of the neighbourhood lane: 1.0 = pure overall taste, 0.0 = only your "
              "last-day mood. The remainder goes to recent mood.",
              0.0, 1.0, 0.05, 0.70),
    ParamSpec("recent_mood_window_hours", "Recent-mood window (hours)", "discovery",
              "How far back 'recent mood' looks when tilting the neighbourhood lane.",
              1, 168, 1, 24, integer=True),
    ParamSpec("recent_mood_tracks", "Recent-mood tracks", "discovery",
              "How many of your latest plays define your current mood.",
              1, 50, 1, 12, integer=True),
    ParamSpec("explore_top_artists", "Explore skips top N artists", "discovery",
              "Explore stays novel by skipping your most-played N artists.",
              0, 100, 1, 25, integer=True),
    ParamSpec("erosion_view_cap", "Card rotation cap", "discovery",
              "How many times you can reload Home before each card rotates to a fresh set of "
              "suggestions. Lower = the cards turn over faster.",
              1, 10, 1, 3, integer=True),
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
