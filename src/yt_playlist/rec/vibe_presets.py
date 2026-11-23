"""Listening purposes and their metadata priors."""
from .musical_profiles import dimension


# Listening purposes, with provisional cross-genre sound preferences. The full
# descriptions also define the internal model-development labeling task; those
# raw annotations are not ranking inputs or track overrides. Genre is never a target.
PRESETS = {
    "relax": {"label": "Relax", "icon": "feather", "color": "mint",
              "attributes": "Calm, soothing, unhurried",
              "description": "Helps me settle down and release tension.",
              "signals": "Calm, soothing, unhurried. Helps me settle down and release tension.",
              "weights": {"relaxed": 3, "intensity": -2, "aggression": -1}},
    "concentrate": {"label": "Focus", "icon": "orbit", "color": "blue",
                    "attributes": "Steady, absorbing, unobtrusive",
                    "description": "Supports concentration without repeatedly grabbing my attention.",
                    "signals": "Steady, absorbing, unobtrusive. Supports concentration without repeatedly grabbing my attention.",
                    "weights": {"instrumental": 2, "relaxed": 1, "intensity": -1,
                                "aggression": -2, "density": -1}},
    "cruise": {"label": "Cruise", "icon": "moon", "color": "lilac",
               "attributes": "Easygoing, moderately lively, comfortable company",
               "description": "Keeps everyday listening enjoyable without pushing me toward a particular mood.",
               "signals": "Easygoing, moderately lively, comfortable company. Keeps everyday listening enjoyable without pushing me toward a particular mood.",
               "weights": {"intensity": 3, "relaxed": 1, "aggression": -2},
               "targets": {"intensity": .5}},
    "energize": {"label": "Energize", "icon": "pulse", "color": "orange",
                 "attributes": "Driving, animated, propulsive",
                 "description": "Gives me momentum and makes me feel more active.",
                 "signals": "Driving, animated, propulsive. Gives me momentum and makes me feel more active.",
                 "weights": {"intensity": 1}},
    "uplift": {"label": "Lift my mood", "icon": "sun", "color": "gold",
               "attributes": "Positive, buoyant, encouraging",
               "description": "Gives an emotional lift, whether gentle or energetic.",
               "signals": "Positive, buoyant, encouraging. Gives an emotional lift, whether gentle or energetic.",
               "weights": {"happiness": 3, "sadness": -1}},
    "release": {"label": "Let off steam", "icon": "spark", "color": "rose",
                "attributes": "Forceful, hard-hitting, cathartic",
                "description": "Delivers intensity and impact; can be slow and heavy.",
                "signals": "Forceful, hard-hitting, cathartic. Delivers intensity and impact; can be slow and heavy.",
                "weights": {"aggression": 4, "intensity": 1}},
}


def axis_fit(preset, name, value):
    """A measured midpoint can be desirable; an unknown value stays neutral."""
    if value is None:
        return .5
    target = PRESETS[preset].get('targets', {}).get(name)
    if target is not None:
        return 1 - abs(value - target) / max(target, 1 - target)
    return value if PRESETS[preset]['weights'][name] > 0 else 1 - value


def heuristic_affinity(profile, preset):
    """Compute the weak catalog prior used during Bayesian preparation."""
    weights = PRESETS[preset]["weights"]
    if not profile:
        return .5
    total = evidence = 0.0
    for name, weight in weights.items():
        value, support = dimension(profile, name)
        if value is not None:
            # Intensity already contains conservative shrinkage. A midpoint
            # target still needs support: uncertainty is not measured moderation.
            reliability = (1 if name == 'intensity' and name not in PRESETS[preset].get('targets', {})
                           else min(1, support / .6))
            evidence += abs(weight) * (axis_fit(preset, name, value) - .5) * reliability
        total += abs(weight)
    return 0.5 + evidence / total
