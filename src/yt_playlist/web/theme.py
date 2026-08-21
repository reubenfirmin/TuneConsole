"""Theme lookups for server-rendered colour.

Python never names a colour. It names a ROLE, and returns a CSS `var(--token)`
reference that resolves against static/tokens.css — the one file in the codebase
allowed to hold a colour literal. That keeps a retheme to a single file instead
of a grep across templates, stylesheets, JS canvases and these modules.

Two genre sets exist on purpose, because they do different jobs:
  family_hue()   -> the cohesive COOL band set (--fam-*), for charts and the
                    recap aura, so a family keeps the hue its chart band has.
  genre_tint()   -> the saturated tint set (--genre-*), for glowing subject
                    cards, where the point is a fun, high-chroma wash.
They used to be two hard-coded dicts in two modules that disagreed with each
other on every shared key (house was violet in one and orange in the other).
"""

# Families that have a dedicated chart band. Anything else falls back.
_BAND_FAMILIES = frozenset({"ambient", "electro-synth", "house", "techno", "trance"})

# Families with a dedicated saturated tint (mirrors the --genre-* roles).
_TINT_FAMILIES = frozenset({
    "techno", "house", "trance", "dnb", "breakbeat", "garage-bass", "ambient",
    "electro-synth", "rock-classic", "rock-indie", "rock-post", "metal", "punk",
    "pop", "hiphop", "soul-funk", "jazz", "blues", "folk-country", "world-latin",
    "classical", "experimental",
})


def family_hue(family: str | None) -> str:
    """CSS colour for a genre family in the chart/aura band set."""
    return f"var(--fam-{family})" if family in _BAND_FAMILIES else "var(--fam-other)"


def genre_tint(token: str | None) -> str:
    """CSS colour for a genre family in the saturated subject-card tint set."""
    return f"var(--genre-{token})" if token in _TINT_FAMILIES else "var(--genre-default)"
